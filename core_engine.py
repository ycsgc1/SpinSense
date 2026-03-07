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
from shazamio import Shazam

# --- 1. Load Configuration ---
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
with open(CONFIG_PATH, 'r') as f:
    config = json.load(f)

THRESHOLD = config.get('Audio', {}).get('Volume_Threshold', 0.015)
SILENCE_LIMIT = config.get('Audio', {}).get('Song_Sample_Length', 10) 
SAMPLE_LEN = config.get('Audio', {}).get('Song_Sample_Length', 10)

MIC_DEVICE = config.get('Audio', {}).get('Input_Device', None)
if MIC_DEVICE == "" or MIC_DEVICE == "default":
    MIC_DEVICE = None 

MQTT_HOST = config.get('MQTT', {}).get('Broker', {}).get('Host', '192.168.1.100')
MQTT_USER = config.get('MQTT', {}).get('Broker', {}).get('User', 'vinylrecord')
MQTT_PASS = config.get('MQTT', {}).get('Broker', {}).get('Password', '')
MQTT_PORT = config.get('MQTT', {}).get('Broker', {}).get('Port', 1883)

BASE_TOPIC = "home/vinyl"
TOPIC_STATE = f"{BASE_TOPIC}/state"
TOPIC_TITLE = f"{BASE_TOPIC}/title"
TOPIC_ARTIST = f"{BASE_TOPIC}/artist"
TOPIC_ARTART = f"{BASE_TOPIC}/album_art"
LEGACY_TOPIC = f"{BASE_TOPIC}/now_playing"
DISCOVERY_TOPIC = "homeassistant/media_player/vinyl_pi/config"

# --- 2. MQTT Setup ---
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
if MQTT_USER and MQTT_PASS:
    mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)

MQTT_ENABLED = False
print(f"Attempting to connect to MQTT at {MQTT_HOST}...")
try:
    mqtt_client.connect(MQTT_HOST, MQTT_PORT, 3) 
    mqtt_client.loop_start()
    MQTT_ENABLED = True
    print("✅ MQTT Connected!")
except Exception as e:
    print("⚠️ Running in OFFLINE TESTING MODE (MQTT messages will print to console).")

def announce_to_ha():
    payload = {
        "name": "Vinyl Record Player",
        "unique_id": "vinyl_pi_record_player",
        "device_class": "speaker",
        "state_topic": TOPIC_STATE, 
        "json_attributes_topic": LEGACY_TOPIC, 
        "payload_play": "playing",
        "payload_stop": "stopped",
        "payload_idle": "idle"
    }
    if MQTT_ENABLED:
        mqtt_client.publish(DISCOVERY_TOPIC, json.dumps(payload), retain=True)

def publish_state(status, artist="", title="", album="", art_url=""):
    if MQTT_ENABLED:
        mqtt_client.publish(TOPIC_STATE, status, retain=True)
        mqtt_client.publish(TOPIC_TITLE, title, retain=True)
        mqtt_client.publish(TOPIC_ARTIST, artist, retain=True)
        mqtt_client.publish(TOPIC_ARTART, art_url, retain=True)
        payload = json.dumps({"status": status, "artist": artist, "title": title, "album": album, "art_url": art_url})
        mqtt_client.publish(LEGACY_TOPIC, payload, retain=True)
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
    "current_rms": 0.0
}

async def fetch_itunes_metadata(artist, title):
    """Hits the iTunes API to get the high-res art and album name."""
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
                        # Swap 100x100 for 1000x1000 for crisp artwork
                        art_url = result.get("artworkUrl100", "").replace("100x100bb", "1000x1000bb")
                        return album, art_url
    except Exception as e:
        print(f"⚠️ iTunes API error: {e}")
    return None, None

async def recognize_audio():
    print(f"\n[!] Music detected. Recording {SAMPLE_LEN}s for identification...")
    recording = sd.rec(int(SAMPLE_LEN * 48000), samplerate=48000, channels=1, dtype='int16', device=MIC_DEVICE)
    sd.wait() 
    
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
        
        # Fallbacks just in case iTunes draws a blank
        if not art_url:
            art_url = track.get('images', {}).get('coverarthq', track.get('images', {}).get('coverart', ''))
        if not album:
            album = "Unknown Album"
            
        result_str = f"{artist} - {title}"
        
        # Update the state dictionary so the socket picks it up immediately
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
    announce_to_ha()
    print(f"--- VINYL SCROBBLER ALPHA ACTIVE ---")
    
    def audio_callback(indata, frames, time, status):
        rms = np.sqrt(np.mean(indata**2))
        state["current_rms"] = float(rms)

    stream = sd.InputStream(samplerate=48000, channels=1, callback=audio_callback, device=MIC_DEVICE)
    with stream:
        while True:
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
                                "art_url": state.get("art_url", "")
                            }
                        }
                    }) + "\n"
                    writer.write(payload.encode())
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
            except Exception:
                pass 
            
            if vol > THRESHOLD:
                if not state["in_song"] or state["silence_counter"] > 0:
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
                        state["artist"] = ""
                        state["title"] = ""
                        state["album"] = ""
                        state["art_url"] = ""
                        state["silence_counter"] = 0
                        
            await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(audio_monitor_loop())
    except KeyboardInterrupt:
        print("\nShutting down...")
        if MQTT_ENABLED:
            mqtt_client.loop_stop()