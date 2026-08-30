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
    # Peak-normalise each sample before sending it to the recognizer. Quiet
    # songs are the ones it misses; see normalize_pcm() in core/core_engine.py.
    Normalize_Sample: bool = True
    Normalize_Target_dBFS: float = -3.0
    # Throw away a capture that was mostly silence instead of asking the
    # recognizer about it — the needle-drop thump that starts a scan of the
    # lead-in groove. See active_audio_ratio() in core/core_engine.py.
    Needle_Drop_Guard: bool = True
    Retrigger_On_Track_Change: bool = False
    Fallback_Provider: Literal["none", "audd", "acoustid"] = "none"
    AudD_API_Token: str = ""

class LastFMConfig(BaseModel):
    """Scrobbling credentials. The user registers their own API application at
    last.fm/api/account/create, so the rate limit and the terms are theirs.

    Session_Key is obtained by the two-step auth flow in gui/lastfm.py and is
    permanent until revoked. Like the AudD token it is
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
    # When the hold clock starts: "album" waits for the whole record to finish,
    # "track" starts as each song ends. A side is played as a unit, so the album
    # is the useful moment — it means a whole side releases together, and a
    # mislabelled track can be caught while the rest of the record is still on.
    Submit_Trigger: Literal["track", "album"] = "album"
    # Minutes to hold after that moment, so a wrong identification can be
    # deleted or corrected first — Last.fm has no API to edit or remove a
    # scrobble afterwards. 0 submits on the next sweep.
    Submit_Delay_Mins: int = 30


class MDNSConfig(BaseModel):
    Enabled: bool = True
    Service_Name: str = ""  # empty => derive from hostname at runtime

class DiscoveryConfig(BaseModel):
    mDNS: MDNSConfig = MDNSConfig()

class SpinSenseConfig(BaseModel):
    System: SystemConfig = SystemConfig()
    Hardware: HardwareConfig = HardwareConfig()
    Audio: AudioConfig = AudioConfig()
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
    used to be enough to replace the user's AudD token and
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
