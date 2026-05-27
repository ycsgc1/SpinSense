import asyncio
import json
import os
import io
import wave
import urllib.parse
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
        "Volume_Threshold": 0.015,
        "Song_Sample_Length": 5.0,
        "New_Song_Silence_Interval": 2.0,
        "Stopped_Silence_Interval": 5.0,
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
runtime = {
    "threshold": 0.015,
    "sample_len": 5.0,
    "new_song_silence": 2.0,
    "stopped_silence": 5.0,
    "mic_device": None,
    "mqtt_host": "192.168.1.100",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_pass": "",
}


def _populate_runtime(cfg):
    runtime["threshold"]        = cfg.get('Audio', {}).get('Volume_Threshold', 0.015)
    runtime["sample_len"]       = cfg.get('Audio', {}).get('Song_Sample_Length', 5.0)
    runtime["new_song_silence"] = cfg.get('Audio', {}).get('New_Song_Silence_Interval', 2.0)
    runtime["stopped_silence"]  = cfg.get('Audio', {}).get('Stopped_Silence_Interval', 5.0)
    runtime["mic_device"]       = _normalize_mic(cfg)
    runtime["mqtt_host"]        = cfg.get('MQTT', {}).get('Broker', {}).get('Host', '192.168.1.100')
    runtime["mqtt_port"]        = cfg.get('MQTT', {}).get('Broker', {}).get('Port', 1883)
    runtime["mqtt_user"]        = cfg.get('MQTT', {}).get('Broker', {}).get('User', '')
    runtime["mqtt_pass"]        = cfg.get('MQTT', {}).get('Broker', {}).get('Password', '')


_initial_cfg = _load_config()
_populate_runtime(_initial_cfg)
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
MQTT_ENABLED = False

# Cross-task signal: the config watcher sets this when the mic device changes
# so the audio loop tears down + rebuilds the InputStream on its next pass.
mic_change_event = asyncio.Event()

# Single-flight handle on the connect loop so a broker change doesn't spawn
# parallel connect attempts on the same paho Client.
_mqtt_task: asyncio.Task | None = None


async def connect_mqtt_loop():
    """Initial connect + retries. Re-entered by _reconnect_mqtt() whenever the
    config watcher detects a broker change."""
    global MQTT_ENABLED
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
    loop with the new broker fields. Safe to call when no connection exists."""
    global MQTT_ENABLED, _mqtt_task
    print("📡 MQTT broker changed, reconnecting…")
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
state = {
    "in_song": False,
    "last_song": "",
    "artist": "",
    "title": "",
    "album": "",
    "art_url": "",
    "silence_counter": 0,
    "current_rms": 0.0,
}


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


async def recognize_audio():
    sample_len = runtime["sample_len"]
    mic = runtime["mic_device"]
    print(f"\n[!] Music detected. Recording {sample_len}s for identification...")
    recording = sd.rec(int(sample_len * 48000), samplerate=48000, channels=1, dtype='int16', device=mic)
    await asyncio.to_thread(sd.wait)

    wav_io = io.BytesIO()
    with wave.open(wav_io, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes(recording.tobytes())

    print("[!] Analyzing with Shazam...")
    out = await shazam.recognize(wav_io.getvalue())

    if 'track' in out:
        track = out['track']
        title = track.get('title', 'Unknown Title')
        artist = track.get('subtitle', 'Unknown Artist')

        print("[!] Fetching high-res metadata from iTunes...")
        album, art_url = await fetch_itunes_metadata(artist, title)
        if not art_url:
            art_url = track.get('images', {}).get('coverarthq', track.get('images', {}).get('coverart', ''))
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

        if result_str != state["last_song"]:
            print(f"🎵 NEW TRACK: {result_str}")
            print(f"💿 Album:     {album}")
            print(f"🖼️  Art URL:   {art_url}")
            publish_state("stopped")
            await asyncio.sleep(0.5)
            publish_state("playing", artist, title, album, art_url, art_base64)
            state["last_song"] = result_str
        else:
            print(f"      (Confirmed same track: {state['last_song']})")
            publish_state("playing", artist, title, album, art_url, art_base64)

        state["in_song"] = True
    else:
        print("❌ Could not identify track.")

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


async def audio_monitor_loop():
    global _mqtt_task
    _mqtt_task = asyncio.create_task(connect_mqtt_loop())
    asyncio.create_task(config_watch_loop())
    print("--- VINYL SCROBBLER ALPHA ACTIVE ---")

    def audio_callback(indata, frames, time, status):
        rms = np.sqrt(np.mean(indata ** 2))
        state["current_rms"] = float(rms)

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

        try:
            if os.path.exists('/tmp/spinsense.sock'):
                reader, writer = await asyncio.open_unix_connection('/tmp/spinsense.sock')
                payload = json.dumps({
                    "type": "live_status",
                    "payload": {
                        "rms_level": vol,
                        "engine_active": True,
                        "status_msg": "Playing" if state["in_song"] else "Listening",
                        "track": {
                            "title": state.get("title", ""),
                            "artist": state.get("artist", ""),
                            "album": state.get("album", ""),
                            "art_url": state.get("art_url", ""),
                        },
                    },
                }) + "\n"
                writer.write(payload.encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
        except Exception:
            pass

        if vol > runtime["threshold"]:
            if not state["in_song"] or state["silence_counter"] > 0:
                stream.stop()
                stream.close()
                await recognize_audio()
                stream = _open_input_stream(audio_callback)
                state["current_rms"] = 0.0
            else:
                print(".", end="", flush=True)
        else:
            if state["in_song"]:
                state["silence_counter"] += 1
                print("s", end="", flush=True)
                if state["silence_counter"] >= runtime["stopped_silence"]:
                    print(f"\n[ STOPPED ] {runtime['stopped_silence']}s silence limit reached.")
                    publish_state("stopped")
                    state["in_song"] = False
                    state["last_song"] = ""
                    state["artist"] = ""
                    state["title"] = ""
                    state["album"] = ""
                    state["art_url"] = ""
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


def _apply_config_diff(new_cfg):
    """Re-populate the runtime dict and dispatch side-effects per category."""
    old_mic = runtime["mic_device"]
    old_mqtt = (
        runtime["mqtt_host"], runtime["mqtt_port"],
        runtime["mqtt_user"], runtime["mqtt_pass"],
    )

    _populate_runtime(new_cfg)

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

    if old_mqtt != new_mqtt:
        asyncio.create_task(_reconnect_mqtt())


if __name__ == "__main__":
    try:
        asyncio.run(audio_monitor_loop())
    except KeyboardInterrupt:
        print("\nShutting down...")
        if MQTT_ENABLED:
            mqtt_client.loop_stop()
