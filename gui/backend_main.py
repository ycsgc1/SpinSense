import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import paho.mqtt.client as mqtt
from pydantic import ValidationError
import sounddevice as sd

import play_history
from config_manager import SpinSenseConfig, load_config, save_config
from ipc_manager import ART_DIR, manager, handle_uds_client

# Paths that the setup-wizard redirect must let through. Everything outside
# this list is gated when Setup_Wizard_State == "pending".
_SETUP_ALLOWED_PREFIXES = ("/setup", "/api/", "/static/", "/art/", "/ws/")


async def start_uds_listener():
    socket_path = '/tmp/spinsense.sock'
    if os.path.exists(socket_path):
        os.remove(socket_path)

    server = await asyncio.start_unix_server(handle_uds_client, path=socket_path)
    print(f"🎧 Now listening for Core Engine on {socket_path}")

    async with server:
        await server.serve_forever()


@asynccontextmanager
async def lifespan(app: FastAPI):
    play_history.init_db()
    os.makedirs(ART_DIR, exist_ok=True)
    task = asyncio.create_task(start_uds_listener())
    yield
    task.cancel()


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


# StaticFiles asserts these directories exist at construction time.
os.makedirs(ART_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/art", StaticFiles(directory=ART_DIR), name="art")
templates = Jinja2Templates(directory="templates")


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


@app.get("/api/recent")
async def get_recent(limit: int = 10):
    rows = await asyncio.to_thread(play_history.recent_plays, limit)
    return {"plays": rows}


@app.get("/api/plays")
async def get_plays(limit: int = 50, offset: int = 0):
    rows = await asyncio.to_thread(play_history.recent_plays, limit, offset)
    total = await asyncio.to_thread(play_history.count_plays)
    return {"plays": rows, "total": total}


# --- WebSocket ---

@app.websocket("/ws/live-status")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
