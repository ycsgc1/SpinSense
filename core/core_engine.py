import asyncio
import json
import os
import io
import wave
import urllib.parse
from collections import deque
import aiohttp
import numpy as np
import sounddevice as sd
import paho.mqtt.client as mqtt
import base64
from shazamio import Shazam

# --- 1. Paths + config bootstrap ---
DATA_DIR = os.environ.get('SPINSENSE_DATA_DIR', os.path.join(os.path.dirname(__file__), '..'))
CONFIG_PATH = os.path.join(DATA_DIR, 'config.json')

DEFAULT_CONFIG = {
    "System": {
        "Auto_Start": False,
        "Engine_Status": "stopped",
        "Setup_Wizard_State": "pending",
    },
    "Hardware": {
        "Mic_Device": "default",
    },
    "Audio": {
        "Volume_Threshold": 0.01,
        "Song_Sample_Length": 5.0,
        "New_Song_Silence_Interval": 3.0,
        "Stopped_Silence_Interval": 5.0,
        "Rescan_Wait_Interval": 5.0,
    },
    "MQTT": {
        "Broker": {
            "Host": "192.168.1.100",
            "Port": 1883,
            "User": "vinylrecord",
            "Password": "",
        },
        "Discovery": {
            "Enabled": True,
            "Discovery_Topic": "homeassistant/media_player/spinsense/config",
        },
        "Topics": {
            "State": "home/vinyl/state",
            "Title": "home/vinyl/title",
            "Artist": "home/vinyl/artist",
            "Album_Art": "home/vinyl/album_art",
        },
    },
}


def _load_config():
    """Read config.json, or write defaults if missing. Returns the dict."""
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CONFIG_PATH, 'w') as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        return json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    with open(CONFIG_PATH, 'r') as f:
        return json.load(f)


def _normalize_mic(cfg):
    v = cfg.get('Hardware', {}).get('Mic_Device', None)
    if v in ("", "default", None):
        return None
    return v


# Mutable mirror of the parts of config that the engine actually reads. The
# file watcher re-populates this dict on every config.json change; the audio
# loop, recognize_audio(), and the MQTT connect loop read from it on every
# iteration so changes take effect without a restart.
# MQTT.Enabled is mirrored into MQTT_WANTED below at startup and refreshed live
# by the config watcher (_apply_config_diff), so toggling MQTT on/off in the GUI
# takes effect without an engine restart. (mDNS is handled in the GUI process.)
runtime = {
    "threshold": 0.01,
    "sample_len": 5.0,
    "new_song_silence": 3.0,
    "stopped_silence": 5.0,
    "rescan_wait": 5.0,
    "mic_device": None,
    "mqtt_host": "192.168.1.100",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_pass": "",
    "retrigger_on_track_change": False,
}


def _populate_runtime(cfg):
    runtime["threshold"]        = cfg.get('Audio', {}).get('Volume_Threshold', 0.01)
    runtime["sample_len"]       = cfg.get('Audio', {}).get('Song_Sample_Length', 5.0)
    runtime["new_song_silence"] = cfg.get('Audio', {}).get('New_Song_Silence_Interval', 3.0)
    runtime["stopped_silence"]  = cfg.get('Audio', {}).get('Stopped_Silence_Interval', 5.0)
    runtime["rescan_wait"]      = cfg.get('Audio', {}).get('Rescan_Wait_Interval', 5.0)
    runtime["retrigger_on_track_change"] = cfg.get('Audio', {}).get('Retrigger_On_Track_Change', False)
    runtime["mic_device"]       = _normalize_mic(cfg)
    runtime["mqtt_host"]        = cfg.get('MQTT', {}).get('Broker', {}).get('Host', '192.168.1.100')
    runtime["mqtt_port"]        = cfg.get('MQTT', {}).get('Broker', {}).get('Port', 1883)
    runtime["mqtt_user"]        = cfg.get('MQTT', {}).get('Broker', {}).get('User', '')
    runtime["mqtt_pass"]        = cfg.get('MQTT', {}).get('Broker', {}).get('Password', '')


