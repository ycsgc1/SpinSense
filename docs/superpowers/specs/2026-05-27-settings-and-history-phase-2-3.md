# Settings + History (Phase 2 + Phase 3)

**Date:** 2026-05-27
**Status:** Drafted, awaiting approval
**Scope:** Second and third passes of the four-phase frontend redesign. Replaces the stub `/settings` and `/history` pages landed in Phase 1 with working surfaces.

## Context

Phase 1 ([2026-05-26-frontend-shell-and-dashboard-design.md](2026-05-26-frontend-shell-and-dashboard-design.md)) landed the sidebar shell, Dashboard, SQLite play history, and stub pages for `/history`, `/settings`, `/setup`. The user has confirmed Phase 1 looks right visually but hasn't been able to validate it on real hardware because there's no way to pick a mic, tune thresholds, or review accumulated plays from the UI yet.

This spec bundles Phase 2 (Settings) and Phase 3 (History) into a single pass so the user can stand up a real hardware test after this lands. Phase 4 (Setup Wizard) stays deferred.

## Goals

- Replace `/settings` stub with a Material-3 form that reads and writes the config fields that are actually wired through the engine: **Hardware.Mic_Device, Audio (all four fields), and MQTT.Broker (Host, Port, User, Password)**. Other config fields (`System`, `MQTT.Discovery.*`, `MQTT.Topics.*`) are excluded — see "Engine wiring reality" below.
- Fix a pre-existing engine bug: `core/core_engine.py` line 63 reads `config['Audio']['Input_Device']`, which doesn't exist in the schema. The schema's mic field is `Hardware.Mic_Device`. Patch the engine to read the right key so the Settings UI's mic dropdown actually takes effect on container restart.
- Add a live RMS preview alongside the `Volume_Threshold` slider so calibration is "set it where the green meter is when needle is on but no music plays" rather than guesswork.
- Replace `/history` stub with a scrollable, date-grouped play list backed by a paginated `/api/plays` endpoint.
- Keep the file-per-page convention from Phase 1 — one route, one template, one JS module per page.

## Engine wiring reality

A check of `core/core_engine.py` revealed three things this spec has to account for:

1. **Config is read once at module import** (lines 56–57). Every setting becomes a module-level constant (`THRESHOLD`, `MQTT_HOST`, etc.). There is no file-watch and no reload signal — so all config changes require a container restart (`docker compose restart`) to take effect. The Settings UI surfaces this with a static notice; no in-UI restart button.
2. **Mic device key mismatch** (line 63): `config['Audio']['Input_Device']` is queried, but the schema stores it at `Hardware.Mic_Device`. This pass fixes the engine; the GUI side already uses the correct key.
3. **MQTT discovery + topics are hardcoded** (lines 72–79): `BASE_TOPIC = "home/vinyl"` and `DISCOVERY_TOPIC = "homeassistant/media_player/spinsense/config"` are string constants in the engine. The matching `MQTT.Discovery.*` and `MQTT.Topics.*` config fields are not read. **Out of scope this pass** — those fields stay in `config.json` as dead-but-default values, and the Settings UI does not expose them, because exposing fields that don't affect anything would mislead users. Filing the engine-side fix is left to a future pass.

## Non-goals

- The Setup Wizard (Phase 4). MQTT lives in Settings now; the Wizard becomes about guided first-run calibration only.
- Engine restart from the UI. Engine reads config at startup; some changes (MQTT broker, mic device) need a container restart. The Settings page surfaces this with a static notice, not a button.
- Auth, multi-user, or any account concept.
- Search, filter, delete, or export on History. Pure read-only chronological list this pass.
- Editing or deleting past plays.
- Server-Sent Events or any new transport. RMS preview reuses the existing `window.SpinSense.onFrame` pub/sub from `shell.js`.

## Design Direction

Match Stitch's settings/history surfaces in `stitch/` where they exist; otherwise follow Phase 1's visual language (Tailwind M3 dark tokens, Inter/Outfit, Material Symbols, glass-panel cards).

