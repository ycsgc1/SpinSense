"""Last.fm scrobbling.

Lives in the GUI process because that is where the play history is. The engine
never sees any of this — it just reports what's playing, and `ipc_manager`
writes the rows this module later submits.

Three pieces, in dependency order:

1. **Pure protocol** — request signing, the auth URL, batch parameter building,
   response interpretation. No I/O, so the fiddly parts (Last.fm's signature
   algorithm, its habit of returning an object where you expect a list) are
   directly testable.
2. **The HTTP layer** — one `_api_post`, isolated so tests stub a single seam.
3. **The queue** — a periodic flush that drains eligible, unscrobbled plays.

**Auth model.** Two routes to the same session key, because a LAN appliance
has no fixed public address:

- **Redirect (default).** Last.fm's web flow accepts a per-request `cb`
  parameter overriding the callback registered on the API account, so we build
  one from the address the browser is *already* using to reach SpinSense. The
  user clicks once, sees Last.fm's own login page, approves, and lands back on
  the Settings page connected. Their password never touches SpinSense.
- **Manual (fallback).** The desktop flow: mint a request token, hand over a
  URL to approve it, and let the user tell us when they're done. Needed when
  the redirect can't come back — approving on a phone, or reaching SpinSense
  through something that rewrites the origin.

Both end at `auth.getSession`, so the difference is only how the token is
obtained.

**Credentials.** SpinSense ships its own Last.fm application key so connecting
is one click, the way Pano Scrobbler and Web Scrobbler do it. `credentials()`
resolves them, most specific first: the user's own from config, then an
environment override, then the built-in pair. Bring-your-own stays as an
advanced path and as the escape hatch if the shared key is ever rate-limited or
revoked.
"""
import asyncio
import hashlib
import logging
import os
import time
import urllib.parse

import play_history
from config_manager import load_config, save_config

log = logging.getLogger(__name__)

# SpinSense's own Last.fm application. This is NOT a secret and is not treated
# as one: it ships in a public repository and inside every published image, so
# anyone can read it. That is the accepted cost of one-click login, and it is
# what every user-facing scrobbler does — Last.fm has no public-client flow
# (no PKCE equivalent), and auth.getSession cannot be signed without the secret.
#
# What it does NOT grant: access to anyone's Last.fm account. Scrobbling still
# requires a per-user session key obtained through the approval flow below.
# The realistic blast radius is someone burning the shared rate limit or getting
# the key revoked — at which point every install falls back to bring-your-own,
# which is why that path is kept rather than removed.
#
# Registered to the Last.fm account `ycsgc` as "Spinsense" — that is where to go
# to rotate or revoke it. Override without editing code via
# SPINSENSE_LASTFM_KEY / SPINSENSE_LASTFM_SECRET.
BUILTIN_API_KEY = "86fcabef3f02c9b51a8620628033b221"
BUILTIN_API_SECRET = "5961449e958b192c40d75045b881b924"

API_ROOT = "https://ws.audioscrobbler.com/2.0/"
AUTH_URL = "https://www.last.fm/api/auth/"

# Last.fm accepts at most 50 scrobbles per track.scrobble call.
MAX_BATCH = 50

# How often the background flush runs. Scrobbles are not time-critical (the
# timestamp is what counts, not when we submit), so this stays gentle.
FLUSH_INTERVAL_SECS = 120

# Last.fm rejects scrobbles older than 14 days outright; don't spend calls on
# them. Slightly under, to leave room for clock skew.
MAX_SCROBBLE_AGE_SECS = 13 * 24 * 3600

# Error codes worth naming. 9 is the one that needs the user: the session key
# was revoked (password change, app removed) and only re-authorising fixes it.
ERROR_INVALID_SESSION = 9
ERROR_RATE_LIMIT = 29
# Transient service problems — keep the rows pending and try again later.
RETRYABLE_ERRORS = {8, 11, 16, ERROR_RATE_LIMIT}


# --- 1. Pure protocol ------------------------------------------------------

def sign(params: dict, secret: str) -> str:
    """Last.fm's api_sig: every parameter except `format` and `callback`, sorted
    by name, concatenated as name+value with no separators, the shared secret
    appended, then MD5.

    MD5 is Last.fm's choice, not ours — it is a protocol requirement here, not a
    security primitive, hence usedforsecurity=False.
    """
    raw = "".join(
        f"{k}{v}" for k, v in sorted(params.items())
        if k not in ("format", "callback", "api_sig")
    ) + secret
    return hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).hexdigest()


