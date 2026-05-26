import asyncio
import json
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import sounddevice as sd

from config_manager import load_config, save_config
from ipc_manager import manager, handle_uds_client

async def start_uds_listener():
    """Starts the Unix Domain Socket server."""
    socket_path = '/tmp/spinsense.sock'
    if os.path.exists(socket_path):
        os.remove(socket_path)
        
    server = await asyncio.start_unix_server(handle_uds_client, path=socket_path)
    print(f"🎧 Now listening for Core Engine on {socket_path}")
    
    async with server:
        await server.serve_forever()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Boot up the UDS listener in the background
    task = asyncio.create_task(start_uds_listener())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)

# Mount static files and templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# --- Routes ---

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

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
    """Returns mic devices as objects so the frontend JS can read them."""
    try:
        devices = sd.query_devices()
        # Create a list of dictionaries with a 'name' key
        mics = [{"name": d['name']} for d in devices if d['max_input_channels'] > 0]
        # Remove duplicates
        unique_mics = list({m['name']: m for m in mics}.values())
        return {"devices": unique_mics}
    except Exception as e:
        print(f"Error querying devices: {e}")
        return {"devices": []}

@app.post("/api/engine/start")
def start_engine():
    config = load_config()
    config["System"]["Auto_Start"] = True
    config["System"]["Engine_Status"] = "active"
    save_config(config)
    return {"status": "success"}

@app.post("/api/engine/stop")
def stop_engine():
    config = load_config()
    config["System"]["Auto_Start"] = False
    config["System"]["Engine_Status"] = "stopped"
    save_config(config)
    return {"status": "success"}

# --- The Missing WebSocket Route ---

@app.websocket("/ws/live-status")
async def websocket_endpoint(websocket: WebSocket):
    """Handles real-time WebSocket connection from the browser."""
    await manager.connect(websocket)
    try:
        while True:
            # Keep the connection alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)