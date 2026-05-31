# Design: Home Assistant mDNS Discovery + History Schema Future-Proofing

**Date:** 2026-05-31
**Branch:** `feat/homeassistant-mdns-discovery`
**Target release:** 1.0

## Summary

Add zero-configuration Home Assistant integration to SpinSense via mDNS/zeroconf
service advertisement, so a companion HACS integration auto-discovers the device
on the LAN with no broker and no manual setup. The existing MQTT path is retained
and becomes an independent, opt-in choice in the setup wizard.

Alongside this, make two small forward-looking changes: (1) add a handful of
nullable enrichment columns to the play-history table so a future "listening
analytics / Wrapped" feature has rich data to work with (you cannot backfill
history you never recorded), and (2) make the web server's listen port
configurable, defaulting to `3313` (a nod to 33⅓ RPM), so it stops colliding
with the very common port `8000` — which matters now that mDNS requires host
networking.

The working HA integration already exists in a community fork
(`fwump38/SpinSense`, integration v0.1.23). This project will **bring a
first-party copy in-house** in a dedicated repo under the owner's control, with
attribution preserved.

## Scope

**In scope (this spec):**
- **B** — mDNS/zeroconf advertisement + `GET /api/status` + setup-wizard
  integration choice + configurable listen port.
- **In-house HA integration** — a dedicated `ycsgc1/homeassistant-spinsense`
  repo seeded from the fork, with attribution.
- **Light D** — additive, nullable history-schema columns for future analytics.
- **A (prerequisite/deployment)** — database persistence fix + host-networking +
  port changes documented for the Dockge stacks.

**Explicitly out of scope (deferred post-1.0):**
- **C** — the listening-analytics / "Spotify Wrapped" UI and queries.
- Clean database export/import for device migration.
- Full schema normalization (separate `artists` / `tracks` tables).

## Background: the integration contract

The fork's HACS integration (`custom_components/spinsense/`) is a *client*;
SpinSense is the *server*. The integration expects, from the SpinSense host:

1. **mDNS advertisement** of service type `_spinsense._tcp.local.` on the HTTP
   port. (Declared in the integration's `manifest.json` `zeroconf` key. Its
   `async_step_zeroconf` reads `discovery_info.host`/`.port` and auto-creates the
   config entry — so it is port-agnostic via discovery.)
2. **`GET /api/status`** → HTTP 200 JSON of shape:
   ```json
   {
     "engine_active": false,
     "status_msg": "stopped",
     "rms_level": 0.0,
     "track": { "title": "", "artist": "", "album": "", "art_url": "" }
   }
   ```
   Used for config-flow connection validation, initial state, and reconnect
   fallback.
3. **`WS /ws/live-status`** pushing frames of shape
   `{"type": "live_status", "payload": { …same as /api/status… }}`.
4. **`track.art_url`** as an absolute `http(s)` URL for album art.

### Current state vs. contract

| Contract item | Status today | Action |
| --- | --- | --- |
| mDNS `_spinsense._tcp.local.` | ❌ none | **Build** advertiser |
| `GET /api/status` | ❌ endpoint absent | **Build** endpoint |
| `WS /ws/live-status` payload | ✅ already emits the exact shape (`core/core_engine.py:509-516`) | **No change** |
| `track.art_url` absolute URL | ✅ stored in DB `art_url` column | **No change** (verify it is the remote URL, not the local `art_path`) |

The status-message casing already aligns: the engine emits `"Playing"` /
`"Listening"`; the integration lowercases and matches `"playing"` → PLAYING,
`"stopped"` → OFF, else IDLE.

## Architecture

SpinSense runs two processes (launched by `docker/entrypoint.sh`):

- **GUI** — FastAPI + uvicorn, serves HTTP and `/ws/live-status`. This is the
  endpoint the HA integration connects to.
- **Core engine** — `core/core_engine.py`; captures audio, recognizes tracks,
  optionally publishes MQTT, and pushes `live_status` frames to the GUI over a
  Unix domain socket. `gui/ipc_manager.py` receives those frames and broadcasts
  them to WebSocket clients.

The mDNS advertiser therefore lives in the **GUI process**, bound to FastAPI's
startup/shutdown lifecycle, because it advertises the HTTP port. The core engine
is untouched by discovery; it only gains the schema-capture change.

```
                       mDNS (_spinsense._tcp, port 3313)
  Home Assistant  <───────────────────────────────────────  GUI (FastAPI)
   (HACS integ.)  ──HTTP GET /api/status──────────────────>   ├─ discovery.py (zeroconf)
                  ──WS  /ws/live-status────────────────────>   ├─ /api/status (last-status cache)
                                                                └─ ipc_manager (caches last frame)
                                                                       ▲ UDS
                                                                Core engine ──> SQLite (plays + new cols)
```

## Components

