import asyncio
import collections
import hashlib
import io
import json
import logging
import os
import time
from typing import TYPE_CHECKING

import lastfm
import play_history
import reconcile

if TYPE_CHECKING:
    from fastapi import WebSocket

log = logging.getLogger(__name__)

ART_DIR = os.path.join(play_history.DATA_DIR, "art")


def _data_root() -> str:
    """The directory `art_path` values are relative to.

    Derived from ART_DIR rather than read independently, so the two cannot
    disagree about where artwork lives.
    """
    return os.path.dirname(ART_DIR)


# Stands in for a real frame on /api/status until the engine reports, so it
# must carry every key a frame does — consumers shouldn't need two code paths.
DEFAULT_STATUS = {
    "engine_active": False,
    "status_msg": "stopped",
    "phase": "listening",
    "rms_level": 0.0,
    "track": {"title": "", "artist": "", "album": "", "art_url": ""},
    "play_clock": None,
}


class ConnectionManager:
    def __init__(self):
        self.active_connections: list["WebSocket"] = []
        self.last_status: dict = dict(DEFAULT_STATUS)

    async def connect(self, websocket: "WebSocket"):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: "WebSocket"):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        if message.get("type") == "live_status" and isinstance(message.get("payload"), dict):
            self.last_status = message["payload"]
        dead = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                # Send failed (socket closed/broken). Drop it after the loop so
                # it isn't retried on every future frame.
                dead.append(connection)
        for connection in dead:
            self.disconnect(connection)


manager = ConnectionManager()

# Recent engine diagnostics, newest last. The engine's print() output only
# reaches `docker logs`, which needs shell access on the host — so the things
# worth noticing (a stalled input, a recognition given up on) were invisible
# from the web UI. In memory on purpose: this is for "what just happened",
# not an audit trail, and it must never grow without bound or outlive a
# restart in a way that misleads.
EVENT_LIMIT = 200
events: collections.deque = collections.deque(maxlen=EVENT_LIMIT)


def record_event(payload: dict) -> None:
    """File one engine event. Tolerates anything, since it crosses a socket."""
    if not isinstance(payload, dict):
        return
    message = str(payload.get("message") or "").strip()
    if not message:
        return
    level = str(payload.get("level") or "info").lower()
    if level not in ("info", "warning", "error"):
        level = "info"
    ts = payload.get("ts")
    events.append({
        "ts": int(ts) if isinstance(ts, (int, float)) else int(time.time()),
        "level": level,
        "message": message[:500],
    })

# Module-level dedupe state: the (artist, title) of the most recent play we
# wrote to SQLite. Reset to None whenever the engine reports silence so the same
# record played twice in a row gets two rows. Keyed on artist+title (not title
# alone) so two different songs that share a title aren't collapsed into one row.
_last_recorded_key: tuple[str, str] | None = None

# The row id of the most recent play we recorded, still "open" (no ended_at).
# Stamped when the next track starts or the engine reports silence; a GUI
# restart mid-play simply leaves the row's ended_at NULL (excluded from
# listening-time stats — never estimated).
_last_play_id: int | None = None

# Strong refs to in-flight art-download tasks so the event loop's weak task
# tracking can't GC them mid-download; each removes itself on completion.
_art_tasks: set = set()

# Same, for the fire-and-forget Last.fm "now playing" ping.
_now_playing_tasks: set = set()


def _spawn_now_playing(track: dict) -> None:
    """Tell Last.fm what's on the platter, without making recording wait for it.
    Purely decorative — the scrobble itself happens later, from the queue."""
    task = asyncio.create_task(lastfm.update_now_playing(track))
    _now_playing_tasks.add(task)
    task.add_done_callback(_now_playing_tasks.discard)


def _thumbnail(data: bytes) -> bytes:
    """Full-size artwork bytes -> the 64x64 JPEG we actually store."""
    import io as _io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        img.thumbnail((64, 64))
        out = _io.BytesIO()
        img.save(out, "JPEG", quality=75)
        return out.getvalue()


async def _fetch_art(art_url: str) -> bytes | None:
    import aiohttp

    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(art_url) as resp:
                if resp.status != 200:
                    log.warning("art fetch %s returned HTTP %s", art_url, resp.status)
                    return None
                return await resp.read()
    except Exception as e:
        log.warning("art fetch failed for %s: %s", art_url, e)
        return None


def art_filename(play_id: int, data: bytes) -> str:
    """Content-addressed name for a play's artwork.

    Artwork used to live at a fixed `art/{id}.jpg` and be rewritten in place
    when a play's album was corrected — a stable URL with changing bytes, which
    is unserveable through any cache. Adding `?v=` to the URL fixed it in the
    browser and not in the reverse proxy in front of it, since some caches key
    on the path alone and some rewrite `expires` for image extensions
    regardless of what we send.

    A filename derived from the bytes sidesteps every one of those: new
    artwork is simply a different URL, so nothing anywhere can serve the old
    one, and correct caching stays possible instead of being fought.
    """
    return f"{play_id}-{hashlib.sha256(data).hexdigest()[:8]}.jpg"


