import sounddevice as sd

def get_audio_devices():
    """Queries the host OS for available audio input devices."""
    devices = []
    try:
        device_list = sd.query_devices()
        for idx, dev in enumerate(device_list):
            # Only return devices that have input channels (microphones)
            if dev.get('max_input_channels', 0) > 0:
                devices.append({
                    "index": idx,
                    "name": dev.get('name', f"Unknown Device {idx}")
                })
    except Exception as e:
        print(f"⚠️ Error querying audio devices: {e}")
        # Failsafe so the API doesn't crash if drivers are missing during dev
        devices = [{"index": 0, "name": "default"}]
        
    return devices