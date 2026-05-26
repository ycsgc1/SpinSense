import asyncio
import io
import json
import logging
import os
import random
from typing import TYPE_CHECKING

import play_history

if TYPE_CHECKING:
    from fastapi import WebSocket

log = logging.getLogger(__name__)

ART_DIR = os.path.join(play_history.DATA_DIR, "art")


class ConnectionManager:
    def __init__(self):
        self.active_connections: list["WebSocket"] = []

    async def connect(self, websocket: "WebSocket"):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: "WebSocket"):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass


manager = ConnectionManager()

# Module-level dedupe state: the title of the most recent play we wrote to
# SQLite. Reset to "" whenever the engine reports silence so the same record
# played twice in a row gets two rows.
_last_recorded_title: str = ""


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


async def _record_if_new(track: dict) -> None:
    """Record a new identification if the title differs from the last one we
    saved. On silence (empty title) reset the dedupe state so the next play is
    treated as new."""
    global _last_recorded_title
    title = (track or {}).get("title", "") or ""

    if title == "":
        _last_recorded_title = ""
        return

    if title == _last_recorded_title:
        return

    artist = track.get("artist", "") or ""
    album = track.get("album") or None
    art_url = track.get("art_url") or None

    try:
        play_id = await asyncio.to_thread(
            play_history.record_play, title, artist, album, art_url
        )
    except Exception as e:
        log.error("failed to record play %s - %s: %s", artist, title, e)
        return

    _last_recorded_title = title

    if art_url:
        asyncio.create_task(_download_and_store_art(play_id, art_url))


# --- The Mock Generator for UI Development ---
async def mock_core_engine_stream():
    while True:
        fake_rms = random.uniform(0.0, 0.1)
        payload = {
            "type": "live_status",
            "payload": {
                "rms_level": round(fake_rms, 4),
                "engine_active": True,
                "status_msg": "Listening (Mock Data)",
                "track": {"title": "", "artist": "", "album": "", "art_url": ""},
            },
        }
        await manager.broadcast(payload)
        await asyncio.sleep(0.2)


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
