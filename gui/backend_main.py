import asyncio
import json
import os
import urllib.parse
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import paho.mqtt.client as mqtt
from pydantic import ValidationError
import sounddevice as sd
import aiohttp

import play_history
import stats
import reconcile
from config_manager import SpinSenseConfig, load_config, save_config
from ipc_manager import ART_DIR, manager, handle_uds_client, spawn_art_download
from discovery import advertiser

# Paths that the setup-wizard redirect must let through. Everything outside
# this list is gated when Setup_Wizard_State == "pending".
_SETUP_ALLOWED_PREFIXES = ("/setup", "/api/", "/static/", "/art/", "/ws/")

CMD_SOCKET_PATH = '/tmp/spinsense-cmd.sock'


async def _send_cmd(payload: dict, timeout: float = 2.0) -> dict:
    """Open a short-lived connection to the engine's command socket, write
    one JSON line, read one JSON line, close. Returns the parsed reply.

    Raises FileNotFoundError if the socket doesn't exist, ConnectionRefusedError
    if the engine isn't listening, asyncio.TimeoutError on either side."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(CMD_SOCKET_PATH),
        timeout=timeout,
    )
    try:
        writer.write((json.dumps(payload) + '\n').encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        return json.loads(line.decode())
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def start_uds_listener():
    socket_path = '/tmp/spinsense.sock'
    if os.path.exists(socket_path):
        os.remove(socket_path)

    server = await asyncio.start_unix_server(handle_uds_client, path=socket_path)
    print(f"🎧 Now listening for Core Engine on {socket_path}")

    async with server:
        await server.serve_forever()


async def _purge_loop():
    """Reclaim art for scrobbles soft-deleted beyond the Undo grace window."""
    while True:
        await asyncio.sleep(1800)  # 30 min
        try:
            await asyncio.to_thread(play_history.purge_deleted)
        except Exception as e:
            print(f"⚠️ purge sweep failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    play_history.init_db()
    os.makedirs(ART_DIR, exist_ok=True)
    try:
        await asyncio.to_thread(play_history.purge_deleted)
    except Exception as e:
        print(f"⚠️ startup purge failed: {e}")
    task = asyncio.create_task(start_uds_listener())
    purge_task = asyncio.create_task(_purge_loop())
    try:
        await advertiser.start(load_config())
    except Exception as e:
        print(f"⚠️ mDNS advertiser failed to start: {e}")
    yield
    await advertiser.stop()
    task.cancel()
    purge_task.cancel()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def setup_wizard_gate(request: Request, call_next):
    """Redirect to /setup whenever Setup_Wizard_State is "pending" and the
    user is hitting a normal page route. API + static + the wizard itself are
    always allowed through."""
    path = request.url.path
    if not any(path.startswith(p) for p in _SETUP_ALLOWED_PREFIXES):
        try:
            cfg = load_config()
            state = cfg.get("System", {}).get("Setup_Wizard_State", "pending")
        except Exception:
            state = "pending"
        if state == "pending":
            return RedirectResponse(url="/setup", status_code=307)
    return await call_next(request)


@app.middleware("http")
async def no_cache_app_assets(request: Request, call_next):
    """Force the browser to revalidate the app's own HTML pages and static
    JS/CSS, so a rebuild can never serve a stale asset against fresh markup
    (the 'works in incognito but not my normal browser' class of bug). ETag /
    Last-Modified still produce cheap 304s; /art and /api are left cacheable."""
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if request.url.path.startswith("/static/") or ctype.startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# StaticFiles asserts these directories exist at construction time.
os.makedirs(ART_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/art", StaticFiles(directory=ART_DIR), name="art")
templates = Jinja2Templates(directory="templates")

# Version-stamp static asset URLs so each release busts browser/proxy caches
# (kills the 'new HTML, stale CSS/JS' class of bug). VERSION lives at repo root.
try:
    with open(os.path.join(os.path.dirname(__file__), "..", "VERSION")) as _vf:
        ASSET_VERSION = _vf.read().strip() or "dev"
except Exception:
    ASSET_VERSION = "dev"
templates.env.globals["asset_v"] = ASSET_VERSION


# --- Page routes ---

@app.get("/")
async def dashboard(request: Request):
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "current_page": "dashboard"}
    )


@app.get("/history")
async def history(request: Request):
    return templates.TemplateResponse(
        "history.html", {"request": request, "current_page": "history"}
    )


@app.get("/settings")
async def settings(request: Request):
    return templates.TemplateResponse(
        "settings.html", {"request": request, "current_page": "settings"}
    )


@app.get("/stats")
async def stats_page(request: Request):
    return templates.TemplateResponse(
        "stats.html", {"request": request, "current_page": "stats"}
    )


@app.get("/setup")
async def setup(request: Request):
    return templates.TemplateResponse(
        "setup.html", {"request": request, "current_page": "setup"}
    )


# --- API routes ---

@app.get("/api/config")
def get_config():
    return load_config()


@app.post("/api/config")
async def update_config(request: Request):
    new_config = await request.json()
    try:
        SpinSenseConfig(**new_config)
    except ValidationError as e:
        errs = e.errors()
        first = errs[0] if errs else {}
        loc = ".".join(str(p) for p in first.get("loc", []))
        msg = first.get("msg", "Validation failed")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": f"{loc}: {msg}" if loc else msg},
        )
    if not save_config(new_config):
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": "Failed to write config.json"},
        )
    try:
        await advertiser.reconcile(new_config)
    except Exception as e:
        print(f"⚠️ mDNS reconcile after config save failed: {e}")
    return {"status": "success"}


@app.get("/api/devices")
def get_audio_devices():
    try:
        devices = sd.query_devices()
        mics = [{"name": d['name']} for d in devices if d['max_input_channels'] > 0]
        unique_mics = list({m['name']: m for m in mics}.values())
        return {"devices": unique_mics}
    except Exception as e:
        print(f"Error querying devices: {e}")
        return {"devices": []}


@app.get("/api/setup-state")
def get_setup_state():
    cfg = load_config()
    return {"state": cfg.get("System", {}).get("Setup_Wizard_State", "pending")}


def _try_mqtt_connect(host: str, port: int, user: str, password: str) -> tuple[bool, str]:
    """Open a short-lived paho client, attempt connect, close. Returns
    (ok, detail). Kept synchronous; the caller wraps it in to_thread so the
    socket-level timeout doesn't block the event loop."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if user and password:
        client.username_pw_set(user, password)
    try:
        client.connect(host, port, keepalive=5)
        client.disconnect()
        return True, "Connected"
    except Exception as e:
        return False, str(e) or e.__class__.__name__


