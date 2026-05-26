import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import sounddevice as sd

import play_history
from config_manager import load_config, save_config
from ipc_manager import ART_DIR, manager, handle_uds_client


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
    save_config(new_config)
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


@app.get("/api/recent")
async def get_recent(limit: int = 10):
    rows = await asyncio.to_thread(play_history.recent_plays, limit)
    return {"plays": rows}


# --- WebSocket ---

@app.websocket("/ws/live-status")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
