# Changelog

All notable changes to SpinSense are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/) and the project uses a 4-digit `MAJOR.MINOR.PATCH.MICRO` version scheme.

## [1.0.0.0] - 2026-06-01

### Added
- **Home Assistant mDNS/zeroconf discovery.** SpinSense advertises `_spinsense._tcp.local.` on the LAN (`gui/discovery.py`), so the companion HACS integration auto-discovers it with no broker. Advertising is toggleable and reconciled live whenever config is saved; failures are non-fatal.
- **`GET /api/status`** — returns the last engine status (`engine_active`, `status_msg`, `rms_level`, `track{title,artist,album,art_url,…}`), the contract the Home Assistant integration polls. Backed by a last-status cache in `ipc_manager` that defaults to a "stopped" payload before the engine reports.
- **Configurable listen port** via `SPINSENSE_PORT` (default **3313**, a nod to 33⅓ RPM); `EXPOSE 3313` in the Dockerfile. Required because mDNS needs `network_mode: host`, which removes port remapping.
- **Setup wizard "Home Assistant & Integrations" step** — independent **mDNS** (on by default, zero-config) and **MQTT** (opt-in) toggles, replacing the MQTT-only step.
- **History enrichment columns** — nullable `isrc`, `genre`, `release_year` on the `plays` table, populated best-effort from the recognition result, paving the way for listening analytics. Idempotent `ALTER TABLE` migration; old rows stay valid.
- First-party HACS integration repository: [ycsgc1/homeassistant-spinsense](https://github.com/ycsgc1/homeassistant-spinsense).
- Installation, Setup, and Usage sections in the README.

### Changed
- **The MQTT enable toggle is now live.** Flipping `MQTT.Enabled` in the wizard/Settings reconnects or tears down the broker connection via the config watcher — no engine restart.
- App HTML pages and `/static` JS/CSS are served `Cache-Control: no-cache` (revalidate; cheap 304s when unchanged) so a rebuild can never leave the browser executing a stale asset against fresh markup. `/art` and `/api` stay cacheable.

### Notes
- `MQTT.Discovery.Discovery_Topic` and `MQTT.Topics.*` remain hardcoded in the engine (unchanged from prior releases).
- Listening analytics ("Wrapped") and clean DB export/import are planned for a future release; the history schema is now ready for the former.

## [0.3.0.0] - 2026-05-27

### Added
- Setup Wizard at `/setup` — five-step guided onboarding (Welcome, Mic, Threshold calibration with live RMS preview, MQTT, Done). State persisted in `System.Setup_Wizard_State`. Auto-redirects from any page when state is `"pending"`. X-button dismiss leaves state as `"pending"` so the wizard returns on the next page load; "Skip setup" link sets state to `"skipped"` and stops the auto-redirect for good; "Save and finish" sets `"completed"`. A "Re-run setup wizard" link in Settings opens the wizard regardless of state.
- `GET /api/setup-state` returning `{"state": "..."}`.
- `POST /api/mqtt/test` — opens a short-lived paho client with a 3.5s timeout, returns `{"ok": true|false, "detail": "..."}`. The wizard's MQTT step pops an error modal with "Try again" / "Skip MQTT" on failure; Settings shows the result inline next to the new "Test connection" button.
- Three new tests covering `Setup_Wizard_State` defaults + literal validation.

### Changed
- **Engine: full Tier 3 hot-reload.** `core_engine.py` watches `config.json` mtime every 2s; on change it re-reads the file and dispatches by category. Audio thresholds update in place. MQTT broker changes cancel the in-flight connect task and reconnect with the new fields. Mic device changes raise `mic_change_event`; the audio loop tears down the `sd.InputStream` and rebuilds against the new device. **No container restart needed for any config change.**
- Settings page: dropped the "Restart required" banner. Save toast reads "Saved and applied" on success.
- FastAPI middleware gates `/`, `/history`, `/settings` (and any future non-API page) behind the wizard when `Setup_Wizard_State == "pending"`. `/api/*`, `/static/*`, `/art/*`, `/ws/*`, and `/setup` itself always pass through.

### Notes
- `MQTT.Discovery.Discovery_Topic` and `MQTT.Topics.*` are still hardcoded in the engine. Out of scope this pass; the Wizard and Settings continue to hide those fields.

## [0.2.0.0] - 2026-05-27

### Added
- Real Settings page at `/settings` covering Hardware (mic device dropdown sourced from `/api/devices`), Audio (volume threshold slider with a live RMS preview bar, song sample length, the two silence-interval fields), and MQTT broker auth (Host, Port, User, Password). Sticky save bar with dirty-state tracking, beforeunload guard, and a Saved/Error toast.
- Real History page at `/history` rendering all play history grouped by date headers (Today, Yesterday, then explicit dates). Infinite scroll via IntersectionObserver, 50 rows per page.
- `GET /api/plays?limit=N&offset=M` endpoint returning a paginated slice of the play history plus a total count. Default `limit=50`, capped at 100.
- `play_history.count_plays()` helper for the History page header chip.
- `gui/tests/test_config_round_trip.py` covering `config_manager` round-trip and pydantic validation rejection for invalid types. Three new pagination tests added to `gui/tests/test_play_history.py`.

### Changed
- `POST /api/config` now returns HTTP 400 with `{"status": "error", "detail": "<pydantic error>"}` when validation fails, instead of silently returning `{"status": "success"}` on failure. The Settings UI surfaces the detail as a red toast.
- `play_history.recent_plays()` gains an `offset` parameter (default 0) and the upper limit cap moves from 50 to 100. Existing callers are unaffected.
- `core/core_engine.py` reads the configured mic device from `Hardware.Mic_Device` instead of the non-existent `Audio.Input_Device` key. Mic selection has never actually taken effect before this fix — restart the container after picking a device for it to apply.

### Notes
- The engine reads `config.json` once at module import. Every Settings change requires a container restart (`docker compose restart`) to take effect — the Settings page surfaces this with a banner.
- `MQTT.Discovery.Discovery_Topic` and `MQTT.Topics.*` config fields remain unread by the engine (hardcoded in `core_engine.py`). Not exposed in the Settings UI to avoid misleading users; tracked as a future cleanup.

## [0.1.0.0] - 2026-05-26

### Added
- New sidebar-driven app shell (`gui/templates/_layout.html`) with Material 3 dark theme (Tailwind CDN, Inter + Outfit fonts, Material Symbols Outlined) and a live engine-status pill that reflects WebSocket state (Idle / Listening / Playing / Disconnected).
- Redesigned Dashboard at `/` with vinyl-centric Now Playing card, RMS input meter, System Health (Input Level in dB, Connection placeholder), and a Recent Plays list.
- SQLite-backed play history (`gui/play_history.py`) with album-art caching to 64x64 JPEG thumbnails under `$SPINSENSE_DATA_DIR/art/`.
- `GET /api/recent?limit=N` endpoint surfacing the most recent plays for the Dashboard and future History page (default 10, capped at 50).
- Stub routes and templates for `/history`, `/settings`, and `/setup` so the sidebar links work today and future phases can land as small, self-contained passes.
- `Pillow==10.3.0` dependency for album-art thumbnailing.
- `gui/tests/test_play_history.py` covering the `play_history` round-trip, ordering, art-path mutation, limit clamping, and the four `ipc_manager` de-dupe cases.

### Changed
- `gui/ipc_manager.py` now records new identifications to SQLite and schedules a fire-and-forget album-art download, with module-level de-dupe by track title.
- `gui/backend_main.py` initialises the play-history DB and the `art/` directory in the FastAPI lifespan and mounts `/art` as a static route alongside `/static`.
- `gui/static/styles.css` slimmed to vinyl rotation keyframes, the glass-panel utility, and engine-pill state styling. Most layout work moved to Tailwind in the templates.

### Removed
- Old monolithic `gui/templates/index.html` and `gui/static/app.js` (replaced by the new shell + `dashboard.html` + `shell.js` + `dashboard.js`).
- `POST /api/engine/start` and `POST /api/engine/stop` endpoints. The engine lifecycle now belongs entirely to the Docker container.