@app.post("/api/mqtt/test")
async def test_mqtt(request: Request):
    body = await request.json()
    host = str(body.get("host", "") or "")
    port = body.get("port", 1883)
    user = str(body.get("user", "") or "")
    password = str(body.get("password", "") or "")
    try:
        port = int(port)
    except (TypeError, ValueError):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "detail": "Port must be an integer"},
        )
    if not host:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "detail": "Host is required"},
        )
    try:
        ok, detail = await asyncio.wait_for(
            asyncio.to_thread(_try_mqtt_connect, host, port, user, password),
            timeout=3.5,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "detail": f"Timed out connecting to {host}:{port}"}
    if ok:
        return {"ok": True, "detail": detail}
    return {"ok": False, "detail": detail}


@app.post("/api/calibrate/start")
async def calibrate_start(request: Request):
    body = await request.json()
    phase = body.get("phase")
    if phase not in ("noise_floor", "music"):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "detail": f"invalid phase: {phase!r}"},
        )
    try:
        reply = await _send_cmd({"cmd": "start_calibration", "phase": phase})
    except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError):
        return JSONResponse(
            status_code=503,
            content={"ok": False, "detail": "Engine not reachable"},
        )
    return reply


@app.get("/api/calibrate/status")
async def calibrate_status():
    try:
        reply = await _send_cmd({"cmd": "get_calibration"})
    except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError):
        return JSONResponse(
            status_code=503,
            content={"status": "none", "samples_count": 0, "stats": None, "detail": "Engine not reachable"},
        )
    return reply


