import json
import os
from typing import Literal
from pydantic import BaseModel

# Resolve config folder dynamically using the environment variable SPINSENSE_DATA_DIR
DATA_DIR = os.environ.get('SPINSENSE_DATA_DIR', os.path.join(os.path.dirname(__file__), '..'))
CONFIG_PATH = os.path.join(DATA_DIR, 'config.json')

# --- Pydantic Models for Strict Type Validation ---
class SystemConfig(BaseModel):
    Auto_Start: bool = False
    Setup_Wizard_State: Literal["pending", "skipped", "completed"] = "pending"

class HardwareConfig(BaseModel):
    Mic_Device: str = "default"

class AudioConfig(BaseModel):
    # Defaults must match core/core_engine.py DEFAULT_CONFIG["Audio"].
    Volume_Threshold: float = 0.01
    Song_Sample_Length: float = 5.0
    New_Song_Silence_Interval: float = 3.0
    Stopped_Silence_Interval: float = 5.0
    Rescan_Wait_Interval: float = 5.0
    Retrigger_On_Track_Change: bool = False
    Fallback_Provider: Literal["none", "audd", "acoustid"] = "none"
    AudD_API_Token: str = ""

class MQTTBrokerConfig(BaseModel):
    Host: str = "127.0.0.1"
    Port: int = 1883
    User: str = ""
    Password: str = ""

class MQTTTopicsConfig(BaseModel):
    State: str = "home/vinyl/state"
    Title: str = "home/vinyl/title"
    Artist: str = "home/vinyl/artist"
    Album_Art: str = "home/vinyl/album_art"

class MQTTConfig(BaseModel):
    Enabled: bool = False
    Broker: MQTTBrokerConfig = MQTTBrokerConfig()
    Topics: MQTTTopicsConfig = MQTTTopicsConfig()

class MDNSConfig(BaseModel):
    Enabled: bool = True
    Service_Name: str = ""  # empty => derive from hostname at runtime

class DiscoveryConfig(BaseModel):
    mDNS: MDNSConfig = MDNSConfig()

class SpinSenseConfig(BaseModel):
    System: SystemConfig = SystemConfig()
    Hardware: HardwareConfig = HardwareConfig()
    Audio: AudioConfig = AudioConfig()
    MQTT: MQTTConfig = MQTTConfig()
    Discovery: DiscoveryConfig = DiscoveryConfig()

# --- Core Functions ---
def get_default_config() -> dict:
    """Returns the default configuration as a dictionary."""
    return SpinSenseConfig().dict()

def load_config() -> dict:
    """Loads config.json. Recreates it with defaults if missing or invalid."""
    if not os.path.exists(CONFIG_PATH):
        save_config(get_default_config())
    
    try:
        with open(CONFIG_PATH, 'r') as f:
            data = json.load(f)
            # Passing data to SpinSenseConfig validates the types automatically
            validated = SpinSenseConfig(**data)
            return validated.dict()
    except Exception as e:
        print(f"⚠️ Error loading config, regenerating defaults: {e}")
        defaults = get_default_config()
        save_config(defaults)
        return defaults

def save_config(data: dict) -> bool:
    """Validates and saves a dictionary to config.json."""
    try:
        validated = SpinSenseConfig(**data)
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, 'w') as f:
            json.dump(validated.dict(), f, indent=2)
        return True
    except Exception as e:
        print(f"❌ Error saving config (Validation failed): {e}")
        return False