from fastapi import FastAPI, HTTPException
import uvicorn
from config_manager import load_config, save_config
from audio_utils import get_audio_devices

app = FastAPI(title="SpinSense Web GUI API")

@app.get("/api/health")
def health_check():
    """Simple check to ensure the backend is alive."""
    return {"status": "ok", "message": "SpinSense backend is running."}

@app.get("/api/config")
def get_config():
    """Returns the current contents of config.json."""
    return load_config()

@app.post("/api/config")
def update_config(new_config: dict):
    """Updates the config.json with new values."""
    success = save_config(new_config)
    if not success:
        raise HTTPException(status_code=400, detail="Invalid configuration format or save error.")
    return {"status": "success", "message": "Configuration updated."}

@app.get("/api/devices")
def list_devices():
    """Returns the list of discovered audio input devices."""
    return {"devices": get_audio_devices()}

if __name__ == "__main__":
    # Runs the server on port 5000
    uvicorn.run("backend_main:app", host="0.0.0.0", port=5000, reload=True)