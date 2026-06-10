# ⚙️ Configuration reference

Every setting below lives on the **Settings** page of the SpinSense web UI (`http://<your-host-ip>:3313` → **Settings**). Changes are **saved live** — the running engine hot-reloads them, no restart needed.

Settings are persisted to `config.json` under your data directory (`SPINSENSE_DATA_DIR`, e.g. `./data/config.json`). You can edit that file directly if you prefer, but the UI is the recommended way. Secrets (MQTT password, AudD token) are stored in plaintext — fine for a self-hosted LAN box; just don't commit `config.json` anywhere public.

---

## Audio

How SpinSense tells music from silence, how it samples for recognition, and which recognizers it uses.

| Setting | What it does | Default | Range |
|---|---|---|---|
| **Volume threshold** | The loudness above which SpinSense decides music is playing and starts identifying. Set it just above the noise floor of a silent spinning record — too low and silence triggers scans, too high and quiet passages look like silence. Edited in **dB** in the UI; stored as linear RMS (`Audio.Volume_Threshold`) in `config.json`. | ≈ −40 dB (`0.01`) | −120 … 0 dB |
| **Song sample length** | How many seconds of audio to record before sending it to the recognizer. ~5 s is usually plenty; longer can help on hard-to-identify tracks but makes each recognition slower. | 5 s | 1 … 60 s |
| **Rescan wait interval** | When a track can't be identified, SpinSense waits this many seconds before trying again, recording a longer sample each time (1×, then 2×, then 3× the sample length). Longer waits sample more of the song; shorter retries faster. | 5 s | 0 … 60 s |
| **New-song silence interval** | How long a quiet gap must last before SpinSense treats the next audio as a **new song** rather than a continuation — roughly the gap between tracks on a record. Also how long a failed/unidentifiable track stays "backed off" before a fresh gap re-arms scanning. | 3 s | 1 … 600 s |
| **Stopped silence interval** | How long silence must persist before SpinSense marks the record **stopped** and clears "now playing". Longer tolerates quiet passages within a song; shorter marks "stopped" sooner after the needle lifts. Keep this ≥ the new-song interval. | 5 s | 1 … 600 s |
| **Re-announce each track to Home Assistant** | When on, each new song briefly drops Home Assistant to idle before playing again (over both the integration and MQTT), so automations that trigger on "started playing" re-fire on every track. Off keeps playback smooth, with no idle blip. | Off | toggle |
| **Backup recognizer** | A second recognizer to try when the primary (Shazam) can't identify a track on its first attempt. See [Backup recognizers](#backup-recognizers) below. | None | None / AudD / AcoustID |
| **AudD API token** | Your API token from [audd.io](https://dashboard.audd.io/) — only needed when **Backup recognizer** is set to AudD. | *(empty)* | string |

> **Tip — tuning the silence intervals.** If a track with quiet passages keeps getting re-identified mid-song, raise **New-song silence interval**. If the player lingers on "now playing" too long after a side ends, lower **Stopped silence interval**. The recommended ordering is `New-song ≤ Stopped`.

### Backup recognizers

Shazam is always the **primary** recognizer (free, on by default — nothing to configure). The **Backup recognizer** dropdown adds a fallback for the small fraction of tracks Shazam can't get on the first try:

**How it works:** Shazam tries the first sample → if it misses, the chosen backup tries that **same** sample → if the backup also misses, Shazam continues its longer-sample retries (2×, 3×). The backup fires at most once per recognition, so it stays frugal.

| Option | Cost | Setup | Notes |
|---|---|---|---|
| **None** | — | none | Shazam only (default). |
| **AudD** | 300 free requests, then pay-as-you-go | Paste an API token (below) | Large catalog, strong on noisy clips. Only spends a request on a Shazam miss. |
| **AcoustID** | **Free** | **Nothing** — a key ships built-in | On-device Chromaprint fingerprint matched against the MusicBrainz-linked AcoustID database. A different catalog from Shazam, so a genuine second shot. |

**Using AudD:**
1. Sign up at **[dashboard.audd.io/signup](https://dashboard.audd.io/signup)** (no card; 300 free requests).
2. Copy your `api_token` from the dashboard.
3. Settings → **Backup recognizer → AudD**, paste the token into **AudD API token**, **Save**.

**Using AcoustID:** just set **Backup recognizer → AcoustID** and **Save**. Nothing else — the AcoustID app key is bundled. *(Advanced: override it with the `SPINSENSE_ACOUSTID_KEY` environment variable if you want to use your own.)*

Either way, album art is always fetched high-res from iTunes by artist + title, so artwork quality is the same regardless of which recognizer matched.

---

## Hardware

| Setting | What it does | Default |
|---|---|---|
| **Microphone** | The audio input device receiving your turntable's signal (e.g. a USB audio interface). The list comes from devices the container can see; **System default** works if you only have one. The setup wizard helps you pick and calibrate it. | System default |

---

## Home Assistant & discovery

SpinSense can be discovered two independent ways — run one, both, or neither.

### mDNS (recommended, zero-config)

Advertises SpinSense on the LAN so the [companion HACS integration](https://github.com/ycsgc1/homeassistant-spinsense) auto-discovers it — no broker, no IP to type.

| Setting | What it does | Default |
|---|---|---|
| **mDNS discovery** | Advertise the `_spinsense._tcp` service for Home Assistant auto-discovery. Requires `network_mode: host` (multicast doesn't cross Docker's bridge network). | On |
| **Service name** | The name shown during discovery. Empty derives one from the host's hostname. | *(hostname)* |

### MQTT (advanced)

Publishes track state to your own MQTT broker; Home Assistant's MQTT integration then picks SpinSense up via MQTT discovery. Off by default.

| Setting | What it does | Default |
|---|---|---|
| **Enable MQTT** | Connect to the broker and publish state. | Off |
| **Host** | Broker hostname or IP. | `127.0.0.1` |
| **Port** | Broker port. | `1883` |
| **Username** / **Password** | Broker credentials (leave blank for anonymous). Password stored in plaintext in `config.json`. | *(empty)* |

The Settings page has a **Test connection** button to verify host/port/credentials before saving.

---

## Environment variables

Set in your `docker compose` file (the `environment:` block). These are host/deployment settings, not part of `config.json`.

| Variable | What it does | Default |
|---|---|---|
| `SPINSENSE_PORT` | Web UI / API port the app binds (a nod to 33⅓ RPM). Under `network_mode: host` the app binds this directly on the host. | `3313` |
| `SPINSENSE_DATA_DIR` | Where `config.json`, the SQLite history database, and the album-art cache live. Mount a volume here to persist data across rebuilds. | `/app/data` |
| `SPINSENSE_ACOUSTID_KEY` | *(Advanced)* Override the bundled AcoustID application key with your own (from [acoustid.org/new-application](https://acoustid.org/new-application)). Most users never need this. | *(bundled key)* |

---

Back to the [README](../README.md).
