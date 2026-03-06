console.log("1. app.js file loaded successfully!");

document.addEventListener("DOMContentLoaded", () => {
    console.log("2. Web page elements loaded. Starting connections...");
    
    // --- 1. WebSocket Connection ---
    const wsUrl = `ws://${window.location.host}/ws/live-status`;
    console.log("3. Attempting to connect to WebSocket at:", wsUrl);
    
    const socket = new WebSocket(wsUrl);
    window.socket = socket; // This exposes 'socket' so your console command works!

    socket.onopen = () => console.log("✅ WebSocket Connected!");
    socket.onerror = (err) => console.error("❌ WebSocket Error:", err);
    socket.onclose = () => console.log("⚠️ WebSocket Closed.");

    const volBar = document.getElementById('volume-bar');
    const volText = document.getElementById('volume-text');
    const trackTitle = document.getElementById('track-title');

    socket.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === "live_status") {
            const payload = data.payload;
            let percent = Math.min(payload.rms_level * 1000, 100);
            
            if (volBar) volBar.style.width = `${percent}%`;
            if (volText) volText.textContent = payload.rms_level.toFixed(4);
            if (trackTitle) trackTitle.textContent = payload.track.title;
        }
    };

    // --- 2. Load Devices ---
    console.log("4. Fetching audio devices...");
    fetch('/api/devices')
        .then(res => {
            if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
            return res.json();
        })
        .then(data => {
            console.log("✅ Devices loaded:", data);
            const select = document.getElementById('mic-device');
            data.devices.forEach(dev => {
                const opt = document.createElement('option');
                opt.value = dev.name;
                opt.textContent = dev.name;
                select.appendChild(opt);
            });
        })
        .catch(err => console.error("❌ Failed to load devices:", err));

    // --- 3. Load Config ---
    console.log("5. Fetching configuration...");
    fetch('/api/config')
        .then(res => {
            if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
            return res.json();
        })
        .then(config => {
            console.log("✅ Config loaded:", config);
            document.getElementById('vol-threshold').value = config.Audio.Volume_Threshold;
            document.getElementById('sample-len').value = config.Audio.Song_Sample_Length;
            document.getElementById('mqtt-host').value = config.MQTT.Broker.Host;
            document.getElementById('mqtt-port').value = config.MQTT.Broker.Port;
            
            // Wait 100ms for the device dropdown to populate before setting its value
            setTimeout(() => {
                const micSelect = document.getElementById('mic-device');
                if (micSelect && micSelect.querySelector(`option[value="${config.Hardware.Mic_Device}"]`)) {
                    micSelect.value = config.Hardware.Mic_Device;
                }
            }, 100);
        })
        .catch(err => console.error("❌ Failed to load config:", err));

        // --- 4. Save Configuration ---
    const btnSave = document.getElementById('btn-save-config');
    btnSave.addEventListener('click', async (e) => {
        e.preventDefault(); // Prevents the page from refreshing
        btnSave.textContent = "Saving...";

        try {
            // 1. Fetch the current config so we don't overwrite hidden variables
            const res = await fetch('/api/config');
            const config = await res.json();

            // 2. Update it with values from our UI
            config.Hardware.Mic_Device = document.getElementById('mic-device').value;
            config.Audio.Volume_Threshold = parseFloat(document.getElementById('vol-threshold').value);
            config.Audio.Song_Sample_Length = parseFloat(document.getElementById('sample-len').value);
            config.MQTT.Broker.Host = document.getElementById('mqtt-host').value;
            config.MQTT.Broker.Port = parseInt(document.getElementById('mqtt-port').value);

            // 3. POST the updated config back to the server
            const saveRes = await fetch('/api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(config)
            });

            if (saveRes.ok) {
                btnSave.textContent = "Saved!";
                btnSave.classList.replace("primary", "success"); // Turns button green
                
                // Reset button after 2 seconds
                setTimeout(() => {
                    btnSave.textContent = "Save Settings";
                    btnSave.classList.replace("success", "primary");
                }, 2000);
            }
        } catch (err) {
            console.error("❌ Failed to save config:", err);
            btnSave.textContent = "Error!";
            btnSave.classList.replace("primary", "danger");
        }
    });

    // --- 5. Engine Controls ---
    const badge = document.getElementById('engine-status-badge');

    document.getElementById('btn-start').addEventListener('click', async () => {
        const res = await fetch('/api/engine/start', { method: 'POST' });
        if (res.ok) {
            badge.textContent = "Engine: Active";
            badge.className = "badge active"; // Turns badge green
        }
    });

    document.getElementById('btn-stop').addEventListener('click', async () => {
        const res = await fetch('/api/engine/stop', { method: 'POST' });
        if (res.ok) {
            badge.textContent = "Engine: Stopped";
            badge.className = "badge stopped"; // Turns badge grey
        }
    });
});