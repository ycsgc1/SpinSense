import json
import os
from pydantic import BaseModel

# Path points to the root directory, one level up from /gui/
CONFIG_PATH = os.path.join(os.path.dirname(__file__), '..', 'config.json')

# --- Pydantic Models for Strict Type Validation ---
class SystemConfig(BaseModel):
    Auto_Start: bool = False
    Engine_Status: str = "stopped"

class HardwareConfig(BaseModel):
    Mic_Device: str = "default"

class AudioConfig(BaseModel):
    Volume_Threshold: float = 0.0062
    Song_Sample_Length: float = 10.0
    New_Song_Silence_Interval: float = 10.0
    Stopped_Silence_Interval: float = 30.0

class MQTTBrokerConfig(BaseModel):
    Host: str = "127.0.0.1"
    Port: int = 1883
    User: str = ""
    Password: str = ""

class MQTTDiscoveryConfig(BaseModel):
    Enabled: bool = True
    Discovery_Topic: str = "homeassistant/media_player/spin_sense/config"

class MQTTTopicsConfig(BaseModel):
    State: str = "home/vinyl/state"
    Title: str = "home/vinyl/title"
    Artist: str = "home/vinyl/artist"
    Album_Art: str = "home/vinyl/album_art"

class MQTTConfig(BaseModel):
    Broker: MQTTBrokerConfig = MQTTBrokerConfig()
    Discovery: MQTTDiscoveryConfig = MQTTDiscoveryConfig()
    Topics: MQTTTopicsConfig = MQTTTopicsConfig()

class SpinSenseConfig(BaseModel):
    System: SystemConfig = SystemConfig()
    Hardware: HardwareConfig = HardwareConfig()
    Audio: AudioConfig = AudioConfig()
    MQTT: MQTTConfig = MQTTConfig()

# --- Core Functions ---
def get_default_config() -> dict:
    """Returns the default configuration as a dictionary."""
    return SpinSenseConfig().model_dump()

def load_config() -> dict:
    """Loads config.json. Recreates it with defaults if missing or invalid."""
    if not os.path.exists(CONFIG_PATH):
        save_config(get_default_config())
    
    try:
        with open(CONFIG_PATH, 'r') as f:
            data = json.load(f)
            # Passing data to SpinSenseConfig validates the types automatically
            validated = SpinSenseConfig(**data)
            return validated.model_dump()
    except Exception as e:
        print(f"⚠️ Error loading config, regenerating defaults: {e}")
        defaults = get_default_config()
        save_config(defaults)
        return defaults

def save_config(data: dict) -> bool:
    """Validates and saves a dictionary to config.json."""
    try:
        validated = SpinSenseConfig(**data)
        with open(CONFIG_PATH, 'w') as f:
            json.dump(validated.model_dump(), f, indent=2)
        return True
    except Exception as e:
        print(f"❌ Error saving config (Validation failed): {e}")
        return False