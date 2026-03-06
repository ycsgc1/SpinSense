import asyncio
import json
import os
import numpy as np
import sounddevice as sd
import paho.mqtt.client as mqtt
from shazamio import Shazam

# --- 1. Load Configuration ---
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
with open(CONFIG_PATH, 'r') as f:
    config = json.load(f)

# Extract config vars
THRESHOLD = config['Audio']['Volume_Threshold']
SILENCE_LIMIT = config['Audio']['Stopped_Silence_Interval']
SAMPLE_LEN = config['Audio']['Song_Sample_Length']

MQTT_HOST = config['MQTT']['Broker']['Host']
MQTT_USER = config['MQTT']['Broker']['User']
MQTT_PASS = config['MQTT']['Broker']['Password']
MQTT_PORT = config['MQTT']['Broker']['Port']

TOPIC_STATE = config['MQTT']['Topics']['State']
TOPIC_TITLE = config['MQTT']['Topics']['Title']
TOPIC_ARTIST = config['MQTT']['Topics']['Artist']
TOPIC_ARTART = config['MQTT']['Topics']['Album_Art'] # Added for HA
DISCOVERY_TOPIC = config['MQTT']['Discovery']['Discovery_Topic']
LEGACY_TOPIC = "home/vinyl/now_playing" # From your bash script

# --- 2. MQTT Setup ---
mqtt_client = mqtt.Client()
if MQTT_USER and MQTT_PASS:
    mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
mqtt_client.loop_start()

def announce_to_ha():
    """Publishes the Home Assistant Discovery Payload."""
    payload = {
        "name": "Vinyl Record Player",
        "unique_id": "vinyl_pi_record_player",
        "device_class": "speaker",
        "state_topic": TOPIC_STATE, # Updated for cleaner HA integration
        "json_attributes_topic": LEGACY_TOPIC, # Stores extra data like Art/Album
        "payload_play": "playing",
        "payload_stop": "stopped",
        "payload_idle": "idle"
    }
    mqtt_client.publish(DISCOVERY_TOPIC, json.dumps(payload), retain=True)

def publish_state(status, artist="", title="", album="", art_url=""):
    """Publishes state to split topics and legacy JSON topic."""
    # Split topics for HACS Media Player
    mqtt_client.publish(TOPIC_STATE, status, retain=True)
    mqtt_client.publish(TOPIC_TITLE, title, retain=True)
    mqtt_client.publish(TOPIC_ARTIST, artist, retain=True)
    mqtt_client.publish(TOPIC_ARTART, art_url, retain=True)

    # Legacy JSON payload (added album and art)
    payload = json.dumps({
        "status": status,
        "artist": artist,
        "title": title,
        "album": album,
        "art_url": art_url
    })
    mqtt_client.publish(LEGACY_TOPIC, payload, retain=True)

# --- 3. Shazam & Audio Logic ---
shazam = Shazam()
state = {
    "in_song": False,
    "last_song": "",
    "silence_counter": 0,
    "current_rms": 0.0
}

async def recognize_audio():
    """Records 10 seconds of audio and identifies it via Shazam."""
    print(f"\n[!] Music detected. Recording {SAMPLE_LEN}s for identification...")
    
    # Record audio block
    recording = sd.rec(int(SAMPLE_LEN * 48000), samplerate=48000, channels=1, dtype='float32')
    sd.wait() # Wait until recording is finished
    
    # Convert float32 array to bytes for Shazam
    audio_bytes = (recording * 32767).astype(np.int16).tobytes()
    
    print("[!] Analyzing with Shazam...")
    out = await shazam.recognize_song(audio_bytes)
    
    if 'track' in out:
        track = out['track']
        title = track.get('title', 'Unknown Title')
        artist = track.get('subtitle', 'Unknown Artist')
        
        # Extract Album Art (Prioritize high res, fallback to low res)
        art_url = track.get('images', {}).get('coverarthq', track.get('images', {}).get('coverart', ''))
        
        # Extract Album Name (Usually buried in sections)
        album = "Unknown Album"
        for section in track.get('sections', []):
            if section.get('type') == 'SONG':
                for meta in section.get('metadata', []):
                    if meta.get('title') == 'Album':
                        album = meta.get('text')
        
        result_str = f"{artist} - {title}"
        
        if result_str != state["last_song"]:
            print(f"🎵 NEW TRACK: {result_str} (Album: {album})")
            publish_state("stopped")
            await asyncio.sleep(0.5)
            publish_state("playing", artist, title, album, art_url)
            state["last_song"] = result_str
        else:
            print(f"      (Confirmed same track: {state['last_song']})")
            publish_state("playing", artist, title, album, art_url)
            
        state["in_song"] = True
    else:
        print("❌ Could not identify track.")
        
    state["silence_counter"] = 0

async def audio_monitor_loop():
    """Continuous loop monitoring microphone RMS."""
    announce_to_ha()
    print(f"--- VINYL SCROBBLER ALPHA ACTIVE (Threshold: {THRESHOLD}) ---")
    
    def audio_callback(indata, frames, time, status):
        # Calculate RMS for this small chunk of audio
        rms = np.sqrt(np.mean(indata**2))
        state["current_rms"] = float(rms)

    # Open continuous audio stream (Updates multiple times a second)
    stream = sd.InputStream(samplerate=48000, channels=1, callback=audio_callback)
    with stream:
        while True:
            vol = state["current_rms"]
            
            # Send data to GUI via Unix Socket (If it exists)
            try:
                if os.path.exists('/tmp/spinsense.sock'):
                    reader, writer = await asyncio.open_unix_connection('/tmp/spinsense.sock')
                    payload = json.dumps({
                        "type": "live_status",
                        "payload": {
                            "rms_level": vol,
                            "engine_active": True,
                            "status_msg": "Listening",
                            "track": {"title": state["last_song"]}
                        }
                    }) + "\n"
                    writer.write(payload.encode())
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
            except Exception:
                pass # Fail silently if GUI is not running
            
            # State Machine Logic (Mirrors your bash script)
            if vol > THRESHOLD:
                if not state["in_song"] or state["silence_counter"] > 0:
                    # Trigger identification! (Pause monitoring to record)
                    stream.stop()
                    await recognize_audio()
                    stream.start()
                else:
                    print(".", end="", flush=True)
            else:
                if state["in_song"]:
                    state["silence_counter"] += 1
                    print("s", end="", flush=True)
                    
                    if state["silence_counter"] >= SILENCE_LIMIT:
                        print(f"\n[ STOPPED ] {SILENCE_LIMIT}s silence limit reached.")
                        publish_state("stopped")
                        state["in_song"] = False
                        state["last_song"] = ""
                        state["silence_counter"] = 0
                        
            await asyncio.sleep(1) # Check state every 1 second

if __name__ == "__main__":
    try:
        asyncio.run(audio_monitor_loop())
    except KeyboardInterrupt:
        print("\nShutting down...")
        mqtt_client.loop_stop()