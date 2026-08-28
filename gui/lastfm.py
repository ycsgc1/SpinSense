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

**Auth model.** Last.fm has no callback-free desktop flow that suits a LAN
appliance, so we use the web flow in two steps the user drives: we fetch a
request token and hand them a last.fm URL to approve it, then they come back and
we trade the approved token for a permanent session key. No public callback URL,
no inbound connection, nothing to expose. The user brings their own API key and
secret from last.fm/api/account/create — one account, theirs, rate-limited to
them.
"""
import asyncio
import hashlib
import logging
import time

import play_history
from config_manager import load_config, save_config

log = logging.getLogger(__name__)

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
    """Where the user goes to approve the request token."""
    return f"{AUTH_URL}?api_key={api_key}&token={token}"


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


def is_connected(cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else settings()
    return bool(cfg.get("API_Key") and cfg.get("API_Secret") and cfg.get("Session_Key"))


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
    params = {
        "method": "track.updateNowPlaying",
        "api_key": cfg["API_Key"],
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

    body, err = await _api_post(signed(params, cfg["API_Secret"]))
    if err:
        log.debug("last.fm now-playing failed: %s", err)
        return
    code, message = _error_of(body)
    if code is not None:
        log.warning("last.fm now-playing rejected (%s): %s", code, message)


def pending(limit: int = MAX_BATCH, db_path: str | None = None) -> list[dict]:
    """Eligible, unscrobbled plays, oldest first.

    Bounded below by `Scrobble_Since` — the moment the account was connected.
    Without it, connecting Last.fm after months of listening would dump the
    entire back catalogue at the API, most of it too old to be accepted anyway.
    """
    since = int(settings().get("Scrobble_Since") or 0)
    rows = play_history.scrobble_candidates(
        since=since, limit=limit, pending_only=True, db_path=db_path)
    return [r for r in rows if r["eligible"]]


async def flush(db_path: str | None = None) -> dict:
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

    batch = pending(db_path=db_path)
    if not batch:
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
                   "api_key": cfg["API_Key"], "sk": cfg["Session_Key"]})
    body, err = await _api_post(signed(params, cfg["API_Secret"]))
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
    return {
        "connected": is_connected(cfg),
        "enabled": bool(cfg.get("Enabled")),
        "username": cfg.get("Username") or "",
        "has_credentials": bool(cfg.get("API_Key") and cfg.get("API_Secret")),
        "pending": len(pending(limit=1000, db_path=db_path)) if is_connected(cfg) else 0,
    }