def _replace_art_file(play_id: int, data: bytes) -> str:
    """Write artwork under its content name, point the row at it, and unlink
    whatever the row referenced before. Returns the new relative path."""
    os.makedirs(ART_DIR, exist_ok=True)
    previous = None
    try:
        row = play_history.get_play(play_id)
        previous = (row or {}).get("art_path")
    except Exception:
        pass

    name = art_filename(play_id, data)
    rel = f"art/{name}"
    with open(os.path.join(ART_DIR, name), "wb") as fh:
        fh.write(data)
    play_history.set_art_path(play_id, rel)

    if previous and previous != rel:
        # The old file is unreferenced now. purge_deleted() only cleans art for
        # deleted rows, so without this every album correction leaks a file.
        try:
            os.remove(os.path.join(_data_root(), previous))
        except OSError:
            pass
    return rel


async def _download_and_store_art(play_id: int, art_url: str) -> None:
    """Fire-and-forget: fetch art_url, thumbnail it, store it under a
    content-addressed name, and point the row at it. Errors are swallowed —
    the play stays recorded and the frontend renders the placeholder."""
    raw = await _fetch_art(art_url)
    if raw is None:
        return
    try:
        thumb = await asyncio.to_thread(_thumbnail, raw)
        await asyncio.to_thread(_replace_art_file, play_id, thumb)
    except Exception as e:
        log.warning("art store failed for play %s: %s", play_id, e)


def spawn_art_download(play_id: int, art_url: str) -> None:
    """create_task + strong ref until done. Used by the UDS record path, where
    a play is being written and nothing is waiting on the artwork."""
    _art_tasks.add(task := asyncio.create_task(_download_and_store_art(play_id, art_url)))
    task.add_done_callback(_art_tasks.discard)


async def unify_art(play_ids: list[int], source_play_id: int,
                    art_url: str | None = None) -> list[int]:
    """Give every play in `play_ids` the same artwork. Returns the ids changed.

    Unlike the record path this is awaited, because a person clicked a button
    and the response should not claim success before the files exist.

    Two sources, in order. If `art_url` is given — the user picked a specific
    album — it is fetched and thumbnailed **once** and the same bytes written to
    every play; the previous code spawned an independent download per row, so a
    ten-play run pulled the same 1000x1000 image ten times. If no URL is given —
    the album was typed by hand, or the chosen candidate had no artwork — the
    run is unified onto whatever the edited play already has, since making the
    run look like the row you were editing is what "apply to the whole session"
    means.
    """
    thumb: bytes | None = None
    if art_url:
        raw = await _fetch_art(art_url)
        if raw is not None:
            try:
                thumb = await asyncio.to_thread(_thumbnail, raw)
            except Exception as e:
                log.warning("could not thumbnail %s: %s", art_url, e)
    if thumb is None:
        thumb = await asyncio.to_thread(_read_stored_art, source_play_id)
    if thumb is None:
        return []

    def _write_all() -> list[int]:
        written = []
        for pid in play_ids:
            try:
                _replace_art_file(pid, thumb)
            except OSError as e:
                log.warning("could not write art for play %s: %s", pid, e)
                continue
            written.append(pid)
        return written

    return await asyncio.to_thread(_write_all)


def _read_stored_art(play_id: int) -> bytes | None:
    """The artwork a play currently shows, read via its recorded path.

    Must go through art_path rather than guessing a filename: names are
    content-addressed now, so there is nothing to guess.
    """
    try:
        row = play_history.get_play(play_id)
        rel = (row or {}).get("art_path")
        if not rel:
            return None
        with open(os.path.join(_data_root(), rel), "rb") as fh:
            return fh.read()
    except OSError:
        return None


# How long after a play was filed a manual rescan may still replace it.
#
# A rescan means "that is not what is playing", and early on the reason is
# almost always a misfire: the needle drop triggered a scan of the lead-in
# groove and the recognizer answered from the half second of music at the end
# of it. Replacing that play is right. Later, a rescan means something else —
# the engine sat through a transition it never heard — and the play being
# corrected is a real one that really did play, so it must survive.
#
# The window is therefore shorter than any track that could plausibly have
# finished inside it, interludes included.
SUPERSEDE_WINDOW_SECS = 90


async def _supersede_last_play() -> bool:
    """Drop the open play instead of closing it. True if one was dropped.

    Soft-deletes, so the row is recoverable for the same grace period as a
    delete from the history page rather than being destroyed on a judgement
    call. Never touches a play already scrobbled: Last.fm has no API to take
    one back, so a submitted play is history whether or not it was right.
    """
    global _last_play_id
    if _last_play_id is None:
        return False
    play_id = _last_play_id
    try:
        row = await asyncio.to_thread(play_history.get_play, play_id)
    except Exception as e:
        log.warning("could not read play %s to supersede it: %s", play_id, e)
        return False
    if not row or row.get("scrobbled_at") is not None:
        return False
    age = int(time.time()) - int(row.get("played_at") or 0)
    if age > SUPERSEDE_WINDOW_SECS:
        return False
    try:
        dropped = await asyncio.to_thread(play_history.delete_play, play_id)
    except Exception as e:
        log.warning("could not supersede play %s: %s", play_id, e)
        return False
    if not dropped:
        return False
    log.info("rescan superseded play %s (%s - %s) after %ss",
             play_id, row.get("artist"), row.get("title"), age)
    record_event({
        "level": "info",
        "message": (f"Rescan replaced {row.get('artist')} - {row.get('title')}, "
                    f"filed {age}s earlier"),
    })
    _last_play_id = None
    return True


