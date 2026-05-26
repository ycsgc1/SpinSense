# Frontend Shell + Dashboard Redesign (Phase 1)

**Date:** 2026-05-26
**Status:** Approved, ready for implementation planning
**Scope:** First of four passes that re-imagine the SpinSense frontend based on the Stitch design mockups in `stitch/`.

## Context

Today's `gui/templates/index.html` is a single-page Flask/FastAPI app: a header with engine start/stop, a Live Playback card (volume meter + now-playing), and an inline configuration form. It works, but it doesn't scale to the four surfaces the Stitch mockups introduce: a sidebar-driven app shell, a Dashboard, a History list, a Settings page, and a first-run Setup Wizard.

This spec covers Phase 1 only: the shared app shell and the redesigned Dashboard. Phases 2–4 (Settings, History, Setup Wizard) are explicitly out of scope here and ship as their own follow-up passes. The phasing is driven by the user's preference to spread token use across multiple sessions rather than land the whole redesign at once.

## Goals

- Replace today's monolithic `index.html` with a sidebar-shelled multi-page app that matches the Stitch design language (Material 3 dark theme via Tailwind CDN, Inter/Outfit fonts, Material Symbols).
- Introduce persistent play history in SQLite so the Dashboard's Recent Plays card shows real data and the future History page is nearly free to build.
- Download and locally cache low-resolution album art so the UI doesn't depend on remote CDN availability.
- Set up file structure so each future page-pass (Settings, History, Setup Wizard) is a small, self-contained addition: one route, one template, one JS file.

## Non-goals

- Building the Settings, History, or Setup Wizard pages. They get stub placeholders only.
- Adding a build step. Tailwind comes in via CDN; no PostCSS, no bundler.
- Adding authentication, user accounts, or membership concepts. The Stitch dashboard's user chip is replaced with a passive engine-status pill.
- Migrating away from FastAPI + Jinja templates.
- Wiring up MQTT-connection telemetry or audio-buffer latency to System Health. Only Input Level (dB) is real in this pass; Connection shows a pending state; Audio Buffer is removed.
- Manual engine start/stop controls in the UI. The engine's lifecycle is owned by the Docker container.

## Design Direction

Stitch as direction, not gospel. Match the visual language closely (color tokens, layout, typography, vinyl-centric dashboard) but deviate where it improves the UX:

- The Stitch dashboard's Spotify-style prev / play-pause / next transport controls don't fit a passive turntable scrobbler. The vinyl becomes a status-only indicator that animates while a track is identified.
- The Stitch user chip becomes a read-only engine-status pill, since this is a self-hosted tool with no users.
- System Health is reduced to two rows (Input Level live, Connection pending) instead of three.

## Architecture

### File Layout

**Templates (`gui/templates/`):**
- `_layout.html` — new. App shell: `<html>` head with Tailwind CDN + M3 color tokens lifted from `stitch/dashboard/index.html`, Inter (400/500/600), Outfit (600/700), Material Symbols Outlined. Body owns the sidebar (brand, nav, footer pill) and a `{% block content %}` slot.
- `dashboard.html` — new, replaces `index.html`. Extends `_layout.html`. Owns Now Playing + System Health + Recent Plays.
- `history.html`, `settings.html`, `setup.html` — new stubs. Each extends `_layout.html` and renders a centered "Coming soon" empty state with a Material icon. Future phases overwrite these.
- `index.html` — deleted.

**Static (`gui/static/`):**
- `shell.js` — new. Loaded on every page from `_layout.html`. Owns the WebSocket connection, the engine-status pill, sidebar active-state hooks, and a tiny pub/sub so page-specific scripts can subscribe to live frames without opening a second WebSocket.
- `dashboard.js` — new. Subscribes to `shell.js` for live frames; updates vinyl spin state, input meter, track display, system-health input level; refreshes Recent Plays when a new identification arrives.
- `styles.css` — kept and slimmed. Holds vinyl rotation `@keyframes` and any utility Tailwind can't express cleanly.
- `placeholder.jpg` — kept.
- `app.js` — deleted (split into `shell.js` + `dashboard.js`).

**Backend (`gui/backend_main.py`):**
- `GET /` → renders `dashboard.html` with `current_page="dashboard"`.
- `GET /history`, `GET /settings`, `GET /setup` → render the matching stub template with their `current_page` value.
- `GET /api/recent?limit=N` → new. Returns most recent plays from SQLite. Default `limit=10`, capped at 50.
- `GET /api/config`, `POST /api/config`, `GET /api/devices`, `WS /ws/live-status` — unchanged.
- `POST /api/engine/start`, `POST /api/engine/stop` — **removed**. Container lifecycle is the engine lifecycle.

