# 💿 SpinSense
Integrate your analogue record player into your digital life. This tool uses audio recognition and MQTT to create a media player entity for Home Assistant to show the song currently spinning on your turntable. 

## ✨ Features
- Automatic ID: Powered by songrec (Shazam-compatible) for high-accuracy track recognition.

- Zero-Config Discovery: Auto-appears in Home Assistant as a media_player — either via mDNS (no broker, recommended) or MQTT Discovery.

- Multi-Arch Ready: Runs natively on Raspberry Pi (ARM) near your deck or on your main NAS (x64).

- Guided Onboarding: A built-in Web GUI to help you calibrate your "Silence vs. Music" thresholds.

## 🚀 How It Works
- SpinSense doesn't just "guess." It monitors the RMS volume of your input device. When the needle drops:

  - Detection: It identifies a rise in volume above your calibrated THRESHOLD.

  - Recognition: It captures a 10-second high-fidelity sample and identifies it.

  - Communication: It publishes the Artist, Album, and Title to your MQTT Broker.

  - Silence Logic: When the side ends or the record is stopped, it waits for a SILENCE_LIMIT before marking the player as Stopped.

## 🏠 Home Assistant

SpinSense offers two independent integration paths, chosen during the setup wizard:

### mDNS auto-discovery (recommended, zero-config, no broker)

SpinSense advertises itself on your LAN as `_spinsense._tcp.local.`. Install the
companion **SpinSense HACS integration** ([ycsgc1/homeassistant-spinsense](https://github.com/ycsgc1/homeassistant-spinsense))
and Home Assistant discovers the device automatically — it appears under
**Settings → Devices & Services → Discovered** as a `media_player` (Turn Table)
that reflects what's currently spinning. The integration polls `GET /api/status`
and streams `ws://<host>:3313/ws/live-status`.

> **Requires host networking.** mDNS multicast does not cross Docker bridge
> networking, so the container must run with `network_mode: host`. Under host
> mode the app binds `SPINSENSE_PORT` (default **3313**) directly on the host —
> there is no `ports:` mapping. See `docker-compose.yml`.

### MQTT (advanced)

Prefer a broker? Enable MQTT in the wizard and SpinSense publishes track state
for Home Assistant's MQTT integration to pick up via MQTT Discovery. mDNS and
MQTT can be enabled independently — one, both, or neither.

### Data persistence

The play history and album-art cache live in a SQLite database under
`SPINSENSE_DATA_DIR` (`/app/data`). Keep the `./data:/app/data` volume mount so
your history survives container rebuilds and updates.

## 🛠 Project Structure

This project is built to be modular and Docker-first:

/core: The Python-based recognition engine.

/gui: A lightweight Flask/FastAPI web interface for configuration.

/docker: Multi-arch build files for Pi and NAS compatibility.

## 🏗 Installation (Coming Soon)