def signed(params: dict, secret: str) -> dict:
    """`params` plus its signature and a JSON format request."""
    out = dict(params)
    out["api_sig"] = sign(params, secret)
    out["format"] = "json"
    return out


def auth_url(api_key: str, token: str) -> str:
    """Manual flow: where the user goes to approve a token we minted."""
    return f"{AUTH_URL}?api_key={api_key}&token={token}"


def callback_url(origin: str) -> str | None:
    """Our callback endpoint, at the address the user reaches SpinSense on.

    `origin` comes from the browser, and we hand the result to Last.fm, so it is
    checked rather than trusted: a bare http(s) scheme and host, nothing else.
    Returns None if it doesn't look like one.
    """
    try:
        parsed = urllib.parse.urlsplit((origin or "").strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    # A real origin has no path, query or fragment. Anything else is either a
    # mistake or someone trying to steer the redirect.
    if parsed.path.strip("/") or parsed.query or parsed.fragment:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/api/lastfm/callback"


def web_auth_url(api_key: str, callback: str) -> str:
    """Redirect flow: Last.fm mints its own token and hands it to `callback`.

    No token of ours is involved — that's the whole point. Last.fm shows its
    login page if needed, then returns the user to us already approved.
    """
    query = urllib.parse.urlencode({"api_key": api_key, "cb": callback})
    return f"{AUTH_URL}?{query}"


def build_scrobble_params(plays: list[dict]) -> dict:
    """Indexed batch parameters for track.scrobble.

    `album` and `duration` are optional and omitted rather than sent empty —
    Last.fm matches better with a field absent than blank, and an empty value
    would still have to be signed.
    """
    params: dict = {}
    for i, play in enumerate(plays):
        params[f"artist[{i}]"] = play["artist"]
        params[f"track[{i}]"] = play["title"]
        params[f"timestamp[{i}]"] = str(int(play["timestamp"]))
        album = play.get("album")
        if album and album != "Unknown Album":
            params[f"album[{i}]"] = album
        duration = play.get("duration_secs")
        if duration:
            params[f"duration[{i}]"] = str(int(duration))
    return params


def read_scrobble_result(body) -> tuple[int, int]:
    """(accepted, ignored) from a track.scrobble response.

    Last.fm returns `scrobbles.@attr` for a batch but plain `scrobbles.scrobble`
    as a bare object for a single item, and the counts are strings. Normalising
    here keeps the caller from caring.
    """
    if not isinstance(body, dict):
        return 0, 0
    scrobbles = body.get("scrobbles")
    if not isinstance(scrobbles, dict):
        return 0, 0
    attr = scrobbles.get("@attr")
    if isinstance(attr, dict):
        try:
            return int(attr.get("accepted", 0)), int(attr.get("ignored", 0))
        except (TypeError, ValueError):
            return 0, 0
    # Single-scrobble shape: one object, no @attr.
    one = scrobbles.get("scrobble")
    if isinstance(one, dict):
        ignored = one.get("ignoredMessage", {})
        code = ignored.get("code") if isinstance(ignored, dict) else None
        return (0, 1) if code not in (None, "0", 0) else (1, 0)
    return 0, 0


def is_too_old(play: dict, now: int | None = None) -> bool:
    """Whether Last.fm would reject this scrobble's timestamp as stale."""
    now = now if now is not None else int(time.time())
    return (now - int(play["timestamp"])) > MAX_SCROBBLE_AGE_SECS


# --- 2. Configuration ------------------------------------------------------

def settings() -> dict:
    return (load_config() or {}).get("LastFM", {}) or {}


def _pair(key: str, secret: str) -> tuple[str, str] | None:
    """A credential pair only if both halves are present. Half a pair is worse
    than none: mixing a user's key with the built-in secret produces a signature
    Last.fm rejects, with an error that points nowhere useful."""
    key, secret = (key or "").strip(), (secret or "").strip()
    return (key, secret) if key and secret else None


def credentials(cfg: dict | None = None) -> tuple[str, str]:
    """The Last.fm application key and secret to authenticate with.

    Most specific wins: the user's own from config, then the environment, then
    the pair SpinSense ships with. Returns ("", "") only if none resolve, which
    means the build has no built-in key and the user hasn't supplied one.
    """
    cfg = cfg if cfg is not None else settings()
    return (
        _pair(cfg.get("API_Key", ""), cfg.get("API_Secret", ""))
        or _pair(os.environ.get("SPINSENSE_LASTFM_KEY", ""),
                 os.environ.get("SPINSENSE_LASTFM_SECRET", ""))
        or _pair(BUILTIN_API_KEY, BUILTIN_API_SECRET)
        or ("", "")
    )


def uses_own_credentials(cfg: dict | None = None) -> bool:
    """Whether the user supplied their own key rather than using the built-in."""
    cfg = cfg if cfg is not None else settings()
    return _pair(cfg.get("API_Key", ""), cfg.get("API_Secret", "")) is not None


def is_connected(cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else settings()
    api_key, api_secret = credentials(cfg)
    return bool(api_key and api_secret and cfg.get("Session_Key"))


def is_active(cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else settings()
    return bool(cfg.get("Enabled")) and is_connected(cfg)


def _store(**fields) -> bool:
    """Merge fields into config.json's LastFM section, leaving the rest alone."""
    cfg = load_config()
    cfg.setdefault("LastFM", {}).update(fields)
    return save_config(cfg)


# --- 3. HTTP ---------------------------------------------------------------

async def _api_post(params: dict) -> tuple[dict | None, str | None]:
    """POST to the Last.fm API. Returns (body, error_message).

    The single network seam in this module — tests stub exactly this. A non-2xx
    with a parseable body still returns the body, because Last.fm reports its
    own error codes with HTTP 400/403 and those codes are what we branch on.
    """
    import aiohttp  # late import: keeps this module importable in bare test envs

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(API_ROOT, data=params) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    return None, f"Last.fm returned unparseable HTTP {resp.status}"
                return body, None
    except asyncio.TimeoutError:
        return None, "Timed out talking to Last.fm"
    except Exception as e:
        return None, f"{e.__class__.__name__}: {e}"


def _error_of(body) -> tuple[int | None, str]:
    if isinstance(body, dict) and "error" in body:
        try:
            code = int(body["error"])
        except (TypeError, ValueError):
            code = None
        return code, str(body.get("message") or "Last.fm error")
    return None, ""


# --- 4. Auth flow ----------------------------------------------------------

async def request_token(api_key: str, api_secret: str) -> tuple[str | None, str]:
    """Step one: a request token for the user to approve. (token, error)."""
    if not api_key or not api_secret:
        return None, "API key and secret are both required"
    body, err = await _api_post(
        signed({"method": "auth.getToken", "api_key": api_key}, api_secret))
    if err:
        return None, err
    code, message = _error_of(body)
    if code is not None:
        return None, message
    token = (body or {}).get("token")
    if not token:
        return None, "Last.fm returned no token"
    return token, ""


async def complete_auth(api_key: str, api_secret: str, token: str) -> tuple[str, str]:
    """Step two: trade an approved token for a permanent session key.

    On success the key and username are persisted, and `Scrobble_Since` is
    stamped at now — see `pending()` for why. Returns (username, error).
    """
    if not token:
        return "", "No pending authorisation — start again"
    body, err = await _api_post(signed(
        {"method": "auth.getSession", "api_key": api_key, "token": token},
        api_secret))
    if err:
        return "", err
    code, message = _error_of(body)
    if code is not None:
        return "", message
    session = (body or {}).get("session") or {}
    key, username = session.get("key"), session.get("name") or ""
    if not key:
        return "", "Last.fm returned no session key"
    if not _store(Session_Key=key, Username=username,
                  Scrobble_Since=int(time.time()), Enabled=True):
        return "", "Authorised, but writing config.json failed"
    return username, ""


def disconnect() -> bool:
    """Forget the session. The API key and secret stay — they're the user's
    application registration, not a login, and re-connecting shouldn't mean
    re-typing them."""
    return _store(Session_Key="", Username="", Enabled=False)


# --- 5. Submission ---------------------------------------------------------

async def update_now_playing(track: dict) -> None:
    """Best-effort 'listening now' on the user's profile. Never raises: this is
    decorative, and a failure must not disturb recording a play."""
    cfg = settings()
    if not is_active(cfg) or not cfg.get("Scrobble_Now_Playing", True):
        return
    api_key, api_secret = credentials(cfg)
    params = {
        "method": "track.updateNowPlaying",
        "api_key": api_key,
        "sk": cfg["Session_Key"],
        "artist": track.get("artist") or "",
        "track": track.get("title") or "",
    }
    if not params["artist"] or not params["track"]:
        return
    album = track.get("album")
    if album and album != "Unknown Album":
        params["album"] = album
    duration = track.get("duration_secs")
    if duration:
        params["duration"] = str(int(duration))

    body, err = await _api_post(signed(params, api_secret))
    if err:
        log.debug("last.fm now-playing failed: %s", err)
        return
    code, message = _error_of(body)
    if code is not None:
        log.warning("last.fm now-playing rejected (%s): %s", code, message)


DEFAULT_DELAY_MINS = 30


def submit_delay_secs(cfg: dict | None = None) -> int:
    """How long to wait after the trigger before a play can be submitted."""
    cfg = cfg if cfg is not None else settings()
    try:
        return max(0, int(cfg.get("Submit_Delay_Mins", DEFAULT_DELAY_MINS))) * 60
    except (TypeError, ValueError):
        return DEFAULT_DELAY_MINS * 60


def submit_trigger(cfg: dict | None = None) -> str:
    """What starts the hold clock: the track ending, or the whole album."""
    cfg = cfg if cfg is not None else settings()
    return "track" if cfg.get("Submit_Trigger") == "track" else "album"


def release_time(play: dict, trigger: str, delay_secs: int,
                 db_path: str | None = None) -> int | None:
    """When this play becomes submittable, or None while it is still playing.

    Last.fm has no API to edit or delete a scrobble once sent, so the only
    place a wrong identification can be caught is before it goes — and holding
    a play means deleting or correcting it in SpinSense, which already excludes
    it from the queue, actually prevents the scrobble instead of arriving too
    late to matter.

    On the **album** trigger the clock starts when the whole record finishes,
    not each track. A side is played as a unit: nothing releases while it is
    still spinning, then the entire side releases together once it has been
    off for the delay. That is also the window in which a mislabelled track is
    most likely to be noticed, because the record it belongs to is still on.

    Falls back to the track's own end when the album is unknown — there is no
    record to wait for.
    """
    ended_at = play.get("ended_at")
    if ended_at is None:
        return None   # still playing; nothing to submit yet anyway
    reference = int(ended_at)
    if trigger == "album":
        last = play_history.album_last_ended(
            play.get("artist", ""), play.get("album"),
            play.get("played_at", reference), db_path)
        if last is not None:
            reference = max(reference, int(last))
    return reference + delay_secs


def is_held(play: dict, now: int | None = None, delay_secs: int | None = None,
            trigger: str | None = None, db_path: str | None = None) -> bool:
    """Whether this play is still inside its review window."""
    now = now if now is not None else int(time.time())
    delay_secs = delay_secs if delay_secs is not None else submit_delay_secs()
    trigger = trigger if trigger is not None else submit_trigger()
    release = release_time(play, trigger, delay_secs, db_path)
    return release is None or now < release


def pending(limit: int = MAX_BATCH, db_path: str | None = None,
            include_held: bool = False) -> list[dict]:
    """Eligible, unscrobbled plays, oldest first.

    Bounded below by `Scrobble_Since` — the moment the account was connected.
    Without it, connecting Last.fm after months of listening would dump the
    entire back catalogue at the API, most of it too old to be accepted anyway.

    Plays inside the review window are withheld unless `include_held`, which is
    what the "Send now" button passes: an explicit release by someone who has
    just looked at them.
    """
    cfg = settings()
    since = int(cfg.get("Scrobble_Since") or 0)
    rows = play_history.scrobble_candidates(
        since=since, limit=limit, pending_only=True, db_path=db_path)
    eligible = [r for r in rows if r["eligible"]]
    if include_held:
        return eligible
    now = int(time.time())
    delay, trigger = submit_delay_secs(cfg), submit_trigger(cfg)
    return [r for r in eligible
            if not is_held(r, now, delay, trigger, db_path)]


async def flush(db_path: str | None = None, release_held: bool = False) -> dict:
    """Submit one batch of pending scrobbles.

    Returns a summary the status endpoint reports verbatim. Rows are marked
    scrobbled whenever Last.fm *accepted the request*, including ones it chose
    to ignore — an ignored scrobble (Last.fm dislikes the artist name, the
    timestamp is duplicated) will be ignored again next time, so retrying it
    forever would just be a slow leak. Only transport and service errors leave
    rows pending.
    """
    cfg = settings()
    if not is_active(cfg):
        return {"ok": True, "submitted": 0, "detail": "not connected"}
    api_key, api_secret = credentials(cfg)

    batch = pending(db_path=db_path, include_held=release_held)
    if not batch:
        held = len(pending(db_path=db_path, include_held=True))
        if held:
            mins = submit_delay_secs(cfg) // 60
            after = "the album ends" if submit_trigger(cfg) == "album" else "each track ends"
            return {"ok": True, "submitted": 0, "held": held,
                    "detail": f"{held} held until {mins} min after {after}"}
        return {"ok": True, "submitted": 0, "detail": "nothing pending"}

    now = int(time.time())
    stale = [p for p in batch if is_too_old(p, now)]
    fresh = [p for p in batch if not is_too_old(p, now)]
    if stale:
        # Retire them: Last.fm will never take these, and leaving them pending
        # means re-reading them on every flush for the rest of time.
        await asyncio.to_thread(
            play_history.mark_scrobbled, [p["id"] for p in stale], now, db_path)
        log.info("last.fm: retired %d scrobble(s) older than 14 days", len(stale))
    if not fresh:
        return {"ok": True, "submitted": 0, "detail": f"retired {len(stale)} stale"}

    params = build_scrobble_params(fresh)
    params.update({"method": "track.scrobble",
                   "api_key": api_key, "sk": cfg["Session_Key"]})
    body, err = await _api_post(signed(params, api_secret))
    if err:
        log.warning("last.fm scrobble failed, %d still pending: %s", len(fresh), err)
        return {"ok": False, "submitted": 0, "detail": err}

    code, message = _error_of(body)
    if code == ERROR_INVALID_SESSION:
        # The user has to act; stop trying until they do.
        _store(Enabled=False)
        log.error("last.fm session rejected — scrobbling disabled until reconnected")
        return {"ok": False, "submitted": 0, "needs_reauth": True, "detail": message}
    if code is not None:
        retryable = code in RETRYABLE_ERRORS
        log.warning("last.fm error %s (%s): %s",
                    code, "will retry" if retryable else "giving up", message)
        if retryable:
            return {"ok": False, "submitted": 0, "detail": message}
        await asyncio.to_thread(
            play_history.mark_scrobbled, [p["id"] for p in fresh], now, db_path)
        return {"ok": False, "submitted": 0, "detail": message}

    accepted, ignored = read_scrobble_result(body)
    await asyncio.to_thread(
        play_history.mark_scrobbled, [p["id"] for p in fresh], now, db_path)
    if ignored:
        log.info("last.fm accepted %d, ignored %d", accepted, ignored)
    return {"ok": True, "submitted": accepted, "ignored": ignored,
            "detail": f"scrobbled {accepted}"}


async def flush_loop() -> None:
    """Background drain. Started from the FastAPI lifespan."""
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_SECS)
        try:
            await flush()
        except Exception as e:
            log.warning("last.fm flush sweep failed: %s", e)


def status(db_path: str | None = None) -> dict:
    """What the Settings page shows about the connection."""
    cfg = settings()
    api_key, api_secret = credentials(cfg)
    return {
        "connected": is_connected(cfg),
        "enabled": bool(cfg.get("Enabled")),
        "username": cfg.get("Username") or "",
        # Whether one-click is possible at all: false only on a build with no
        # built-in key where the user hasn't supplied one, which is the single
        # case the UI must demand the fields up front.
        "can_connect": bool(api_key and api_secret),
        "using_own_key": uses_own_credentials(cfg),
        "pending": len(pending(limit=1000, db_path=db_path)) if is_connected(cfg) else 0,
        # Finished but still inside the review window — releasable by hand.
        "held": (len(pending(limit=1000, db_path=db_path, include_held=True))
                 - len(pending(limit=1000, db_path=db_path))) if is_connected(cfg) else 0,
        "delay_mins": submit_delay_secs(cfg) // 60,
        "trigger": submit_trigger(cfg),
    }