_initial_cfg = _load_config()
_populate_runtime(_initial_cfg)
# Config toggle: whether the user wants MQTT at all (read at startup, like the
# other MQTT settings). Distinct from MQTT_ENABLED below, which is the runtime
# "are we currently connected to the broker" flag.
MQTT_WANTED = bool(_initial_cfg.get("MQTT", {}).get("Enabled", False))
try:
    _config_mtime = os.path.getmtime(CONFIG_PATH)
except OSError:
    _config_mtime = None

# MQTT topics + discovery topic remain hardcoded — the corresponding config
# fields aren't read by the engine. Tracked as a future cleanup; not in scope
# for this pass.
BASE_TOPIC = "home/vinyl"
TOPIC_STATE = f"{BASE_TOPIC}/state"
TOPIC_TITLE = f"{BASE_TOPIC}/title"
TOPIC_ARTIST = f"{BASE_TOPIC}/artist"
TOPIC_ALBUM = f"{BASE_TOPIC}/album"
TOPIC_ARTART = f"{BASE_TOPIC}/album_art"
LEGACY_TOPIC = f"{BASE_TOPIC}/now_playing"
DISCOVERY_TOPIC = "homeassistant/media_player/spinsense/config"

# --- 2. MQTT Setup ---
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
# Runtime connection-state flag: True only once a broker connection is live.
# connect_mqtt_loop() flips it; publish_state()/announce_to_ha() gate on it.
MQTT_ENABLED = False

# Cross-task signal: the config watcher sets this when the mic device changes
# so the audio loop tears down + rebuilds the InputStream on its next pass.
mic_change_event = asyncio.Event()

# Active calibration session, or None. The audio callback appends per-buffer
# RMS to ["samples"] when status == "running"; a one-shot timer task flips
# status to "done" after ["duration"] seconds and populates ["stats"].
# Cleared by the wizard via the clear_calibration command after the user
# reads the result.
calibration: dict | None = None


def _compute_stats(samples: list[float]) -> dict:
    """Reduce raw RMS samples into the stats blob returned to the wizard.
    Pure function; no engine state. Percentiles use linear interpolation on
    the sorted samples (matches numpy.percentile's default 'linear' method)."""
    if not samples:
        return {
            "samples_count": 0,
            "min": 0.0,
            "max": 0.0,
            "mean": 0.0,
            "p10": 0.0,
            "p50": 0.0,
            "p99": 0.0,
        }
    arr = sorted(samples)
    n = len(arr)

    def percentile(q: float) -> float:
        idx = (n - 1) * q
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return arr[lo] * (1 - frac) + arr[hi] * frac

    return {
        "samples_count": n,
        "min": arr[0],
        "max": arr[-1],
        "mean": sum(arr) / n,
        "p10": percentile(0.10),
        "p50": percentile(0.50),
        "p99": percentile(0.99),
    }


async def _finish_calibration(session: dict) -> None:
    """Sleep through the session's capture window, then snapshot the samples
    into stats and flip status to 'done'. If the active calibration has been
    replaced (via clear_calibration or a new start_calibration) while we
    slept, this is a no-op — identity check guards against writing into a
    stale session."""
    await asyncio.sleep(session["duration"])
    if calibration is not session:
        return
    session["stats"] = _compute_stats(list(session["samples"]))
    session["status"] = "done"


CMD_SOCKET_PATH = '/tmp/spinsense-cmd.sock'