@app.post("/api/calibrate/clear")
async def calibrate_clear():
    try:
        reply = await _send_cmd({"cmd": "clear_calibration"})
    except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError):
        return JSONResponse(
            status_code=503,
            content={"ok": False, "detail": "Engine not reachable"},
        )
    return reply


@app.post("/api/rescan")
async def rescan():
    try:
        reply = await _send_cmd({"cmd": "rescan"})
    except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError):
        return JSONResponse(
            status_code=503,
            content={"ok": False, "detail": "Engine not reachable"},
        )
    return reply


@app.get("/api/recent")
async def get_recent(limit: int = 10):
    rows = await asyncio.to_thread(play_history.recent_plays, limit)
    return {"plays": rows}


@app.get("/api/plays")
async def get_plays(limit: int = 50, offset: int = 0):
    rows = await asyncio.to_thread(play_history.recent_plays, limit, offset)
    total = await asyncio.to_thread(play_history.count_plays)
    return {"plays": rows, "total": total}


@app.delete("/api/plays/{play_id}")
async def delete_play_route(play_id: int):
    ok = await asyncio.to_thread(play_history.delete_play, play_id)
    if not ok:
        return JSONResponse(status_code=404, content={"detail": "not found"})
    return {"status": "deleted", "id": play_id}


@app.post("/api/plays/{play_id}/restore")
async def restore_play_route(play_id: int):
    ok = await asyncio.to_thread(play_history.restore_play, play_id)
    if not ok:
        return JSONResponse(status_code=404, content={"detail": "not found"})
    return {"status": "restored", "id": play_id}


async def _itunes_album_candidates(artist: str, title: str) -> list[dict]:
    """Distinct candidate albums for a track from the iTunes Search API.
    Isolated so tests can stub it; any error is an empty list."""
    query = urllib.parse.quote_plus(f"{artist} {title}")
    url = f"https://itunes.apple.com/search?term={query}&entity=song&limit=25"
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
    except Exception as e:
        print(f"⚠️ iTunes candidates lookup failed: {e}")
        return []
    out, seen = [], set()
    for r in data.get("results", []):
        album = r.get("collectionName")
        if not album or album in seen:
            continue
        seen.add(album)
        art = (r.get("artworkUrl100") or "").replace("100x100bb", "1000x1000bb")
        out.append({"album": album, "art_url": art or None})
        if len(out) >= 10:
            break
    return out


@app.get("/api/plays/{play_id}/album-candidates")
async def album_candidates(play_id: int):
    play = await asyncio.to_thread(play_history.get_play, play_id)
    if play is None:
        return JSONResponse(status_code=404, content={"detail": "not found"})
    candidates = await _itunes_album_candidates(play["artist"], play["title"])
    return {"current": play["album"], "candidates": candidates}


@app.post("/api/plays/{play_id}/album")
async def set_album_route(play_id: int, request: Request):
    body = await request.json()
    album = str(body.get("album") or "").strip()
    art_url = body.get("art_url") or None
    if not album:
        return JSONResponse(status_code=400, content={"detail": "album is required"})
    if body.get("apply_to_run"):
        ids = await asyncio.to_thread(reconcile.apply_album_to_run, play_id, album)
        if not ids:
            return JSONResponse(status_code=404, content={"detail": "not found"})
    else:
        ok = await asyncio.to_thread(play_history.set_album, play_id, album)
        if not ok:
            return JSONResponse(status_code=404, content={"detail": "not found"})
        ids = [play_id]
    if art_url:
        for pid in ids:
            spawn_art_download(pid, art_url)
    return {"status": "ok", "updated": len(ids)}


@app.get("/api/stats")
async def get_stats(period: str = "month", year: int | None = None,
                    month: int | None = None):
    try:
        return await asyncio.to_thread(stats.compute_stats, period, year, month)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})


@app.get("/api/status")
def get_status():
    """Last-known engine status, in the shape the Home Assistant integration
    polls. Defaults to a 'stopped' payload when the engine hasn't reported."""
    return manager.last_status


# --- WebSocket ---

@app.websocket("/ws/live-status")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
