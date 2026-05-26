# Changelog

All notable changes to SpinSense are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/) and the project uses a 4-digit `MAJOR.MINOR.PATCH.MICRO` version scheme.

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