async def _handle_command(payload: dict) -> dict:
    """Dispatch one command. Pure-ish — only side effect is mutating the
    module-level `calibration` and scheduling the finish timer task."""
    global calibration
    cmd = payload.get("cmd")

    if cmd == "start_calibration":
        if calibration is not None and calibration["status"] == "running":
            return {"ok": False, "detail": "calibration already running"}
        phase = payload.get("phase")
        if phase not in ("noise_floor", "music"):
            return {"ok": False, "detail": f"invalid phase: {phase!r}"}
        session = {
            "phase": phase,
            "samples": deque(maxlen=500),
            "started_at": asyncio.get_event_loop().time(),
            "duration": 5.0,
            "status": "running",
            "stats": None,
        }
        calibration = session
        asyncio.create_task(_finish_calibration(session))
        return {"ok": True, "duration_s": 5.0}

    if cmd == "get_calibration":
        if calibration is None:
            return {"status": "none", "samples_count": 0, "stats": None}
        return {
            "status": calibration["status"],
            "samples_count": len(calibration["samples"]),
            "stats": calibration["stats"],
        }

    if cmd == "clear_calibration":
        calibration = None
        return {"ok": True}

    if cmd == "rescan":
        state["force_scan"] = True
        state["back_off"] = False
        return {"ok": True}

    return {"ok": False, "detail": f"unknown cmd: {cmd!r}"}


async def _command_client_handler(reader, writer):
    """One JSON-line in, one JSON-line out. Connections are short-lived."""
    try:
        line = await reader.readline()
        if not line:
            return
        try:
            payload = json.loads(line.decode())
        except Exception as e:
            response = {"ok": False, "detail": f"json parse error: {e}"}
        else:
            try:
                response = await _handle_command(payload)
            except Exception as e:
                response = {"ok": False, "detail": f"handler error: {e}"}
        writer.write((json.dumps(response) + "\n").encode())
        await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def command_listener_loop():
    """Bind CMD_SOCKET_PATH and serve commands until cancelled. Removes a
    pre-existing socket file (matches the pattern used by the backend's
    /tmp/spinsense.sock listener)."""
    if os.path.exists(CMD_SOCKET_PATH):
        os.remove(CMD_SOCKET_PATH)
    server = await asyncio.start_unix_server(
        _command_client_handler, path=CMD_SOCKET_PATH,
    )
    print(f"🎛️ Command listener bound on {CMD_SOCKET_PATH}")
    async with server:
        await server.serve_forever()


# Single-flight handle on the connect loop so a broker change doesn't spawn
# parallel connect attempts on the same paho Client.
_mqtt_task: asyncio.Task | None = None


async def connect_mqtt_loop():
    """Initial connect + retries. Re-entered by _reconnect_mqtt() whenever the
    config watcher detects a broker change."""
    global MQTT_ENABLED
    if not MQTT_WANTED:
        print("📡 MQTT disabled in config — skipping broker connection.")
        return
    if runtime["mqtt_user"] and runtime["mqtt_pass"]:
        mqtt_client.username_pw_set(runtime["mqtt_user"], runtime["mqtt_pass"])
    print(f"📡 MQTT connecting to {runtime['mqtt_host']}:{runtime['mqtt_port']}...")
    while not MQTT_ENABLED:
        try:
            await asyncio.to_thread(
                mqtt_client.connect,
                runtime["mqtt_host"],
                runtime["mqtt_port"],
                60,
            )
            mqtt_client.loop_start()
            MQTT_ENABLED = True
            print("✅ MQTT Connected!")
            announce_to_ha()
        except Exception as e:
            print(f"⚠️ MQTT Connection Failed: {e}. Retrying in 10s...")
            await asyncio.sleep(10)


async def _reconnect_mqtt():
    """Tear down the current MQTT client connection and re-enter the connect
    loop. Used when the broker fields change, or when the MQTT enable toggle
    flips: connect_mqtt_loop() returns immediately if MQTT is now disabled, so
    this doubles as a clean teardown. Safe to call when no connection exists."""
    global MQTT_ENABLED, _mqtt_task
    print("📡 MQTT settings changed, reapplying…")
    if _mqtt_task and not _mqtt_task.done():
        _mqtt_task.cancel()
        try:
            await _mqtt_task
        except (asyncio.CancelledError, Exception):
            pass
    try:
        if MQTT_ENABLED:
            await asyncio.to_thread(mqtt_client.loop_stop)
            await asyncio.to_thread(mqtt_client.disconnect)
    except Exception as e:
        print(f"⚠️ MQTT disconnect failed (continuing): {e}")
    MQTT_ENABLED = False
    _mqtt_task = asyncio.create_task(connect_mqtt_loop())