- **Settings** is one scrollable page with section headings (Hardware, Audio, MQTT), a single sticky "Save" button at the bottom that posts the whole `/api/config` payload, and a small "Saved" toast on success. No per-section save buttons — pydantic validates the whole object anyway.
- **History** is a single scrollable list grouped by date header ("Today", "Yesterday", "May 25, 2026"). Each row mirrors the Dashboard's Recent Plays row visually (thumbnail + title + artist + time) at the same density.

## Architecture

### File Layout

**Templates (`gui/templates/`):**
- `settings.html` — replaced. Three section cards (Hardware, Audio, MQTT). Hardware: mic device dropdown. Audio: four numeric fields (`Volume_Threshold` slider + live RMS meter, `Song_Sample_Length`, `New_Song_Silence_Interval`, `Stopped_Silence_Interval`). MQTT: broker host, port, user, password only. Sticky save bar. Static "Changes take effect after container restart" notice at the top of the page.
- `history.html` — replaced. Single content area: page heading + total-plays count + scrollable list grouped by date.

**Static (`gui/static/`):**
- `settings.js` — new. On load: `GET /api/config` to populate form, `GET /api/devices` to populate mic dropdown. Subscribes to `window.SpinSense.onFrame` to render live RMS bar. On save: serialize the form back into the nested config shape, `POST /api/config`, show success/error toast.
- `history.js` — new. Infinite scroll: initial `GET /api/plays?limit=50&offset=0`, render grouped by date; on scroll near bottom, fetch next page until the server returns fewer than `limit` rows. Each row uses the same `escapeHtml` + art-path fallback logic as `dashboard.js`'s Recent Plays — extract into a shared helper.
- `shell.js` — unchanged.
- `styles.css` — minor additions only if Tailwind can't express something cleanly (e.g. the threshold-meter overlay).

**Backend (`gui/backend_main.py`):**
- `GET /api/plays?limit=N&offset=M` — new. `limit` default 50, capped at 100; `offset` default 0, no upper bound. Delegates to `play_history.recent_plays(limit, offset)`.
- `GET /api/recent` — unchanged (Dashboard keeps using it with its small limit).
- `POST /api/config` — unchanged on the success path. On pydantic validation failure, return HTTP 400 with `{"status": "error", "detail": "<message>"}` instead of today's silent success-return-on-failure. (Today's code returns `{"status": "success"}` even when `save_config` returned `False`.)

