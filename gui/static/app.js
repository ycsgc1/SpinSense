document.addEventListener('DOMContentLoaded', () => {
    // --- 1. WebSocket Connection (Live Data) ---
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/live-status`;
    const ws = new WebSocket(wsUrl);

    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            
            if (data.type === 'live_status') {
                const payload = data.payload;

                // Update Volume Meter
                if (payload.rms_level !== undefined) {
                    document.getElementById('volume-text').innerText = payload.rms_level.toFixed(4);
                    let percent = Math.min((payload.rms_level / 0.05) * 100, 100);
                    document.getElementById('volume-bar').style.width = `${percent}%`;
                }

                // Update Track Metadata
                if (payload.track && payload.track.title !== "") {
                    // A song is currently identified!
                    document.getElementById('track-title').innerText = payload.track.title;
                    document.getElementById('track-artist').innerText = payload.track.artist || "Unknown Artist";
                    document.getElementById('track-album').innerText = payload.track.album || "Unknown Album";
                    
                    const artImg = document.getElementById('album-art');
                    if (payload.track.art_url) {
                        artImg.src = payload.track.art_url;
                    }
                } else if (payload.status_msg === "Listening") {
                    // Reset to default when waiting for a song
                    document.getElementById('track-title').innerText = "Waiting for drop...";
                    document.getElementById('track-artist').innerText = "Artist";
                    document.getElementById('track-album').innerText = "Album";
                    document.getElementById('album-art').src = "/static/placeholder.jpg";
                }

                // Update Engine Status Badge
                const badge = document.getElementById('engine-status-badge');
                if (payload.status_msg === "Playing") {
                    badge.innerText = "Engine: Playing";
                    badge.className = "badge success";
                } else {
                    badge.innerText = "Engine: Listening";
                    badge.className = "badge warning";
                }
            }
        } catch (error) {
            console.error("WebSocket payload error:", error);
        }
    };

    // --- 2. Load Config & Devices (Fixes the blank boxes) ---
    async function loadConfigAndDevices() {
        // A. Load Devices for Dropdown
        try {
            const devRes = await fetch('/api/devices');
            const devData = await devRes.json();
            const select = document.getElementById('mic-device');
            select.innerHTML = ''; 
            
            const defaultOpt = document.createElement('option');
            defaultOpt.value = "default";
            defaultOpt.textContent = "System Default";
            select.appendChild(defaultOpt);

            devData.devices.forEach(device => {
                const opt = document.createElement('option');
                opt.value = device.name;
                opt.textContent = device.name;
                select.appendChild(opt);
            });
        } catch (error) {
            console.error("Failed to load devices:", error);
        }

        // B. Load Configuration values into the form
        try {
            const confRes = await fetch('/api/config');
            const config = await confRes.json();
            
            if (config.Hardware && config.Hardware.Mic_Device) {
                document.getElementById('mic-device').value = config.Hardware.Mic_Device;
            }
            if (config.Audio) {
                if (config.Audio.Volume_Threshold) document.getElementById('vol-threshold').value = config.Audio.Volume_Threshold;
                if (config.Audio.Song_Sample_Length) document.getElementById('sample-len').value = config.Audio.Song_Sample_Length;
            }
            if (config.MQTT && config.MQTT.Broker) {
                if (config.MQTT.Broker.Host) document.getElementById('mqtt-host').value = config.MQTT.Broker.Host;
                if (config.MQTT.Broker.Port) document.getElementById('mqtt-port').value = config.MQTT.Broker.Port;
            }
        } catch (error) {
            console.error("Failed to load config:", error);
        }
    }

    // --- 3. Save Config Button ---
    document.getElementById('btn-save-config').addEventListener('click', async () => {
        // Gather all inputs and build the JSON object
        const configPayload = {
            Hardware: { Mic_Device: document.getElementById('mic-device').value },
            Audio: { 
                Volume_Threshold: parseFloat(document.getElementById('vol-threshold').value),
                Song_Sample_Length: parseFloat(document.getElementById('sample-len').value)
            },
            MQTT: {
                Broker: {
                    Host: document.getElementById('mqtt-host').value,
                    Port: parseInt(document.getElementById('mqtt-port').value)
                }
            }
        };

        // Send to backend
        await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(configPayload)
        });
        
        // Brief visual feedback
        const btn = document.getElementById('btn-save-config');
        btn.innerText = "Saved!";
        setTimeout(() => { btn.innerText = "Save Settings"; }, 2000);
    });

    // --- 4. Start/Stop Engine Buttons ---
    document.getElementById('btn-start').addEventListener('click', async () => {
        await fetch('/api/engine/start', { method: 'POST' });
    });

    document.getElementById('btn-stop').addEventListener('click', async () => {
        await fetch('/api/engine/stop', { method: 'POST' });
        const badge = document.getElementById('engine-status-badge');
        badge.innerText = "Engine: Stopped";
        badge.className = "badge stopped";
    });

    // Initialize everything on page load
    loadConfigAndDevices();
});