def announce_to_ha():
    payload = {
        "name": "Vinyl Record Player",
        "state_state_topic": TOPIC_STATE,
        "state_title_topic": TOPIC_TITLE,
        "state_artist_topic": TOPIC_ARTIST,
        "state_album_topic": TOPIC_ALBUM,
        "state_albumart_topic": TOPIC_ARTART,
    }
    if MQTT_ENABLED:
        mqtt_client.publish(DISCOVERY_TOPIC, json.dumps(payload), retain=True)
        print("📡 Sent HACS Auto-Discovery Payload.")


def publish_state(status, artist="", title="", album="", art_url="", art_base64=""):
    if MQTT_ENABLED:
        mqtt_client.publish(TOPIC_STATE, status, retain=True)
        mqtt_client.publish(TOPIC_TITLE, title, retain=True)
        mqtt_client.publish(TOPIC_ARTIST, artist, retain=True)
        mqtt_client.publish(TOPIC_ALBUM, album, retain=True)
        if art_base64:
            mqtt_client.publish(TOPIC_ARTART, art_base64, retain=True)
        else:
            mqtt_client.publish(TOPIC_ARTART, "", retain=True)
        payload = json.dumps({
            "status": status, "artist": artist, "title": title,
            "album": album, "art_url": art_url,
        })
        mqtt_client.publish(LEGACY_TOPIC, payload, retain=True)
        print(f"📡 Published State -> Status: {status.upper()} | {artist} - {title}")
    else:
        print(f"[MOCK MQTT] Published State -> Status: {status.upper()} | {artist} - {title}")


# --- 3. Shazam, iTunes, & Audio Logic ---
shazam = Shazam()
RECOGNIZE_ATTEMPTS = 3  # 1 initial + 2 auto-retries
state = {
    "in_song": False,
    "last_song": "",
    "artist": "",
    "title": "",
    "album": "",
    "art_url": "",
    "silence_counter": 0,
    "current_rms": 0.0,
    "isrc": None,
    "genre": None,
    "release_year": None,
    "back_off": False,
    "force_scan": False,
}


def build_status_payload(phase: str, rms: float, st: dict) -> dict:
    """Build a live_status frame. `phase` is the machine-readable recognition
    phase; the track always reflects current state so the GUI's dedupe hook is
    never reset mid-song. The frontend decides display from phase, not track."""
    return {
        "type": "live_status",
        "payload": {
            "rms_level": rms,
            "engine_active": True,
            "phase": phase,
            "status_msg": "Playing" if st.get("in_song") else "Listening",
            "track": {
                "title": st.get("title", "") or "",
                "artist": st.get("artist", "") or "",
                "album": st.get("album", "") or "",
                "art_url": st.get("art_url", "") or "",
                "isrc": st.get("isrc"),
                "genre": st.get("genre"),
                "release_year": st.get("release_year"),
            },
        },
    }