**New module (`gui/play_history.py`):**
- `init_db()` — runs `CREATE TABLE IF NOT EXISTS` on startup, called from FastAPI lifespan.
- `record_play(title, artist, album, art_url) -> int` — inserts a row with `played_at = int(time.time())`, returns the new row id.
- `recent_plays(limit=10) -> list[dict]` — `SELECT ... ORDER BY played_at DESC LIMIT ?`.
- `set_art_path(play_id, art_path)` — `UPDATE` helper.

Uses stdlib `sqlite3` wrapped in `asyncio.to_thread(...)`. Single writer (the IPC handler), low write rate (one insert per identification), so contention is a non-issue.

### Sidebar App Shell

Sidebar is fixed-width on the left, content fills the rest. Sidebar contains:

1. **Brand:** "SpinSense / Hi-Fi Scrobbler" wordmark, top-left.
2. **Nav:** three links — Dashboard, History, Settings — each with a Material icon. The active link gets a highlighted pill background. Active state is decided server-side via the `current_page` variable each route passes to `_layout.html`; no JS needed for nav highlighting.
3. **Footer:** the engine-status pill. Driven entirely by `shell.js` from WebSocket frames. The core engine currently emits two status values, `"Listening"` and `"Playing"`, which drive the live states. States:
    - `● Idle` (grey) — no WebSocket frames received yet (pre-connection / engine not running).
    - `● Listening` (amber) — `status_msg = "Listening"`.
    - `● Playing` (green) — `status_msg = "Playing"` (equivalently, `payload.track.title` is non-empty).
    - `● Disconnected` (red) — WebSocket open then closed; `shell.js` retries with backoff 1s → 2s → 5s → 10s thereafter.

### WebSocket Pub/Sub

`shell.js` opens the single WebSocket and exposes a minimal API on `window.SpinSense`:

```js
window.SpinSense = {
  onFrame(cb) { /* register callback for each live_status payload */ },
  offFrame(cb) { /* unregister */ },
}
```

`dashboard.js` calls `SpinSense.onFrame(...)` to receive live updates. Stub pages don't subscribe. The pill in the footer reads frames directly within `shell.js`.

### Dashboard Page

Two-column grid on wide screens, stacked on narrow:

**Left column — Now Playing card**
- Vinyl visual: SVG record with centered label disc. CSS rotation (`@keyframes` in `styles.css`) runs only while `payload.track.title !== ""`. Album art, when available, is masked into the label disc; otherwise the label shows the SpinSense logo.
- Track metadata: title (Outfit 700, large) above artist and album. Empty state: "Waiting for drop…".
- RMS input meter: thin horizontal bar below the track info. Width = clamped percentage of `rms_level` against the configured threshold. Smooth `transition: width 0.2s linear`.

**Right column — stacked cards**

*System Health (top):*
- **Input Level** (real): `dB = 20 * log10(rms)`, displayed as e.g. `-16 dB` plus a small horizontal bar. Clamped to `[-60, 0]`. `rms == 0` displays `-∞ dB`.
- **Connection** (placeholder): muted/grey pill labeled "Pending — MQTT telemetry coming soon." No live data wired in this pass.
- Audio Buffer row is omitted entirely.

*Recent Plays (below):*
- Header "Recent" with a "View All" link to `/history` (which is currently a stub).
- Up to 5 rows. Each: 40×40px album-art thumbnail (from `art_path`, with `/static/placeholder.jpg` fallback when NULL), title, artist, relative time ("4m ago" etc.). The 64×64 source is intentionally 1.6× the rendered size to stay crisp on high-DPI displays.
- Loaded on page load via `fetch('/api/recent?limit=5')`. Re-fetched whenever a new identification arrives (`payload.track.title` transitions empty → non-empty, or one title → a different title).
- Empty state: "No plays yet — drop a record to begin."

## Data Flow

**Live frames** (existing path, unchanged):
```
core_engine.py → UDS /tmp/spinsense.sock → ipc_manager.handle_uds_client
              → manager.broadcast() → all open WebSockets → shell.js → onFrame() subscribers
```

**Play recording** (new):
- `ipc_manager.handle_uds_client` tracks the last-recorded track title in a module-level variable.
- When an incoming frame's `payload.track.title` is non-empty **and** differs from the last-recorded title, it calls `record_play(...)`, then schedules a fire-and-forget art-download task. The track-title state update is synchronous; both DB and download work happen via `asyncio.create_task` / `asyncio.to_thread` so the broadcast loop is never blocked.
- Re-broadcasts of the same identification (same title) do not create duplicates. De-dupe is by title only, not (title + artist) — this is more forgiving against songrec's occasional artist-string variation.

**Art download** (fire-and-forget, in the new task):
1. `aiohttp.get(art_url, timeout=5s)`.
2. `Pillow.Image.open(bytes).convert("RGB")`, `.thumbnail((64, 64))`, `.save(path, "JPEG", quality=75)` to `$SPINSENSE_DATA_DIR/art/<id>.jpg`.
3. `set_art_path(id, f"art/{id}.jpg")`.
4. Any exception → log warning, leave `art_path = NULL`. Row stays recorded; frontend renders placeholder.

