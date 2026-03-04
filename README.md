# 💿 SpinSense
Integrate your analogue record player into your digital life. This tool uses audio recognition and MQTT to create a media player entity for Home Assistant to show the song currently spinning on your turntable. 

## ✨ Features
- Automatic ID: Powered by songrec (Shazam-compatible) for high-accuracy track recognition.

- Zero-Config Discovery: Automatically appears in Home Assistant as a media_player via MQTT Discovery.

- Multi-Arch Ready: Runs natively on Raspberry Pi (ARM) near your deck or on your main NAS (x64).

- Guided Onboarding: A built-in Web GUI to help you calibrate your "Silence vs. Music" thresholds.

## 🚀 How It Works
- SpinSense doesn't just "guess." It monitors the RMS volume of your input device. When the needle drops:

  - Detection: It identifies a rise in volume above your calibrated THRESHOLD.

  - Recognition: It captures a 10-second high-fidelity sample and identifies it.

  - Communication: It publishes the Artist, Album, and Title to your MQTT Broker.

  - Silence Logic: When the side ends or the record is stopped, it waits for a SILENCE_LIMIT before marking the player as Stopped.

## 🛠 Project Structure

This project is built to be modular and Docker-first:

/core: The Python-based recognition engine.

/gui: A lightweight Flask/FastAPI web interface for configuration.

/docker: Multi-arch build files for Pi and NAS compatibility.

## 🏗 Installation (Coming Soon)

