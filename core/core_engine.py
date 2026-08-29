import asyncio
import json
import os
import subprocess
import time
import tempfile
import io
import wave
from collections import deque
import aiohttp
import numpy as np
import sounddevice as sd
import paho.mqtt.client as mqtt
import base64
from shazamio import Shazam

import track_clock
from spinsense import itunes
from spinsense.albums import choose_edition

# --- 1. Paths + config bootstrap ---
DATA_DIR = os.environ.get('SPINSENSE_DATA_DIR', os.path.join(os.path.dirname(__file__), '..'))
CONFIG_PATH = os.path.join(DATA_DIR, 'config.json')

DEFAULT_CONFIG = {
    "System": {
        "Auto_Start": False,
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
        "Track_End_Detection": True,
        "Track_End_Grace_Secs": 20.0,
        "Normalize_Sample": True,
        "Normalize_Target_dBFS": -3.0,
        "Retrigger_On_Track_Change": False,
        "Fallback_Provider": "none",
        "AudD_API_Token": "",
        # NOTE: keep these defaults in sync with gui/config_manager.AudioConfig.
    },
    "MQTT": {
        # NOTE: keep these defaults in sync with gui/config_manager.MQTTConfig —
        # whichever process creates config.json first wins, so a divergence here
        # silently decides the user's broker settings.
        "Enabled": False,
        "Broker": {
            "Host": "127.0.0.1",
            "Port": 1883,
            "User": "",
            "Password": "",
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
    "track_end_detection": True,
    "track_end_grace": 20.0,
    "normalize_sample": True,
    "normalize_target_dbfs": -3.0,
    "fallback_provider": "none",
    "audd_token": "",
    "mic_device": None,
    "mqtt_host": "127.0.0.1",
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
    runtime["track_end_detection"] = cfg.get('Audio', {}).get('Track_End_Detection', True)
    runtime["track_end_grace"]  = cfg.get('Audio', {}).get('Track_End_Grace_Secs', 20.0)
    runtime["normalize_sample"] = cfg.get('Audio', {}).get('Normalize_Sample', True)
    runtime["normalize_target_dbfs"] = cfg.get('Audio', {}).get('Normalize_Target_dBFS', -3.0)
    runtime["retrigger_on_track_change"] = cfg.get('Audio', {}).get('Retrigger_On_Track_Change', False)
    runtime["fallback_provider"] = cfg.get('Audio', {}).get('Fallback_Provider', 'none')
    runtime["audd_token"]       = cfg.get('Audio', {}).get('AudD_API_Token', '')
    runtime["mic_device"]       = _normalize_mic(cfg)
    runtime["mqtt_host"]        = cfg.get('MQTT', {}).get('Broker', {}).get('Host', '127.0.0.1')
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

# --- 2. MQTT Setup ---
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
# Runtime connection-state flag: True only once a broker connection is live.
# connect_mqtt_loop() flips it; publish_state() gates on it.
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
        _spawn_bg(_finish_calibration(session))
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
# Strong refs to the other long-lived loops (config watcher, command listener),
# so the event loop's weak task tracking can't GC them mid-run.
_config_task: asyncio.Task | None = None
_command_task: asyncio.Task | None = None
# Strong refs to short-lived fire-and-forget tasks (calibration finish, MQTT
# reconnect) for the same reason; discarded automatically when each completes.
_bg_tasks: set = set()


def _spawn_bg(coro) -> None:
    """create_task + hold a strong ref until done, so the task can't be GC'd."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


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
_MAX_SAMPLE_SECONDS = 60.0  # ceiling for the escalating rescan ladder
SAMPLE_RATE = 48000  # mic capture + recognition sample rate (Hz)

# Ceiling on how much we will amplify a quiet sample. Beyond this the signal is
# so far down that we would mostly be amplifying the noise floor, and handing
# the recognizer louder hiss helps nobody.
MAX_NORMALIZE_GAIN_DB = 30.0
_INT16_PEAK = 32767
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
    "duration_secs": None,
    "album_exclusive": False,
    "back_off": False,
    "force_scan": False,
    # Track-end prediction (core/track_clock.py). `clock` is the current play's
    # TrackClock or None; the capture stamps are two readings of the instant the
    # most recent sample started recording, which is what the clock anchors to.
    "clock": None,
    "capture_mono": 0.0,
    "capture_wall": 0,
    # Input-stall watchdog.
    "last_callback_mono": 0.0,
    "zero_run_secs": 0.0,
    "last_restart_mono": 0.0,
    "input_stalled": False,
}


def normalize_pcm(samples, target_dbfs: float, max_gain_db: float = MAX_NORMALIZE_GAIN_DB):
    """Peak-normalize int16 PCM toward `target_dbfs`, capped at `max_gain_db`.

    Quiet pressings and quiet songs are the ones Shazam misses, even though the
    same needle and preamp identify loud tracks fine. Amplification adds no
    information — a quiet-but-clean line-level signal already holds everything
    the fingerprint needs — but it does stop the recognizer working near the
    bottom of its input range, which is where the misses cluster.

    Pure, so the arithmetic is testable without a sound card. Returns the input
    untouched when it is silent or already at target.
    """
    if samples is None or len(samples) == 0:
        return samples
    peak = float(np.max(np.abs(samples.astype(np.int32))))
    if peak <= 0:
        return samples  # digital silence: nothing to scale

    target_peak = _INT16_PEAK * (10.0 ** (min(float(target_dbfs), 0.0) / 20.0))
    gain = min(target_peak / peak, 10.0 ** (float(max_gain_db) / 20.0))
    if gain <= 1.0:
        return samples  # already at or above target; never attenuate

    scaled = samples.astype(np.float32) * gain
    # Clip before the cast: numpy wraps on int16 overflow, which would turn a
    # loud transient into full-scale noise of the opposite sign.
    return np.clip(scaled, -_INT16_PEAK, _INT16_PEAK).astype(np.int16)


# --- Input-stall detection ---
# The engine went deaf twice in a month: the meter sat at exactly 0 where it
# normally jitters, and only a restart brought it back. Nothing noticed, because
# audio_callback simply stopped being called and the last RMS persisted.
CALLBACK_TIMEOUT_SECS = 5.0    # the callback fires ~22x/sec; 5s of nothing is dead
ZERO_RMS_STALL_SECS = 30.0     # callback alive but handing us digital silence
RESTART_COOLDOWN_SECS = 30.0   # don't thrash if reopening doesn't help


def detect_input_stall(now_mono, last_callback_mono, rms, zero_run_secs,
                       tick_secs: float = 1.0):
    """Pure: one tick of input-liveness checking.

    Returns (zero_run_secs, reason or None). Two independent signals, because
    the failure showed up as both: the callback stopping entirely, and the
    callback still running but returning exact zeros. A real analogue input
    essentially never produces a bit-exact 0.0 RMS, so treating a sustained run
    of them as a fault is safe.
    """
    zero_run_secs = zero_run_secs + tick_secs if rms == 0.0 else 0.0
    if now_mono - last_callback_mono > CALLBACK_TIMEOUT_SECS:
        return zero_run_secs, "no audio callbacks"
    if zero_run_secs >= ZERO_RMS_STALL_SECS:
        return zero_run_secs, "input silent at exactly zero"
    return zero_run_secs, None


def build_status_payload(phase: str, rms: float, st: dict) -> dict:
    """Build a live_status frame. `phase` is the machine-readable recognition
    phase; the track always reflects current state so the GUI's dedupe hook is
    never reset mid-song. The frontend decides display from phase, not track."""
    return {
        "type": "live_status",
        "payload": {
            "rms_level": rms,
            "engine_active": True,
            # False whenever the audio device has gone quiet in a way that
            # isn't music — the dashboard says so instead of looking idle.
            "input_ok": not st.get("input_stalled", False),
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
                "duration_secs": st.get("duration_secs"),
                # True when the track can only be on a qualified edition, which
                # is evidence enough to upgrade the whole run to it.
                "album_exclusive": bool(st.get("album_exclusive")),
            },
            # Where in the track we are and when it really started. Additive:
            # consumers that don't know the key (older HACS integrations) skip
            # it. None whenever no track is playing. See core/track_clock.py.
            "play_clock": track_clock.play_clock_payload(st.get("clock")),
        },
    }


async def _write_uds(line: str) -> None:
    """Best-effort: write one newline-terminated frame to the GUI's UDS. Errors
    are swallowed (the GUI may not be up; the engine must not crash)."""
    try:
        if not os.path.exists('/tmp/spinsense.sock'):
            return
        _, writer = await asyncio.open_unix_connection('/tmp/spinsense.sock')
        writer.write(line.encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


async def emit_event(level: str, message: str) -> None:
    """Send one diagnostic event to the backend's ring buffer.

    The engine's print() output only reaches `docker logs`, which needs shell
    access on the host — so the things worth noticing (a stalled input, a
    recognition given up on, a track-end check firing) were invisible from the
    web UI. Same socket as status frames; the backend files them by `type`.
    """
    print(f"[{level.upper()}] {message}")
    await _write_uds(json.dumps({
        "type": "event",
        "payload": {"ts": int(time.time()), "level": level, "message": message},
    }) + "\n")


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
    """Return (album, art_url, duration_secs, album_exclusive) for a track.

    Asks for several results rather than one, so `choose_edition()` can see
    whether the track also appears on the base album. If every edition it
    appears on is qualified, the record on the platter must be that edition —
    `album_exclusive` carries that finding through to reconciliation, which
    runs much later and cannot re-derive it.
    """
    results = itunes.results_for_track(
        await itunes.search_songs(artist, title), title, artist)
    if not results:
        # iTunes answers a fuzzy query with something rather than nothing, so
        # "no result that is actually this track" is a real and common outcome.
        print(f"[!] iTunes had no match for {title!r} — leaving album unknown")
        return None, None, None, False
    album, exclusive = choose_edition(itunes.album_names(results))
    art_url, duration_secs = itunes.metadata_for(results, album)
    if exclusive:
        print(f"[!] {album!r} is the only edition carrying this track — run upgraded")
    return album, art_url, duration_secs, exclusive


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


async def _capture_sample(sample_len: float | None = None) -> bytes:
    """Record `sample_len` seconds from the mic and return WAV bytes.
    Falls back to the configured base length when called with no argument."""
    if sample_len is None:
        sample_len = runtime["sample_len"]
    mic = runtime["mic_device"]
    # Two readings of the same instant, kept for whichever attempt ends up
    # matching: the play clock anchors to the start of the winning capture, so
    # recognition + network latency never leak into the position estimate.
    state["capture_mono"] = time.monotonic()
    state["capture_wall"] = int(time.time())
    print(f"[!] Recording {sample_len}s sample for identification...")
    recording = sd.rec(int(sample_len * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1,
                       dtype='int16', device=mic)
    await asyncio.to_thread(sd.wait)
    if runtime["normalize_sample"]:
        before = int(np.max(np.abs(recording.astype(np.int32)))) if len(recording) else 0
        recording = normalize_pcm(recording, runtime["normalize_target_dbfs"])
        after = int(np.max(np.abs(recording.astype(np.int32)))) if len(recording) else 0
        if after != before:
            print(f"[!] Normalized sample peak {before} -> {after}")
    wav_io = io.BytesIO()
    with wave.open(wav_io, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(recording.tobytes())
    return wav_io.getvalue()


async def _identify_shazam(wav_bytes: bytes) -> dict | None:
    """Recognize via Shazam; return a normalized track dict, or None on no match.
    Any request error is a clean miss (matches AudD/AcoustID) — an unhandled
    exception here would propagate up through recognize_audio() and kill the
    monitor loop."""
    print("[!] Analyzing with Shazam...")
    try:
        out = await shazam.recognize(wav_bytes)
    except Exception as e:
        print(f"⚠️ Shazam request failed: {e}")
        return None
    if not (isinstance(out, dict) and 'track' in out):
        return None
    track = out['track'] or {}
    images = track.get('images', {}) if isinstance(track, dict) else {}
    enr = _extract_enrichment(track)
    return {
        # Playhead anchor for track-end prediction. Shazam is the only backend
        # that reports where in the reference recording our sample matched.
        "match_offset_secs": track_clock.extract_match_offset(out),
        "title": track.get('title', 'Unknown Title'),
        "artist": track.get('subtitle', 'Unknown Artist'),
        "album": None,  # Shazam has no reliable album; iTunes supplies it downstream
        "art_url": images.get('coverarthq') or images.get('coverart') or None,
        "isrc": enr["isrc"],
        "genre": enr["genre"],
        "release_year": enr["release_year"],
        "duration_secs": None,  # Shazam gives no reliable length
    }


def _audd_to_normalized(result: dict) -> dict:
    """Pure: map an AudD `result` object to the normalized track shape."""
    result = result or {}
    am = result.get("apple_music") or {}
    sp = result.get("spotify") or {}

    release_year = None
    rd = str(result.get("release_date") or "")
    if len(rd) >= 4 and rd[:4].isdigit():
        release_year = int(rd[:4])

    genre = None
    genres = am.get("genreNames")
    if isinstance(genres, list) and genres:
        genre = genres[0] or None

    isrc = am.get("isrc") or result.get("isrc") or None

    # Fallback art only (iTunes is primary downstream): resolve Apple's {w}x{h}
    # artwork template, else a Spotify album image.
    art_url = None
    art = am.get("artwork")
    if isinstance(art, dict) and art.get("url"):
        art_url = str(art["url"]).replace("{w}", "600").replace("{h}", "600")
    elif isinstance(sp.get("album"), dict):
        imgs = sp["album"].get("images")
        if isinstance(imgs, list) and imgs and isinstance(imgs[0], dict):
            art_url = imgs[0].get("url") or None

    duration_secs = None
    dm = am.get("durationInMillis")
    if isinstance(dm, (int, float)) and dm > 0:
        duration_secs = int(round(dm / 1000))

    return {
        "title": result.get("title", "Unknown Title"),
        "artist": result.get("artist", "Unknown Artist"),
        "album": result.get("album") or None,
        "art_url": art_url,
        "isrc": isrc,
        "genre": genre,
        "release_year": release_year,
        "duration_secs": duration_secs,
        "match_offset_secs": None,   # AudD reports no playhead
    }


async def _audd_post(wav_bytes: bytes, token: str) -> dict | None:
    """POST the sample to AudD; return the parsed JSON body, or None on any
    HTTP/timeout/parse error. Isolated so the recognize-flow tests can stub it."""
    try:
        data = aiohttp.FormData()
        data.add_field("api_token", token)
        data.add_field("return", "apple_music,spotify")
        data.add_field("file", wav_bytes, filename="sample.wav", content_type="audio/wav")
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post("https://api.audd.io/", data=data) as resp:
                if resp.status != 200:
                    print(f"⚠️ AudD HTTP {resp.status}")
                    return None
                return await resp.json(content_type=None)
    except Exception as e:
        print(f"⚠️ AudD request failed: {e}")
        return None


async def _identify_audd(wav_bytes: bytes) -> dict | None:
    """Recognize via AudD; return a normalized track dict, or None. No-ops without
    a configured token. Any error is treated as a clean miss."""
    token = runtime["audd_token"]
    if not token:
        return None
    print("[!] Trying AudD fallback...")
    body = await _audd_post(wav_bytes, token)
    if not isinstance(body, dict) or body.get("status") != "success":
        return None
    result = body.get("result")
    if not result:
        return None
    return _audd_to_normalized(result)


ACOUSTID_CLIENT_KEY = os.environ.get("SPINSENSE_ACOUSTID_KEY", "UGhMOSOjGb")
ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"


def _acoustid_to_normalized(results: list) -> dict | None:
    """Pure: map AcoustID lookup `results` to the normalized track shape, or None."""
    if not results:
        return None
    best = max(results, key=lambda r: r.get("score", 0) if isinstance(r, dict) else 0)
    recordings = best.get("recordings") or []
    if not recordings or not isinstance(recordings[0], dict):
        return None
    rec = recordings[0]
    title = rec.get("title")
    if not title:
        return None
    artists = rec.get("artists") or []
    names = [a.get("name", "") for a in artists if isinstance(a, dict) and a.get("name")]
    artist = ", ".join(names) or "Unknown Artist"
    album = None
    rgs = rec.get("releasegroups") or []
    if rgs and isinstance(rgs[0], dict):
        album = rgs[0].get("title") or None
    return {
        "title": title,
        "artist": artist,
        "album": album,
        "art_url": None,   # iTunes enrichment supplies art downstream
        "isrc": None,
        "genre": None,
        "release_year": None,
        "duration_secs": None,
        "match_offset_secs": None,   # AcoustID reports no playhead
    }


def _run_fpcalc(wav_bytes: bytes) -> tuple[int, str] | None:
    """Blocking: write the WAV to a temp file, run `fpcalc -json`, return
    (duration_seconds, fingerprint). None on missing binary / error."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            f.write(wav_bytes)
            f.flush()
            out = subprocess.run(
                ["fpcalc", "-json", f.name],
                capture_output=True, text=True, timeout=15, check=False,
            )
        if out.returncode != 0:
            print(f"⚠️ fpcalc exited {out.returncode}: {out.stderr.strip()}")
            return None
        data = json.loads(out.stdout)
        return (int(round(float(data["duration"]))), data["fingerprint"])
    except FileNotFoundError:
        print("⚠️ fpcalc not installed — AcoustID unavailable")
        return None
    except Exception as e:
        print(f"⚠️ fpcalc failed: {e}")
        return None


async def _chromaprint_fingerprint(wav_bytes: bytes) -> tuple[int, str] | None:
    """Compute a Chromaprint fingerprint via fpcalc, off the event loop."""
    return await asyncio.to_thread(_run_fpcalc, wav_bytes)


async def _acoustid_lookup(duration: int, fingerprint: str) -> dict | None:
    """POST to the AcoustID lookup API; return parsed JSON, or None on any error."""
    try:
        data = aiohttp.FormData()
        data.add_field("client", ACOUSTID_CLIENT_KEY)
        data.add_field("duration", str(duration))
        data.add_field("fingerprint", fingerprint)
        data.add_field("meta", "recordings+releasegroups")
        data.add_field("format", "json")
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(ACOUSTID_LOOKUP_URL, data=data) as resp:
                if resp.status != 200:
                    print(f"⚠️ AcoustID HTTP {resp.status}")
                    return None
                return await resp.json(content_type=None)
    except Exception as e:
        print(f"⚠️ AcoustID request failed: {e}")
        return None


async def _identify_acoustid(wav_bytes: bytes) -> dict | None:
    """Recognize via AcoustID (Chromaprint fingerprint + lookup); normalized dict
    or None. No-ops without a client key; any error is a clean miss."""
    if not ACOUSTID_CLIENT_KEY:
        return None
    fp = await _chromaprint_fingerprint(wav_bytes)
    if not fp:
        return None
    duration, fingerprint = fp
    print("[!] Trying AcoustID fallback...")
    body = await _acoustid_lookup(duration, fingerprint)
    if not isinstance(body, dict) or body.get("status") != "ok":
        return None
    results = body.get("results") or []
    if not results:
        return None
    return _acoustid_to_normalized(results)


async def _identify_fallback(wav_bytes: bytes) -> dict | None:
    """Route the attempt-0 fallback to the configured backup recognizer."""
    provider = runtime["fallback_provider"]
    if provider == "audd":
        return await _identify_audd(wav_bytes)
    if provider == "acoustid":
        return await _identify_acoustid(wav_bytes)
    return None


async def _handle_match(track: dict, reason: str = "onset") -> None:
    """Enrich, publish, and record a matched track. `track` is the NORMALIZED shape
    produced by a backend (_identify_shazam / _identify_audd).

    `reason` is what triggered the recognition — "onset" for the ordinary
    threshold/gap path, "track_end" for a track-end check. It only affects the
    play clock's rescan budget: see the `previous` argument below."""
    title = track.get('title') or 'Unknown Title'
    artist = track.get('artist') or 'Unknown Artist'

    print("[!] Fetching high-res metadata from iTunes...")
    album, art_url, duration_secs, album_exclusive = await fetch_itunes_metadata(artist, title)
    if not art_url:
        art_url = track.get('art_url') or ''   # backend-supplied fallback art
    if not album:
        album = track.get('album') or "Unknown Album"
    if not duration_secs:
        duration_secs = track.get('duration_secs')

    art_base64 = ""
    if art_url:
        print("[!] Encoding album art to Base64 for Home Assistant...")
        art_base64 = await fetch_image_base64(art_url)

    result_str = f"{artist} - {title}"
    state["artist"] = artist
    state["title"] = title
    state["album"] = album
    state["art_url"] = art_url
    state["isrc"] = track.get('isrc')
    state["genre"] = track.get('genre')
    state["release_year"] = track.get('release_year')
    state["duration_secs"] = duration_secs
    state["album_exclusive"] = album_exclusive

    # An end-check that lands on the same track means our prediction was wrong
    # (usually duration metadata for a different edit). Inherit its rescan
    # budget so the mistake can't cost a call every duration+grace for the rest
    # of the side. A same-track match from any other path means gap detection
    # is working, so the budget resets.
    same_track = result_str == state["last_song"]
    state["clock"] = track_clock.start_clock(
        duration_secs=duration_secs,
        match_offset_secs=track.get("match_offset_secs"),
        anchor_mono=state.get("capture_mono") or time.monotonic(),
        anchor_wall=state.get("capture_wall") or int(time.time()),
        grace_floor_secs=runtime["track_end_grace"],
        previous=state.get("clock") if (same_track and reason == "track_end") else None,
    )
    _log_clock(state["clock"])

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


def _log_clock(clock) -> None:
    """One line per match describing the play clock, so offset semantics can be
    sanity-checked against a real record without a debugger."""
    if clock is None or clock.duration_secs is None:
        print("⏱️  Play clock: no duration — track-end prediction disabled for this play.")
        return
    if clock.deadline_mono is None:
        print("⏱️  Play clock: rescan budget spent — no further end-checks this play.")
        return
    print(
        f"⏱️  Play clock: {clock.position_secs:.0f}s into "
        f"{clock.duration_secs:.0f}s ({clock.position_source}); "
        f"end-check in {clock.deadline_mono - time.monotonic():.0f}s"
    )


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
    state["duration_secs"] = None
    state["album_exclusive"] = False
    state["clock"] = None
    state["back_off"] = set_backoff


async def _rescan_pause(seconds: float) -> None:
    """Wait between escalating rescan attempts. Isolated for testability."""
    if seconds > 0:
        await asyncio.sleep(seconds)


async def recognize_audio(preserve_on_miss: bool = False, reason: str = "onset"):
    """Sample + identify with up to 2 auto-retries, lengthening the sample on
    each retry (1x/2x/3x the base length, capped at _MAX_SAMPLE_SECONDS) with a
    Rescan_Wait_Interval pause between attempts. On total failure, publish
    no_match, clear the track, and set the back-off gate so the monitor loop
    waits for a fresh audio onset before scanning again.

    `preserve_on_miss` suppresses that teardown. A track-end check runs against
    a track we have already identified and are still playing, so a miss there
    means "we couldn't tell", not "there is nothing here" — wiping now-playing
    on it would make the feature actively worse than not having it."""
    print("\n[!] Music detected — identifying...")
    base = runtime["sample_len"]
    wait = runtime["rescan_wait"]
    track = None
    for attempt in range(RECOGNIZE_ATTEMPTS):
        if attempt > 0:
            await _rescan_pause(wait)
        await _publish_phase("scanning")
        sample_len = min(base * (attempt + 1), _MAX_SAMPLE_SECONDS)
        wav = await _capture_sample(sample_len)
        await _publish_phase("identifying" if attempt == 0 else "retrying")
        track = await _identify_shazam(wav)
        if track is None and attempt == 0:
            track = await _identify_fallback(wav)  # reuse the first sample
        if track:
            break

    if track:
        await _handle_match(track, reason=reason)
    elif preserve_on_miss:
        print("❓ End-check couldn't identify — keeping the current track.")
        track_clock.defer(state.get("clock"), time.monotonic())
        await _publish_phase("playing" if state["in_song"] else "listening")
    else:
        await emit_event("warning", "Could not identify track — gave up after retries")
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
        samplerate=SAMPLE_RATE, channels=1, callback=callback, device=runtime["mic_device"],
    )
    stream.start()
    return stream


def audio_callback(indata, _frames, _time, _status):
    """Runs on the sounddevice audio thread. Updates the GUI's live RMS
    reading every buffer, and — when a calibration session is collecting —
    appends the per-buffer RMS to its samples deque. deque.append is atomic
    in CPython, safe to call from this thread.

    sounddevice passes four positional arguments and we only use the first;
    the rest carry the underscore convention (and `time`/`status` would shadow
    the module-level `time` import besides)."""
    rms = float(np.sqrt(np.mean(indata ** 2)))
    state["current_rms"] = rms
    # Liveness for the stall watchdog: this is the only place that proves the
    # audio device is still handing us buffers.
    state["last_callback_mono"] = time.monotonic()
    if calibration is not None and calibration["status"] == "running":
        calibration["samples"].append(rms)


def _silence_step(silence_counter, in_song, back_off, new_song_silence, stopped_silence):
    """Pure: process one below-threshold (silence) tick. Returns
    (silence_counter, back_off, stop). `stop` True => clear the track + publish 'stopped'.

    back_off clears only after a *qualifying* gap (>= new_song_silence), so a
    momentary dip or the phantom zero-RMS tick the loop injects right after a scan
    can't re-arm scanning on an unidentifiable track that keeps playing."""
    silence_counter += 1
    if back_off and silence_counter >= new_song_silence:
        back_off = False
    stop = in_song and silence_counter >= stopped_silence
    return silence_counter, back_off, stop


def _scan_decision(vol, threshold, in_song, silence_counter, new_song_silence, back_off):
    """Pure: decide what the monitor loop should do this tick.
    Returns 'scan' | 'tick' | 'wait_gap' | 'silence'.

    A rescan fires on a fresh onset (not in_song) or after a gap that has
    lasted at least `new_song_silence` seconds. A briefer sub-threshold dip
    is treated as the same song still playing (tick), so momentary quiet
    passages don't re-trigger identification."""
    if vol > threshold:
        if back_off:
            return "wait_gap"
        if not in_song:
            return "scan"
        if silence_counter >= new_song_silence:
            return "scan"
        return "tick"
    return "silence"


def _reset_stall_watch() -> None:
    """Re-arm the watchdog after we ourselves closed the stream.

    Recognition stops the stream and zeroes current_rms on purpose. Without
    this the watchdog would read its own handiwork as a dead input and restart
    a device that is working perfectly.
    """
    state["last_callback_mono"] = time.monotonic()
    state["zero_run_secs"] = 0.0


async def audio_monitor_loop():
    global _mqtt_task, _config_task, _command_task
    # Hold strong references to these long-lived tasks: the event loop only
    # keeps a weak ref, so an unreferenced create_task() can be garbage-collected
    # mid-run, silently killing config hot-reload / the command socket.
    _mqtt_task = asyncio.create_task(connect_mqtt_loop())
    _config_task = asyncio.create_task(config_watch_loop())
    _command_task = asyncio.create_task(command_listener_loop())
    print("--- SpinSense engine active ---")

    stream = _open_input_stream(audio_callback)
    state["last_callback_mono"] = time.monotonic()

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
            _reset_stall_watch()

        vol = state["current_rms"]

        # Watchdog: has the audio device stopped talking to us? Checked before
        # anything else, because every decision below trusts `vol`, and a dead
        # input reads exactly like a silent record.
        now_mono = time.monotonic()
        state["zero_run_secs"], stall_reason = detect_input_stall(
            now_mono, state["last_callback_mono"], vol, state["zero_run_secs"],
        )
        if stall_reason and now_mono - state["last_restart_mono"] > RESTART_COOLDOWN_SECS:
            state["last_restart_mono"] = now_mono
            if not state["input_stalled"]:
                state["input_stalled"] = True
                await emit_event(
                    "warning",
                    f"Audio input stalled ({stall_reason}) — restarting capture")
            try:
                stream.stop()
                stream.close()
            except Exception as e:
                print(f"⚠️ Failed to close stalled audio stream: {e}")
            try:
                stream = _open_input_stream(audio_callback)
                state["last_callback_mono"] = time.monotonic()
                state["zero_run_secs"] = 0.0
            except Exception as e:
                await emit_event("error", f"Could not reopen audio input: {e}")
        elif not stall_reason and state["input_stalled"]:
            state["input_stalled"] = False
            await emit_event("info", "Audio input recovered")

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
            _reset_stall_watch()
            await asyncio.sleep(1)
            continue

        decision = _scan_decision(
            vol, runtime["threshold"], state["in_song"],
            state["silence_counter"], runtime["new_song_silence"],
            state.get("back_off", False),
        )
        # Track-end check: the song should be over by now and no gap was
        # detected, so ask what is actually playing rather than keep reporting a
        # track that has probably finished. Deliberately placed after
        # _scan_decision — a real detected gap is always the better trigger.
        if decision in ("tick", "silence") and track_clock.should_check_end(
            state.get("clock"), time.monotonic(),
            enabled=runtime["track_end_detection"],
            in_song=state["in_song"],
            backing_off=state.get("back_off", False),
            gap_qualified=state["silence_counter"] >= runtime["new_song_silence"],
        ):
            await emit_event(
                "info",
                f"Track-end check: {state['last_song']} should be over — re-identifying")
            stream.stop()
            stream.close()
            await recognize_audio(preserve_on_miss=True, reason="track_end")
            stream = _open_input_stream(audio_callback)
            state["current_rms"] = 0.0
            _reset_stall_watch()
            await asyncio.sleep(1)
            continue

        if decision == "scan":
            stream.stop()
            stream.close()
            await recognize_audio()
            stream = _open_input_stream(audio_callback)
            state["current_rms"] = 0.0
            _reset_stall_watch()
        elif decision == "wait_gap":
            print("b", end="", flush=True)
        elif decision == "tick":
            state["silence_counter"] = 0  # song resumed before the gap qualified
            print(".", end="", flush=True)
        else:  # silence
            new_sc, new_bo, stop = _silence_step(
                state["silence_counter"], state["in_song"], state.get("back_off", False),
                runtime["new_song_silence"], runtime["stopped_silence"],
            )
            if state["in_song"]:
                print("s", end="", flush=True)
            state["silence_counter"] = new_sc
            state["back_off"] = new_bo
            if stop:
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
        _spawn_bg(_reconnect_mqtt())


if __name__ == "__main__":
    try:
        asyncio.run(audio_monitor_loop())
    except KeyboardInterrupt:
        print("\nShutting down...")
        if MQTT_ENABLED:
            mqtt_client.loop_stop()