**Recent plays read:**
- `GET /api/recent?limit=N` → `play_history.recent_plays(N)` → returns rows as JSON, each with `id`, `title`, `artist`, `album`, `art_url`, `art_path`, `played_at`.

## Schema

```sql
CREATE TABLE IF NOT EXISTS plays (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  title       TEXT    NOT NULL,
  artist      TEXT    NOT NULL,
  album       TEXT,
  art_url     TEXT,             -- original remote URL, kept for future History link-outs
  art_path    TEXT,             -- e.g. 'art/42.jpg' relative to $SPINSENSE_DATA_DIR; NULL if download failed
  played_at   INTEGER NOT NULL  -- unix epoch seconds
);
CREATE INDEX IF NOT EXISTS idx_plays_played_at ON plays (played_at DESC);
```

No retention policy in v1 — ~150 plays/day yields ~50k rows/year, still trivial for SQLite. Art directory grows at ~3–5 KB per play (~220 MB/year worst case). Cleanup is a future concern.

## Storage Locations

All under `$SPINSENSE_DATA_DIR` (existing env var, defaults to project root, matches `config.json` convention):
- `spinsense.db` — SQLite database.
- `art/<id>.jpg` — 64×64 JPEG quality 75 thumbnails. Directory is auto-created on first run.

FastAPI mounts `art/` as a StaticFiles route at `/art`, alongside the existing `/static` mount, so the browser fetches thumbnails directly with normal HTTP caching.

## Dependencies

`requirements.txt` gains `Pillow` (only new dep). `aiohttp` is already pulled in transitively via `shazamio`; no new HTTP client needed.

## Error Handling

- **WebSocket disconnect** → pill turns red, `shell.js` retries with backoff (1s → 2s → 5s → 10s). Dashboard content freezes at last-known state until reconnect.
- **`/api/recent` failure** → Recent Plays shows "Couldn't load recent plays." No retry UI in v1; user reloads the page.
- **Art download failure** (timeout, non-image response, decode error) → warning log, `art_path` stays NULL, placeholder renders.
- **DB write failure** → error log, broadcast continues. Not surfaced in UI in v1.
- **Missing/corrupt DB file on startup** → `init_db()` recreates the table; existing rows preserved if file exists. No migration framework warranted yet (future column additions use `ALTER TABLE ADD COLUMN`).

## Testing

This project has no test suite today, and Phase 1 isn't where to bootstrap one — but a small amount is warranted:

**Lightweight unit checks** (fast, no external deps):
- `play_history.record_play` + `recent_plays` round-trip against a temp DB file.
- De-dupe logic: identical consecutive title inserts once; different title inserts a new row.

**Manual smoke tests** (documented in the PR description):
1. Fresh container start with empty `$SPINSENSE_DATA_DIR` → `spinsense.db` and `art/` directory get created on first request.
2. Drop a record; song identifies → row appears in `plays`, JPEG appears at `art/<id>.jpg`, Recent Plays list updates without page reload.
3. Re-broadcast of the same identification → no duplicate row.
4. Block outbound HTTP → identification still records, `art_path` is NULL, placeholder renders.
5. Navigate Dashboard → Settings stub → Dashboard → engine pill stays live without dropping the WebSocket.
6. Kill the WebSocket at the network layer → pill turns red and reconnects on restoration.

## Open Items for Future Phases

These are explicitly deferred and do not need to be answered to ship Phase 1:

- **Phase 2 (Settings):** port the existing config form to `/settings` using Stitch's four-card layout. No backend changes — `/api/config` and `/api/devices` already exist.
- **Phase 3 (History):** full paginated play-history list at `/history`. New `/api/history?offset=&limit=` endpoint (pagination on top of `recent_plays`).
- **Phase 4 (Setup Wizard):** Hardware → Noise Floor → Signal → MQTT → Review flow at `/setup`. This is the only phase that requires real engine work: noise-floor and signal-calibration routines don't exist in `core_engine.py` yet.

## Summary of Phase 1 Deliverables

1. Shared `_layout.html` shell with sidebar nav and a live engine-status pill driven by a WebSocket pub/sub in `shell.js`.
2. Dashboard at `/` — Now Playing (vinyl + track + input meter), System Health (input level in dB + pending Connection row), Recent Plays.
3. SQLite play history at `$SPINSENSE_DATA_DIR/spinsense.db` with fire-and-forget album-art download to 64×64 JPEG under `art/`.
4. New `GET /api/recent` endpoint.
5. Stubs at `/history`, `/settings`, `/setup` so the sidebar links work.
6. Removed `/api/engine/start` and `/api/engine/stop`; engine lifecycle now belongs to Docker.