async def _write_uds(line: str) -> None:
    """Best-effort: write one newline-terminated frame to the GUI's UDS. Errors
    are swallowed (the GUI may not be up; the engine must not crash)."""
    try:
        if not os.path.exists('/tmp/spinsense.sock'):
            return
        reader, writer = await asyncio.open_unix_connection('/tmp/spinsense.sock')
        writer.write(line.encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


async def _publish_phase(phase: str) -> None:
    """Publish a phase frame using current state + last RMS reading."""
    payload = build_status_payload(phase, state.get("current_rms", 0.0), state)
    await _write_uds(json.dumps(payload) + "\n")


async def _publish_idle_blip() -> None:
    """Emit one in_song=False live_status frame so WebSocket consumers (the HACS
    media_player + the dashboard) see a PLAYING->IDLE transition between tracks,
    re-firing 'started playing' automations. Gated by Retrigger_On_Track_Change."""
    payload = build_status_payload("listening", state.get("current_rms", 0.0), {"in_song": False})
    await _write_uds(json.dumps(payload) + "\n")


async def fetch_itunes_metadata(artist, title):
    query = urllib.parse.quote_plus(f"{artist} {title}")
    url = f"https://itunes.apple.com/search?term={query}&entity=song&limit=1"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json(content_type=None)
                    if data.get("resultCount", 0) > 0:
                        result = data["results"][0]
                        album = result.get("collectionName", "")
                        art_url = result.get("artworkUrl100", "").replace("100x100bb", "1000x1000bb")
                        return album, art_url
    except Exception as e:
        print(f"⚠️ iTunes API error: {e}")
    return None, None


async def fetch_image_base64(url):
    if not url:
        return ""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    img_bytes = await response.read()
                    return base64.b64encode(img_bytes).decode('utf-8')
    except Exception as e:
        print(f"⚠️ Failed to encode album art to base64: {e}")
    return ""


def _extract_enrichment(track: dict) -> dict:
    """Best-effort pull of stable-id/genre/year from a Shazam track object.
    Every field is optional; anything missing or unparseable is None so it
    never blocks a play from being recorded."""
    track = track or {}
    isrc = track.get("isrc") or None

    genre = None
    genres = track.get("genres")
    if isinstance(genres, dict):
        genre = genres.get("primary") or None

    release_year = None
    sections = track.get("sections")
    for section in sections if isinstance(sections, list) else []:
        if not isinstance(section, dict):
            continue
        metadata = section.get("metadata")
        for item in metadata if isinstance(metadata, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("title") == "Released":
                text = str(item.get("text", "")).strip()
                digits = ""
                for ch in text:
                    if ch.isdigit():
                        digits += ch
                        if len(digits) == 4:
                            break
                    elif digits:
                        break
                if len(digits) == 4:
                    release_year = int(digits)
                break
        if release_year is not None:
            break

    return {"isrc": isrc, "genre": genre, "release_year": release_year}


async def _capture_sample() -> bytes:
    """Record sample_len seconds from the mic and return WAV bytes."""
    sample_len = runtime["sample_len"]
    mic = runtime["mic_device"]
    print(f"[!] Recording {sample_len}s sample for identification...")
    recording = sd.rec(int(sample_len * 48000), samplerate=48000, channels=1,
                       dtype='int16', device=mic)
    await asyncio.to_thread(sd.wait)
    wav_io = io.BytesIO()
    with wave.open(wav_io, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes(recording.tobytes())
    return wav_io.getvalue()


async def _identify(wav_bytes: bytes) -> dict | None:
    """Return the matched Shazam track dict, or None if no match."""
    print("[!] Analyzing with Shazam...")
    out = await shazam.recognize(wav_bytes)
    if isinstance(out, dict) and 'track' in out:
        return out['track']
    return None


async def _handle_match(track: dict) -> None:
    """Enrich, publish, and record a matched track (the old success branch)."""
    title = track.get('title', 'Unknown Title')
    artist = track.get('subtitle', 'Unknown Artist')

    print("[!] Fetching high-res metadata from iTunes...")
    album, art_url = await fetch_itunes_metadata(artist, title)
    if not art_url:
        art_url = track.get('images', {}).get('coverarthq',
                  track.get('images', {}).get('coverart', ''))
    if not album:
        album = "Unknown Album"

    art_base64 = ""
    if art_url:
        print("[!] Encoding album art to Base64 for Home Assistant...")
        art_base64 = await fetch_image_base64(art_url)

    result_str = f"{artist} - {title}"
    state["artist"] = artist
    state["title"] = title
    state["album"] = album
    state["art_url"] = art_url
    enrichment = _extract_enrichment(track)
    state["isrc"] = enrichment["isrc"]
    state["genre"] = enrichment["genre"]
    state["release_year"] = enrichment["release_year"]

    if result_str != state["last_song"]:
        print(f"🎵 NEW TRACK: {result_str}")
        print(f"💿 Album:     {album}")
        print(f"🖼️  Art URL:   {art_url}")
        if runtime.get("retrigger_on_track_change"):
            # Re-announce on BOTH protocols: stopped on MQTT + idle on the WS,
            # so HA automations re-fire on the track change either way.
            publish_state("stopped")
            await _publish_idle_blip()
            await asyncio.sleep(0.5)
        publish_state("playing", artist, title, album, art_url, art_base64)
        state["last_song"] = result_str
    else:
        print(f"      (Confirmed same track: {state['last_song']})")
        publish_state("playing", artist, title, album, art_url, art_base64)

    state["in_song"] = True
    state["back_off"] = False
    await _publish_phase("playing")


def _clear_track_state(set_backoff: bool) -> None:
    """Reset all track + enrichment fields to the 'no song' state. `set_backoff`
    arms the re-scan back-off gate — True after a no_match (don't re-hammer the
    same unidentifiable audio), False on a natural silence-stop."""
    state["in_song"] = False
    state["last_song"] = ""
    state["artist"] = ""
    state["title"] = ""
    state["album"] = ""
    state["art_url"] = ""
    state["isrc"] = None
    state["genre"] = None
    state["release_year"] = None
    state["back_off"] = set_backoff


async def recognize_audio():
    """Sample + identify with up to 2 auto-retries. On total failure, publish
    no_match, clear the track, and set the back-off gate so the monitor loop
    waits for a fresh audio onset before scanning again."""
    print("\n[!] Music detected — identifying...")
    track = None
    for attempt in range(RECOGNIZE_ATTEMPTS):
        await _publish_phase("scanning")
        wav = await _capture_sample()
        await _publish_phase("identifying" if attempt == 0 else "retrying")
        track = await _identify(wav)
        if track:
            break

    if track:
        await _handle_match(track)
    else:
        print("❌ Could not identify track (gave up).")
        # Order matters: clear the track (emptying the title) BEFORE publishing
        # no_match, so the empty-title frame resets ipc_manager's dedupe. Reorder
        # these and a same-title track after a failed ID could be dropped or
        # double-recorded.
        _clear_track_state(set_backoff=True)
        await _publish_phase("no_match")

    state["silence_counter"] = 0


def _open_input_stream(callback):
    """Open and start a sounddevice InputStream against the current mic. Pulled
    out of audio_monitor_loop() so the same code handles fresh startup, the
    post-recognition relock, and the mic-changed rebuild."""
    stream = sd.InputStream(
        samplerate=48000, channels=1, callback=callback, device=runtime["mic_device"],
    )
    stream.start()
    return stream


def audio_callback(indata, frames, time, status):
    """Runs on the sounddevice audio thread. Updates the GUI's live RMS
    reading every buffer, and — when a calibration session is collecting —
    appends the per-buffer RMS to its samples deque. deque.append is atomic
    in CPython, safe to call from this thread."""
    rms = float(np.sqrt(np.mean(indata ** 2)))
    state["current_rms"] = rms
    if calibration is not None and calibration["status"] == "running":
        calibration["samples"].append(rms)


def _scan_decision(vol, threshold, in_song, silence_counter, back_off):
    """Pure: decide what the monitor loop should do this tick.
    Returns 'scan' | 'tick' | 'wait_gap' | 'silence'."""
    if vol > threshold:
        if back_off:
            return "wait_gap"
        if (not in_song) or silence_counter > 0:
            return "scan"
        return "tick"
    return "silence"


async def audio_monitor_loop():
    global _mqtt_task
    _mqtt_task = asyncio.create_task(connect_mqtt_loop())
    asyncio.create_task(config_watch_loop())
    asyncio.create_task(command_listener_loop())
    print("--- VINYL SCROBBLER ALPHA ACTIVE ---")

    stream = _open_input_stream(audio_callback)

    while True:
        # Honor a mic-device change before we evaluate this iteration's volume.
        if mic_change_event.is_set():
            try:
                stream.stop()
                stream.close()
            except Exception as e:
                print(f"⚠️ Failed to close audio stream: {e}")
            try:
                stream = _open_input_stream(audio_callback)
                print(f"🎤 Mic device now {runtime['mic_device']!r}, stream restarted")
            except Exception as e:
                print(f"⚠️ Failed to open new audio stream: {e}")
            mic_change_event.clear()
            state["current_rms"] = 0.0

        vol = state["current_rms"]

        phase = "playing" if state["in_song"] else "listening"
        await _write_uds(json.dumps(build_status_payload(phase, vol, state)) + "\n")

        # Suppress detection during an active calibration capture window.
        # The audio callback still appends samples + still updates the live
        # meter — only the recognize/silence-tracking logic is paused.
        if calibration is not None and calibration["status"] == "running":
            await asyncio.sleep(1)
            continue

        if state.get("force_scan"):
            state["force_scan"] = False
            stream.stop()
            stream.close()
            await recognize_audio()
            stream = _open_input_stream(audio_callback)
            state["current_rms"] = 0.0
            await asyncio.sleep(1)
            continue

        decision = _scan_decision(
            vol, runtime["threshold"], state["in_song"],
            state["silence_counter"], state.get("back_off", False),
        )
        if decision == "scan":
            stream.stop()
            stream.close()
            await recognize_audio()
            stream = _open_input_stream(audio_callback)
            state["current_rms"] = 0.0
        elif decision == "wait_gap":
            print("b", end="", flush=True)
        elif decision == "tick":
            print(".", end="", flush=True)
        else:  # silence
            state["back_off"] = False  # gap observed → next onset is fair game
            if state["in_song"]:
                state["silence_counter"] += 1
                print("s", end="", flush=True)
                if state["silence_counter"] >= runtime["stopped_silence"]:
                    print(f"\n[ STOPPED ] {runtime['stopped_silence']}s silence limit reached.")
                    publish_state("stopped")
                    _clear_track_state(set_backoff=False)
                    state["silence_counter"] = 0

        await asyncio.sleep(1)


# --- 4. Live config reload ---
async def config_watch_loop():
    """Poll CONFIG_PATH mtime every 2s. When it changes, re-read the file and
    dispatch handlers based on which categories actually differ."""
    global _config_mtime
    while True:
        await asyncio.sleep(2)
        try:
            m = os.path.getmtime(CONFIG_PATH)
        except OSError:
            continue
        if m == _config_mtime:
            continue
        try:
            with open(CONFIG_PATH, 'r') as f:
                new_cfg = json.load(f)
        except Exception as e:
            print(f"⚠️ Config reload failed: {e}")
            continue
        _apply_config_diff(new_cfg)
        _config_mtime = m


def _should_reapply_mqtt(old_wanted: bool, new_wanted: bool, broker_changed: bool) -> bool:
    """Whether an MQTT teardown/reconnect is needed after a config change.
    Re-apply when the enable toggle flipped (either direction), or when the
    broker settings changed while MQTT is (still) enabled."""
    return old_wanted != new_wanted or (new_wanted and broker_changed)


def _apply_config_diff(new_cfg):
    """Re-populate the runtime dict and dispatch side-effects per category."""
    global MQTT_WANTED
    old_mic = runtime["mic_device"]
    old_mqtt = (
        runtime["mqtt_host"], runtime["mqtt_port"],
        runtime["mqtt_user"], runtime["mqtt_pass"],
    )
    old_wanted = MQTT_WANTED

    _populate_runtime(new_cfg)
    MQTT_WANTED = bool(new_cfg.get("MQTT", {}).get("Enabled", False))

    new_mic = runtime["mic_device"]
    new_mqtt = (
        runtime["mqtt_host"], runtime["mqtt_port"],
        runtime["mqtt_user"], runtime["mqtt_pass"],
    )

    print(
        f"⚙️ Config reloaded — threshold={runtime['threshold']:.4f}, "
        f"sample={runtime['sample_len']}s, "
        f"stopped_silence={runtime['stopped_silence']}s"
    )

    if old_mic != new_mic:
        print(f"🎤 Mic device change queued: {old_mic!r} → {new_mic!r}")
        mic_change_event.set()

    if _should_reapply_mqtt(old_wanted, MQTT_WANTED, old_mqtt != new_mqtt):
        asyncio.create_task(_reconnect_mqtt())


if __name__ == "__main__":
    try:
        asyncio.run(audio_monitor_loop())
    except KeyboardInterrupt:
        print("\nShutting down...")
        if MQTT_ENABLED:
            mqtt_client.loop_stop()