async def _stamp_last_play_ended() -> None:
    global _last_play_id
    if _last_play_id is None:
        return
    try:
        await asyncio.to_thread(play_history.set_ended_at, _last_play_id, int(time.time()))
    except Exception as e:
        log.warning("failed to stamp ended_at for play %s: %s", _last_play_id, e)
    _last_play_id = None


def _play_clock_fields(play_clock) -> tuple[int | None, int | None]:
    """(started_at, join_offset_secs) from a frame's play_clock block.

    The block is optional and every field in it is best-effort — an engine
    running an older build sends no block at all, and a track with no duration
    metadata sends one with nulls. Anything unusable becomes NULL in the row,
    which the scrobble ledger reads as "unknown" rather than "zero"."""
    if not isinstance(play_clock, dict):
        return None, None
    started_at = play_clock.get("started_at")
    join_offset = play_clock.get("join_offset_secs")
    started_at = int(started_at) if isinstance(started_at, (int, float)) else None
    join_offset = int(join_offset) if isinstance(join_offset, (int, float)) else None
    return started_at, join_offset


async def _record_if_new(track: dict, play_clock: dict | None = None,
                         supersede: bool = False) -> None:
    """Record a new identification if the title differs from the last one we
    saved. On silence (empty title) reset the dedupe state so the next play is
    treated as new, and close the open play's ended_at.

    `supersede` is the engine reporting that this track came from a manual
    rescan and corrects the open play rather than following it."""
    global _last_recorded_key, _last_play_id
    title = (track or {}).get("title", "") or ""

    if title == "":
        await _stamp_last_play_ended()
        _last_recorded_key = None
        return

    artist = track.get("artist", "") or ""
    key = (artist, title)
    if key == _last_recorded_key:
        return

    album = track.get("album") or None
    if album in (None, play_history.UNKNOWN_ALBUM):
        # The listener's own history is a better oracle than a relevance-ranked
        # search: a vinyl collection is small and repetitive, so a track we have
        # filed before almost certainly belongs to the same record again — and
        # if they only own the deluxe, that is what their history says.
        remembered = await asyncio.to_thread(
            play_history.album_for_track, artist, title)
        if remembered:
            log.info("album for %s - %s recalled from history: %s",
                     artist, title, remembered)
            album = remembered
    art_url = track.get("art_url") or None
    isrc = track.get("isrc") or None
    genre = track.get("genre") or None
    release_year = track.get("release_year") or None
    duration_secs = track.get("duration_secs") or None
    album_exclusive = bool(track.get("album_exclusive"))
    started_at, join_offset_secs = _play_clock_fields(play_clock)

    # A different track is starting: the previous one just ended — unless a
    # rescan says it never really started, in which case it is dropped instead.
    if not (supersede and await _supersede_last_play()):
        await _stamp_last_play_ended()

    try:
        play_id = await asyncio.to_thread(
            play_history.record_play, title, artist, album, art_url,
            isrc=isrc, genre=genre, release_year=release_year,
            duration_secs=duration_secs, started_at=started_at,
            join_offset_secs=join_offset_secs, album_exclusive=album_exclusive,
        )
    except Exception as e:
        log.error("failed to record play %s - %s: %s", artist, title, e)
        return

    _last_recorded_key = key
    _last_play_id = play_id

    if art_url:
        spawn_art_download(play_id, art_url)

    _spawn_now_playing(track)

    # Unify edition variants across this play's session run. Best-effort:
    # a reconcile failure must never block or crash recording.
    try:
        await asyncio.to_thread(reconcile.reconcile_album, play_id)
    except Exception as e:
        log.warning("album reconcile failed for play %s: %s", play_id, e)


# --- The Real Unix Domain Socket Listener ---
async def handle_uds_client(reader, writer):
    """Reads real data from the Core engine via /tmp/spinsense.sock. Each line
    is one live_status frame. New identifications get persisted to SQLite and
    spawn a background art-download task."""
    while True:
        data = await reader.readline()
        if not data:
            break
        try:
            payload = json.loads(data.decode())
        except json.JSONDecodeError:
            continue

        if payload.get("type") == "event":
            record_event(payload.get("payload", {}))
            continue   # not a status frame; nothing to broadcast or persist

        if payload.get("type") == "live_status":
            body = payload.get("payload", {})
            await _record_if_new(
                body.get("track", {}), body.get("play_clock"),
                supersede=bool(body.get("supersedes_previous")),
            )

        await manager.broadcast(payload)
