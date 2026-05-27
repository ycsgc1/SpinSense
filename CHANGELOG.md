# Changelog

All notable changes to SpinSense are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/) and the project uses a 4-digit `MAJOR.MINOR.PATCH.MICRO` version scheme.

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
