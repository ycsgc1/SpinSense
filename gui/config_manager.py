"""config.json: its schema, and the code that changes it.

One file under SPINSENSE_DATA_DIR holds every setting. This process validates
and writes it; the engine reads it — writing it only to create one that is
missing — and notices a change by the file's modification time (see
`config_watch_loop` in core/core_engine.py). That, and replacing the file
whole on every save (`spinsense.files.write_atomically`), is all the
coordination two processes sharing one file need.

The models below are the schema. A field added here with a default is all a
migration takes: an older file simply lacks the key and reads as the default.
Anything the engine also reads must be given the same default in
`core_engine.DEFAULT_CONFIG`; `tests/test_config_round_trip.py` checks the two
agree.
"""
import copy
import json
import os
from typing import Literal
from pydantic import BaseModel

from spinsense.files import write_atomically

# Resolve config folder dynamically using the environment variable SPINSENSE_DATA_DIR
DATA_DIR = os.environ.get('SPINSENSE_DATA_DIR', os.path.join(os.path.dirname(__file__), '..'))
CONFIG_PATH = os.path.join(DATA_DIR, 'config.json')

# --- Pydantic Models for Strict Type Validation ---
class SystemConfig(BaseModel):
    """App-level state. `Setup_Wizard_State` is what the page gate reads:
    "pending" sends every page to /setup until the wizard is saved or skipped."""
    Setup_Wizard_State: Literal["pending", "skipped", "completed"] = "pending"

class HardwareConfig(BaseModel):
    """The capture device, by PortAudio name. "default" is the system input."""
    Mic_Device: str = "default"

class AudioConfig(BaseModel):
    """Detection and recognition tuning. Every field is read by the engine and
    hot-reloaded; intervals are in seconds, Volume_Threshold is linear RMS
    (the UI shows it in dB)."""
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
    """Scrobbling: the connection, and how plays are released to it.

    API_Key / API_Secret are empty unless the user overrides the application
    SpinSense ships with (see `lastfm.credentials()`); the built-in pair is
    resolved at run time and never written here.

    Session_Key is obtained by the auth flow in gui/lastfm.py and is permanent
    until revoked. Like the AudD token it is stored in plaintext — fine for a
    self-hosted LAN box, worth knowing before committing config.json anywhere.

    The key, secret, session, username and Scrobble_Since are written only by
    that flow. POST /api/config ignores whatever a page sends for them, since
    a page saves a snapshot that may predate the connection — see
    `lastfm.keep_connection()`.

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
    """Whether to advertise on the LAN for Home Assistant, and under what name."""
    Enabled: bool = True
    Service_Name: str = ""  # empty => derive from hostname at runtime

class DiscoveryConfig(BaseModel):
    """How SpinSense makes itself findable. mDNS is the only way today."""
    mDNS: MDNSConfig = MDNSConfig()

class SpinSenseConfig(BaseModel):
    """The whole of config.json. Every section has defaults, so a partial or
    empty file validates and reads as a fresh install."""
    System: SystemConfig = SystemConfig()
    Hardware: HardwareConfig = HardwareConfig()
    Audio: AudioConfig = AudioConfig()
    LastFM: LastFMConfig = LastFMConfig()
    Discovery: DiscoveryConfig = DiscoveryConfig()

# --- Core Functions ---
#
# `.dict()` and not `.model_dump()`: the image runs pydantic 1.x, because
# shazamio 0.5.1 requires it (see constraints.txt). A machine set up without
# the pins gets pydantic 2, where `.dict()` still works and only warns; the
# other spelling does not exist on what actually ships.
def get_default_config() -> dict:
    """Returns the default configuration as a dictionary."""
    return SpinSenseConfig().dict()

def read_config() -> dict | None:
    """What is saved in config.json, validated — or None if it can't be read.

    For the callers that must tell "the file says this" from "we fell back to
    defaults": merging a default over a saved value is how settings get lost.
    Never creates or rewrites the file.
    """
    try:
        with open(CONFIG_PATH, 'r') as f:
            data = json.load(f)
            # Passing data to SpinSenseConfig validates the types automatically
            return SpinSenseConfig(**data).dict()
    except Exception as e:
        print(f"⚠️ Error loading config — file left untouched: {e}")
        return None


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

    saved = read_config()
    return saved if saved is not None else get_default_config()

def save_config(data: dict) -> bool:
    """Validates and saves a dictionary to config.json.

    The file is replaced whole, never rewritten where it stands: the engine
    reads it from another process, and a save cut short — a crash, the power
    going — must not leave half a file where the settings were.
    """
    try:
        validated = SpinSenseConfig(**data)
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        write_atomically(CONFIG_PATH, json.dumps(validated.dict(), indent=2))
        return True
    except Exception as e:
        print(f"❌ Error saving config (Validation failed): {e}")
        return False


# --- What a browser is shown ---
#
# Nothing here asks who is calling: every route is open to whoever can reach
# the port. Settings can be read back by anyone on the network, and that is
# accepted. Credentials are different — they are worth something away from
# this box, and no page needs to read one in order to keep it.

# Stands in for a saved secret in what GET /api/config returns. A page that
# posts it back unchanged means "leave that as it is".
SECRET_PLACEHOLDER = "********"

# (section, field) of every value that is a credential.
SECRET_FIELDS = (
    ("Audio", "AudD_API_Token"),
    ("LastFM", "API_Secret"),
    ("LastFM", "Session_Key"),
)


def without_secrets(config: dict) -> dict:
    """A copy of `config` fit to send to a browser.

    Each saved secret becomes the placeholder; an empty one stays empty, so a
    page can still tell "set" from "not set".
    """
    shown = copy.deepcopy(config)
    for section, field in SECRET_FIELDS:
        part = shown.get(section)
        if isinstance(part, dict) and part.get(field):
            part[field] = SECRET_PLACEHOLDER
    return shown


def with_saved_secrets(new_config: dict) -> dict:
    """`new_config` with each placeholder turned back into what is saved.

    The other half of `without_secrets`, for a config a page posts back. A
    value the page changed — a new token, or an emptied field — is kept as
    sent. The placeholder itself never reaches the file: with nothing readable
    on disk to restore, the field is saved empty.
    """
    merged = copy.deepcopy(new_config)
    saved = None
    for section, field in SECRET_FIELDS:
        part = merged.get(section)
        if not isinstance(part, dict) or part.get(field) != SECRET_PLACEHOLDER:
            continue
        if saved is None:
            saved = read_config() or get_default_config()
        part[field] = saved[section][field]
    return merged
