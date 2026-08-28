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
    # Track-end prediction (see core/track_clock.py). Grace is the flat floor
    # of the window allowed past a track's predicted end; the engine takes the
    # larger of it and 10% of the track length.
    Track_End_Detection: bool = True
    Track_End_Grace_Secs: float = 20.0
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

class LastFMConfig(BaseModel):
    """Scrobbling credentials. The user registers their own API application at
    last.fm/api/account/create, so the rate limit and the terms are theirs.

    Session_Key is obtained by the two-step auth flow in gui/lastfm.py and is
    permanent until revoked. Like the MQTT password and the AudD token it is
    stored in plaintext — fine for a self-hosted LAN box, worth knowing before
    committing config.json anywhere.

    Scrobble_Since is stamped when the account is connected: only plays after
    that moment are ever submitted, so connecting an account doesn't dump months
    of back catalogue at the API.
    """
    Enabled: bool = False
    API_Key: str = ""
    API_Secret: str = ""
    Session_Key: str = ""
    Username: str = ""
    Scrobble_Now_Playing: bool = True
    Scrobble_Since: int = 0


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
    LastFM: LastFMConfig = LastFMConfig()
    Discovery: DiscoveryConfig = DiscoveryConfig()

# --- Core Functions ---
def get_default_config() -> dict:
    """Returns the default configuration as a dictionary."""
    return SpinSenseConfig().dict()

def load_config() -> dict:
    """Loads config.json, creating it with defaults only if it does not exist.

    A file that exists but fails to read or validate is NEVER overwritten. This
    is called on every page request by the setup-wizard middleware, so a single
    truncated read — the engine writing the file, a half-finished editor save —
    used to be enough to replace the user's MQTT password, AudD token and
    calibrated threshold with defaults, silently and irreversibly. We now fall
    back to defaults in memory and leave the file alone, so the next successful
    read recovers everything.
    """
    if not os.path.exists(CONFIG_PATH):
        save_config(get_default_config())

    try:
        with open(CONFIG_PATH, 'r') as f:
            data = json.load(f)
            # Passing data to SpinSenseConfig validates the types automatically
            validated = SpinSenseConfig(**data)
            return validated.dict()
    except Exception as e:
        print(f"⚠️ Error loading config — serving defaults, file left untouched: {e}")
        return get_default_config()

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