### 1. mDNS advertiser — `gui/discovery.py` (new)

- Wraps `python-zeroconf` using `AsyncZeroconf`.
- Public surface:
  - `async def start(config) -> None` — register the service if enabled.
  - `async def stop() -> None` — unregister and close.
  - `async def reconcile(config) -> None` — start/stop/refresh to match config
    (called after a config save so toggling mDNS needs no restart).
- Builds a `ServiceInfo`:
  - type `_spinsense._tcp.local.`
  - instance name from `Discovery.mDNS.Service_Name` (default derived from the
    system hostname, e.g. `"SpinSense (<hostname>)"`)
  - port from `SPINSENSE_PORT` env (default `3313`)
  - TXT properties: `{ "version": "<app version>", "path": "/" }`
  - addresses: let zeroconf enumerate interface addresses (host networking).
- **Failure is non-fatal.** Any zeroconf error (bind failure, no network, UDP
  5353 conflict) is logged at WARNING and swallowed — the web app keeps serving.
- Wired into FastAPI lifespan: `start()` on startup, `stop()` on shutdown.

### 2. Status endpoint — `GET /api/status` (in `gui/backend_main.py`)

- Returns the most recent `live_status` *payload object* (the inner `payload`,
  not the envelope).
- Backed by a **last-status cache** added to `gui/ipc_manager.py`'s
  `ConnectionManager`: every broadcast updates `manager.last_status`.
- If no frame has been received yet (engine stopped / never started), return the
  default stopped payload:
  `{engine_active: false, status_msg: "stopped", rms_level: 0.0, track: {title:"",artist:"",album:"",art_url:""}}`.
- Read-only, no auth (parity with existing `/api/recent`, `/api/plays`).

### 3. Configurable listen port

- `docker/entrypoint.sh:16`: `--port 8000` → `--port ${SPINSENSE_PORT:-3313}`.
- `gui/discovery.py` reads the same `SPINSENSE_PORT` (default `3313`) so the
  advertised port always matches the bound port.
- `docker/Dockerfile`: add `EXPOSE 3313` (documentation only; irrelevant under
  host networking).
- Rationale: host networking (required for mDNS, see Deployment) removes port
  remapping, so the app's own bind port is what is exposed. Defaulting to `3313`
  keeps the vinyl-speed brand and frees `8000` for other containers.
- Risk: low. `8000` is hardcoded in exactly one place (`entrypoint.sh`); the
  frontend is same-origin and hardcodes no port; internal IPC is over Unix
  sockets, not TCP.

### 4. Setup wizard — Home Assistant & Integrations step

Replaces the current "MQTT Setup" step (`gui/templates/setup.html`,
`gui/static/setup.js`) with an **"Home Assistant & Integrations"** step containing
two **independent** toggles:

- **mDNS auto-discovery** — default **ON**. Short copy explaining zero-click HA
  discovery and a link to install the SpinSense HACS integration.
- **MQTT** — default **OFF**. When enabled, reveals the existing broker fields
  (host / port / user / password / Test connection). Behaves exactly as the
  current MQTT step does today.

The user can enable one, both, or neither. Saving the wizard writes the
corresponding config and triggers `discovery.reconcile()`.

### 5. Config schema — `gui/config_manager.py`

Add a `Discovery` section and an explicit MQTT enable flag:

```text
Discovery:
  mDNS:
    Enabled:      bool = true
    Service_Name: str  = ""     # empty => derive from hostname at runtime
MQTT:
  Enabled: bool = false         # NEW explicit toggle for the wizard switch
  Broker:   { … existing … }
  Discovery:{ … existing HA-over-MQTT discovery settings … }
  Topics:   { … existing … }
```

- `MQTT.Enabled` makes the engine's MQTT activation explicit (today it is
  inferred). The engine reads this flag to decide whether to connect.
- Pydantic validation + defaults preserve backward compatibility: missing keys in
  an existing `config.json` fall back to defaults on load.

### 6. History schema future-proofing — `gui/play_history.py`

Keep the `plays` table; add **nullable** columns via guarded
`ALTER TABLE ADD COLUMN` inside `init_db()` (idempotent — inspect
`PRAGMA table_info(plays)` and add only missing columns):

- `isrc TEXT` — stable international recording identity (robust grouping key for
  future analytics; far better than matching on title/artist strings).
- `genre TEXT`
- `release_year INTEGER`

`record_play()` gains matching optional keyword params, populated **best-effort**
from the recognition result when present (all nullable; a missing field never
fails a write). The exact `shazamio` response fields to map will be confirmed
during implementation; if a field is unavailable it simply stays `NULL`.

Old rows remain valid with `NULL` in the new columns. No data migration, no
analytics UI in this spec — this only ensures richer data starts accumulating now.

