document.addEventListener('DOMContentLoaded', () => {
    // --- 1. Connect to the WebSocket ---
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/live-status`;
    const ws = new WebSocket(wsUrl);

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        
        if (data.type === 'live_status') {
            const payload = data.payload;

            // Update Volume Meter
            if (payload.rms_level !== undefined) {
                document.getElementById('volume-text').innerText = payload.rms_level.toFixed(4);
                // Scale the visual bar (Assuming 0.05 is a loud peak for RMS)
                let percent = Math.min((payload.rms_level / 0.05) * 100, 100);
                document.getElementById('volume-bar').style.width = `${percent}%`;
            }

            // Update Track Metadata
            if (payload.track) {
                document.getElementById('track-title').innerText = payload.track.title || "Listening...";
                document.getElementById('track-artist').innerText = payload.track.artist || "Artist";
                document.getElementById('track-album').innerText = payload.track.album || "Album";
                
                const artImg = document.getElementById('album-art');
                if (payload.track.art_url) {
                    artImg.src = payload.track.art_url;
                } else {
                    // Fallback to placeholder if no art is found
                    artImg.src = "/static/placeholder.jpg"; 
                }
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
    };

    // --- 2. Populate Microphone Dropdown ---
    async function loadDevices() {
        try {
            const response = await fetch('/api/devices');
            const data = await response.json();
            const select = document.getElementById('mic-device');
            
            // Clear existing options
            select.innerHTML = ''; 
            
            // Add a default option
            const defaultOpt = document.createElement('option');
            defaultOpt.value = "default";
            defaultOpt.textContent = "System Default";
            select.appendChild(defaultOpt);

            // Add available mics
            data.devices.forEach(device => {
                const opt = document.createElement('option');
                opt.value = device.name;
                opt.textContent = device.name;
                select.appendChild(opt);
            });
        } catch (error) {
            console.error("Failed to load audio devices:", error);
        }
    }

    // --- 3. Start/Stop Engine Buttons ---
    document.getElementById('btn-start').addEventListener('click', async () => {
        await fetch('/api/engine/start', { method: 'POST' });
    });

    document.getElementById('btn-stop').addEventListener('click', async () => {
        await fetch('/api/engine/stop', { method: 'POST' });
        document.getElementById('engine-status-badge').innerText = "Engine: Stopped";
        document.getElementById('engine-status-badge').className = "badge stopped";
    });

    // Initialize the page
    loadDevices();
});