**Play history (`gui/play_history.py`):**
- `recent_plays(limit=10, offset=0, db_path=None)` — add `offset` parameter. Append `OFFSET ?` to the existing query. Existing call sites (Dashboard's `/api/recent`) keep working since `offset` defaults to 0.

**Engine (`core/core_engine.py`):**
- Line 63 fix: replace `config.get('Audio', {}).get('Input_Device', None)` with `config.get('Hardware', {}).get('Mic_Device', None)` so the engine actually honors the configured mic. Only change in this file — MQTT topic/discovery hardcoding is intentionally left alone (see Engine wiring reality above).

**Tests (`gui/tests/`):**
- `test_play_history.py` — extend with offset cases:
  - `offset=0, limit=2` returns the two newest.
  - `offset=2, limit=2` returns the next two.
  - Offset past the end returns `[]`.
- `test_config_round_trip.py` — new. Verifies `save_config` → `load_config` returns the same dict for a representative payload, and that invalid types (e.g. `Port: "not-a-number"`) cause `save_config` to return `False`.

### Save behavior (Settings)

- Form fields are bound by `name` attributes matching the dot-path into the config (`Audio.Volume_Threshold`, `MQTT.Broker.Host`, etc.).
- On save, `settings.js` walks the form, rebuilds the nested object, and posts it.
- The endpoint returns 200 on success, 400 with `detail` on validation failure. The toast renders either "Saved" (green) or the detail message (red).
- "Restart required" is a static info banner at the top of the Settings page: "Settings changes take effect after the container restarts (`docker compose restart`)." This applies to every field — the engine reads config once at startup. No in-UI restart button.

### RMS preview for Volume_Threshold

- A 4px-tall horizontal bar lives directly below the `Volume_Threshold` slider.
- Bar fills proportional to live RMS (`rms_level` from the WS frame), normalized against `0.05` (the visual ceiling, ~5x typical threshold).
- A vertical tick on the bar marks the current slider value, also normalized to `0.05`.
- The user calibrates by playing silent (needle on, no music) and seeing where the bar peaks, then nudging the slider tick to just above that peak.
- If the WS is disconnected, the bar is grey and shows "Engine offline".

### History pagination

- Page size: 50 rows per request.
- `history.js` keeps an `offset` counter and an `exhausted` flag.
- IntersectionObserver on a sentinel `<div>` after the last row triggers the next fetch.
- Date grouping is client-side: convert each row's `played_at` (Unix seconds) to local date string; insert a header whenever the date changes from the previous row.
- Empty state (offset=0 returns `[]`): friendly message with a record-needle Material icon — "No plays yet. Start spinning records to see them here."

## Acceptance Criteria

- `/settings` loads and renders all current values from `/api/config`. Saving with no changes round-trips identically.
- Editing any field and clicking Save persists to `config.json` on disk (verified by re-loading the page).
- Invalid input (e.g. port `99999999`) shows a red toast with the pydantic error message and does NOT persist.
- The volume-threshold RMS preview bar moves when audio is coming in. The slider tick moves smoothly and updates the value displayed.
- `/history` loads 50 most-recent plays grouped by date. Scrolling to the bottom triggers a second fetch of the next 50.
- A row with `art_path` set shows the cached thumbnail; a row with only `art_url` falls back to the placeholder.
- Test suite: existing 8 pass plus 3 new offset cases and 2 new config-round-trip cases (≥13 total).

## Manual Test Plan

(Run inside Docker on real hardware so the engine is actually feeding the socket.)

- [ ] Load `/settings`. All fields populate from `config.json` defaults. Mic dropdown lists at least the default device.
- [ ] Change `Volume_Threshold` slider. Slider tick on the meter bar moves in lockstep. Live RMS bar reflects current audio.
- [ ] Pick a non-default mic from the dropdown, save, `docker compose restart`. Engine logs (`docker compose logs spinsense | grep -i input`) confirm the new device is in use — verifies the line-63 bug fix.
- [ ] Set `MQTT.Broker.Port` to `1883` and save. Reload page. Field still shows `1883`.
- [ ] Set `MQTT.Broker.Port` to `999999`. Save. Red toast with validation error. Reload — value unchanged.
- [ ] Load `/history`. Date headers and play rows render. Album art appears for rows that have it.
- [ ] Scroll to bottom. Next 50 plays load. Repeat until the last fetch returns fewer than 50 — observer stops firing.
- [ ] Hit `/api/plays?limit=10&offset=0` directly. Check the offset/limit math against the rows you see on `/history`.

## Out-of-scope (deferred to Phase 4)

- Setup Wizard: first-run threshold calibration walkthrough. With MQTT now in Settings, the Wizard scope shrinks to "tune your mic + threshold once, with guided steps."

## Risks / Notes

- Engine reads config at startup. Every Settings change requires container restart to take effect — UI calls this out at the top of the page, and the CHANGELOG mentions it explicitly so users aren't surprised.
- The pydantic v1 `.dict()` calls in `config_manager.py` are deprecated in v2. Out of scope for this pass — leave them.
- `POST /api/config` returning `{"status": "success"}` even on save-failure is a real bug being fixed in this pass; surfaced as part of acceptance criteria above.
- `MQTT.Discovery.Discovery_Topic` and `MQTT.Topics.*` config fields remain unread by the engine (hardcoded in `core_engine.py`). Not addressed in this pass to keep scope tight; tracked as a future cleanup. The Settings UI does not expose them so users don't try to change values that have no effect.