### 7. First-party HA integration — `ycsgc1/homeassistant-spinsense` (new repo)

- Dedicated repository (HACS expects `custom_components/<domain>/` + `hacs.json`
  at the repo root), seeded from `fwump38/SpinSense`'s `custom_components/spinsense/`.
- Attribution preserved: keep `fwump38` credited in `codeowners`/README; add the
  project owner; repoint `documentation` and `issue_tracker` URLs.
- Set `DEFAULT_PORT = 3313` in `const.py` for the manual-entry fallback. (The
  zeroconf discovery path already uses the advertised port, so auto-discovery is
  unaffected by this default.)
- Versioning, branding, and HACS listing become first-party going forward.
- This repo is created/seeded as part of the work but is a separate deliverable
  from the SpinSense app changes; it has no code dependency on the app beyond the
  HTTP/WS contract above.

## Deployment changes (feature A + host networking)

These apply to the user's Dockge stacks; the in-repo `docker-compose.yml` is
updated to match as the reference example.

### Database persistence (priority-zero bug fix)

Today the user's Dockge compose sets neither `SPINSENSE_DATA_DIR` nor a volume,
so the SQLite DB defaults to `/app/spinsense.db` **inside the container's
writable layer** (`gui/play_history.py:9-13`) and is destroyed on every
`build --no-cache && up -d`. Fix:

```yaml
environment:
  - SPINSENSE_DATA_DIR=/app/data
volumes:
  - ./data:/app/data
```

**One-time rescue** of existing history before recreating the main container
(the stopped container still holds the data in its layer):

```bash
docker cp spinsense:/app/spinsense.db ./data/spinsense.db
docker cp spinsense:/app/art ./data/art   # if album-art cache exists
```

### Host networking + port (for mDNS)

mDNS multicast (`224.0.0.251:5353`) does not cross Docker bridge networking to
the LAN, so HA cannot see a bridged container. The container must use
`network_mode: host`. Under host networking the `ports:` mapping is dropped and
the app binds its own port directly on the host — hence the configurable
`SPINSENSE_PORT=3313`.

Reference **test** stack:

```yaml
services:
  spinsense-dev:
    container_name: spinsense-dev
    build:
      context: https://github.com/ycsgc1/SpinSense.git#feat/homeassistant-mdns-discovery
      dockerfile: docker/Dockerfile
    network_mode: host
    devices:
      - /dev/snd:/dev/snd
    group_add:
      - audio
    environment:
      - SPINSENSE_DATA_DIR=/app/data
      - SPINSENSE_PORT=3313
    volumes:
      - ./data-dev:/app/data
    restart: "no"
```

### avahi / port 5353 caveat

If the host already runs `avahi-daemon` (binds UDP 5353), `python-zeroconf`
generally coexists via socket reuse. If a conflict occurs, fallbacks (documented,
not implemented now): publish through the host's avahi instead of in-container
zeroconf, or run the container's zeroconf on the host's mDNS stack. The advertiser
must never crash the app if 5353 is unavailable (see Error handling).

## Error handling

- **mDNS registration failure** — caught, logged WARNING, app continues serving
  HTTP/WS. Discovery is a best-effort enhancement, never a hard dependency.
- **`/api/status` with no engine data** — returns the well-formed "stopped"
  default rather than an error, so the HA integration shows an OFF (not
  unavailable) entity.
- **Recognition enrichment missing** (no ISRC/genre/year) — columns stay `NULL`;
  the write still succeeds.
- **Config backward compatibility** — unknown/missing config keys default via
  Pydantic; existing `config.json` files load unchanged.

## Testing

- `gui/discovery.py` — unit-test the config→`ServiceInfo` mapping (type, port,
  instance name, TXT) and the `reconcile()` enable/disable/refresh logic without
  binding the network (inject/mox the zeroconf registrar).
- `GET /api/status` — FastAPI `TestClient`: (a) returns the cached payload after
  a simulated broadcast, (b) returns the stopped default when the cache is empty.
- Schema — `init_db()` adds the new columns idempotently (run twice, assert no
  error and columns present); `record_play()` round-trips the new fields; an old
  row with `NULL`s reads back cleanly.
- Config — extend `gui/tests/test_config_round_trip.py` for `Discovery.mDNS.*`
  and `MQTT.Enabled` (defaults + persistence).
- Port — assert `entrypoint.sh` honors `SPINSENSE_PORT` (shell-level/manual) and
  that `discovery.py` advertises the same value the server binds.

## Dependencies

- Add `zeroconf` to `requirements.txt` (pin a current version during
  implementation).

## Open items / follow-ups (not this spec)

- Listening analytics / "Wrapped" UI (feature C) — will consume the new columns.
- Clean DB export/import for device migration.
- Decide whether to also expose enrichment fields over `/api/status` / WS later
  (the HA media_player does not need them today).
