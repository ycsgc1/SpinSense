import asyncio
import io
import json
import logging
import os
import time
from typing import TYPE_CHECKING

import play_history

if TYPE_CHECKING:
    from fastapi import WebSocket

log = logging.getLogger(__name__)

ART_DIR = os.path.join(play_history.DATA_DIR, "art")


DEFAULT_STATUS = {
    "engine_active": False,
    "status_msg": "stopped",
    "rms_level": 0.0,
    "track": {"title": "", "artist": "", "album": "", "art_url": ""},
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


async def _download_and_store_art(play_id: int, art_url: str) -> None:
    """Fire-and-forget: fetch art_url, scale to 64x64 JPEG, save under ART_DIR,
    update the SQLite row with the relative path. Errors are swallowed (the play
    row stays recorded; frontend renders the placeholder)."""
    # Late-imported so the rest of ipc_manager stays importable in a minimal
    # test/dev environment where aiohttp + Pillow aren't installed.
    import aiohttp
    from PIL import Image

    try:
        os.makedirs(ART_DIR, exist_ok=True)
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(art_url) as resp:
                if resp.status != 200:
                    log.warning("art fetch %s returned HTTP %s", art_url, resp.status)
                    return
                data = await resp.read()

        def _save() -> None:
            with Image.open(io.BytesIO(data)) as img:
                img = img.convert("RGB")
                img.thumbnail((64, 64))
                img.save(
                    os.path.join(ART_DIR, f"{play_id}.jpg"),
                    "JPEG",
                    quality=75,
                )

        await asyncio.to_thread(_save)
        await asyncio.to_thread(play_history.set_art_path, play_id, f"art/{play_id}.jpg")
    except Exception as e:
        log.warning("art download failed for play %s: %s", play_id, e)


async def _stamp_last_play_ended() -> None:
    global _last_play_id
    if _last_play_id is None:
        return
    try:
        await asyncio.to_thread(play_history.set_ended_at, _last_play_id, int(time.time()))
    except Exception as e:
        log.warning("failed to stamp ended_at for play %s: %s", _last_play_id, e)
    _last_play_id = None


async def _record_if_new(track: dict) -> None:
    """Record a new identification if the title differs from the last one we
    saved. On silence (empty title) reset the dedupe state so the next play is
    treated as new, and close the open play's ended_at."""
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
    art_url = track.get("art_url") or None
    isrc = track.get("isrc") or None
    genre = track.get("genre") or None
    release_year = track.get("release_year") or None
    duration_secs = track.get("duration_secs") or None

    # A different track is starting: the previous one just ended.
    await _stamp_last_play_ended()

    try:
        play_id = await asyncio.to_thread(
            play_history.record_play, title, artist, album, art_url,
            isrc=isrc, genre=genre, release_year=release_year,
            duration_secs=duration_secs,
        )
    except Exception as e:
        log.error("failed to record play %s - %s: %s", artist, title, e)
        return

    _last_recorded_key = key
    _last_play_id = play_id

    if art_url:
        _art_tasks.add(task := asyncio.create_task(_download_and_store_art(play_id, art_url)))
        task.add_done_callback(_art_tasks.discard)


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

        if payload.get("type") == "live_status":
            track = payload.get("payload", {}).get("track", {})
            await _record_if_new(track)

        await manager.broadcast(payload)
