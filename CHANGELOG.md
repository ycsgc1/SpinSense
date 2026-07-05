# Changelog

All notable changes to SpinSense are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/) and the project uses a 4-digit `MAJOR.MINOR.PATCH.MICRO` version scheme.

## [1.5.1.1] - 2026-07-05

### Fixed
- **A network hiccup during identification could permanently silence the engine.** Shazam was the only recognizer whose request errors weren't handled — one failed request crashed the audio monitor loop, leaving the web UI running but nothing ever identified again until a container restart. A failed request is now treated as a miss (matching the AudD/AcoustID backends), so the escalating retries and the backup recognizer still run.
- **History rows loaded by infinite scroll could render outside their date group.** With more than 50 plays in one date group, the next page's rows for that same date were inserted as bare, unstyled list items below the group's panel instead of inside it.
- Internal: reconciled the engine's default-config table with the config validator (`Retrigger_On_Track_Change`); no runtime change.

## [1.5.1.0] - 2026-06-15

### Added
- **SpinSense now has a logo.** A vinyl-and-soundwaves mark (vector source in `art/logo.svg`) ships as the browser favicon and Apple touch icon, sits beside the "SpinSense" wordmark in the desktop sidebar and mobile header, and heads the README. Colours match the existing app palette; no functional change.

## [1.5.0.5] - 2026-06-10

### Added
- **AcoustID backup recognizer + a "Backup recognizer" menu in Settings.** Shazam stays the always-on primary; you can now pick a backup from **None / AudD / AcoustID**. AcoustID is free (no subscription, no signup — an app key ships embedded), using on-device Chromaprint fingerprints against the MusicBrainz-linked AcoustID database. Off by default.

### Fixed
- **Endless rescan loop on unidentifiable tracks.** A track that Shazam (and the backup) couldn't identify would re-scan over and over while it kept playing, because the post-scan RMS reset was misread as a silence gap that cleared the back-off. The back-off now holds until a genuine between-song gap, so a failed track settles quietly instead of looping.

### Changed
- The 1.5.0.0 `Audio.Fallback_Enabled` boolean is replaced by `Audio.Fallback_Provider` (`none`/`audd`/`acoustid`). Existing configs default to `none`.

## [1.5.0.0] - 2026-06-09

### Added
- **AudD backup recognizer.** When Shazam can't identify a track on its first try, SpinSense now retries the *same* sample against [AudD](https://audd.io) before falling back to its longer-sample retries — improving the hard-to-identify tail. Opt-in: enable it and paste an API token in Settings (off by default; nothing changes until configured). Album art is still sourced iTunes-first, so artwork quality is unchanged.

### Changed
- Recognition is refactored behind a normalized track shape so backends (Shazam, AudD, future ones) are pluggable. No behavior change to the Shazam path.

## [1.4.0.1] - 2026-06-09

### Added
- **`requirements-dev.txt`** documenting the dev/CI tooling (pytest, ruff, vulture), kept separate from the runtime `requirements.txt` so the production Docker image stays lean. No runtime change.

## [1.4.0.0] - 2026-06-09

### Fixed
- **mDNS now advertises the real app version** instead of a hardcoded `1.0` in the service TXT record.
- **Play history no longer collapses two different songs that share a title** — scrobble dedupe is now keyed on artist + title, not title alone.
- **Hardened against silently-dropped background tasks** — the config watcher, command listener, MQTT reconnect, calibration finish, and art-download tasks now hold strong references so the event loop can't garbage-collect them mid-run.
- Dead WebSocket connections are dropped after a failed send instead of being retried on every frame.

### Removed
- **Removed the non-functional MQTT auto-discovery payload** (`announce_to_ha`) and its dead `MQTT.Discovery` config. The companion Home Assistant integration discovers SpinSense over mDNS/zeroconf, not MQTT, and the payload used non-standard discovery keys that stock HA ignored — so it never created an entity. (The plain MQTT state topics under `home/vinyl/*` are unchanged.)
- Internal cleanup: deleted the unused `gui/audio_utils.py` module and the dead `mock_core_engine_stream` dev helper; removed the orphaned `System.Engine_Status` config key; reconciled the engine and config-validator default tables (`Song_Sample_Length`, `Stopped_Silence_Interval`, MQTT host, discovery topic) to a single set of values; refreshed a stale log banner and docstring.

## [1.3.0.0] - 2026-06-09

### Added
- **Escalating rescans.** When a track can't be identified, SpinSense now waits a configurable `Rescan_Wait_Interval` (default 5 s) and retries with a progressively longer sample — 1×, then 2×, then 3× the sample length (capped at 60 s) — before backing off. New Settings field with help tooltip.

### Changed
- **dB threshold floor lowered from −80 dB to −120 dB** across the threshold slider, level meters, and auto-calibration, so quiet music on low-noise-floor line-level hardware can be distinguished from silence. The floor is now a single `FLOOR_DB` constant in `db_utils.js`.
- **`New_Song_Silence_Interval` default aligned to 3 s** across the engine and the config validator (previously 2 s vs 10 s).

### Fixed
- **Brief audio dips no longer re-trigger identification.** A momentary drop below the threshold (e.g. a quiet passage) was rescanning the track almost immediately, ignoring the configured silence interval. Rescans now fire only after a gap lasting at least `New_Song_Silence_Interval` seconds; `New_Song_Silence_Interval` previously had no effect at all.

## [1.2.1.0] - 2026-06-07

### Fixed
- **Settings help tooltips now appear in a hover/focus bubble** instead of printing their text inline. The underlying cause was stale cached CSS; static CSS/JS assets are now **version-stamped** (`/static/…?v=<version>`) so every release reliably busts browser and reverse-proxy caches — fixing this whole class of "new HTML, old stylesheet" bug.

### Changed
- **The "Re-announce each track to Home Assistant" toggle now controls both protocols.** When on, each new track re-announces over **both** the WebSocket integration and MQTT; when off, neither does. (Previously MQTT pulsed `stopped`→`playing` on every track regardless of the toggle.) Note: with the toggle off (default), the MQTT entity no longer pulses per track — turn the toggle on to keep that behavior.

## [1.2.0.0] - 2026-06-07

### Added
- **"Re-announce each track to Home Assistant" toggle** (`Audio.Retrigger_On_Track_Change`, opt-in, default off). When on, each new track briefly drops the WebSocket-driven Home Assistant media player to idle before going back to playing, so automations that trigger on "started playing" re-fire on every track (e.g. to push the new title to a Tidbyt). App-side only — no integration change needed; the MQTT entity already re-announces each track regardless.
- **Help tooltips on the Settings page** — a hover/focus "?" next to each option (Microphone and all Audio settings, including the new re-announce toggle) explaining what it does. The MQTT section is intentionally left without tips.

## [1.1.0.0] - 2026-06-07

### Added
- **Recognition status indicators on the dashboard.** A per-phase glow behind the vinyl (listening / scanning / identifying / playing / retrying / no_match), a status caption, and matching engine-pill states. The engine now publishes a machine-readable `phase` field on the `live_status` WebSocket frame — purely additive; the Home-Assistant-polled `status_msg` is unchanged.
- **Recognition resilience.** Identification now auto-retries twice before giving up, surfaces a distinct "couldn't identify this one" (`no_match`) state instead of silently leaving the previous track spinning on screen, and then backs off so it won't re-hammer the same unidentifiable track until the next audio onset (the gap between songs).
- **"Scan again" button** on the dashboard to force a fresh identification on demand — a new `rescan` engine command over the existing command socket, exposed as `POST /api/rescan`.
- **Delete a scrobble from History** — a hover ✕ with a 5-second Undo. Backed by soft-delete (a `deleted_at` column) so Undo is instant; `DELETE /api/plays/{id}` and `POST /api/plays/{id}/restore`.
- **Automatic art cleanup** (`purge_deleted`) — hard-deletes soft-deleted scrobbles past a 120 s grace window and unlinks their cached art only when no remaining row references it. Runs on startup and every 30 minutes.

### Changed
- **`recognize_audio` refactored** into capture / identify / handle stages with a retry policy; track-state clearing is centralized in `_clear_track_state`, shared by the no-match and silence-stop paths. The dashboard now drives all now-playing display from `phase`, clearing stale art the instant recognition leaves `playing` (fixing the case where an unidentifiable track left the previous song's art on screen).

## [1.0.0.0] - 2026-06-01

### Added
- **Home Assistant mDNS/zeroconf discovery.** SpinSense advertises `_spinsense._tcp.local.` on the LAN (`gui/discovery.py`), so the companion HACS integration auto-discovers it with no broker. Advertising is toggleable and reconciled live whenever config is saved; failures are non-fatal.
- **`GET /api/status`** — returns the last engine status (`engine_active`, `status_msg`, `rms_level`, `track{title,artist,album,art_url,…}`), the contract the Home Assistant integration polls. Backed by a last-status cache in `ipc_manager` that defaults to a "stopped" payload before the engine reports.
- **Configurable listen port** via `SPINSENSE_PORT` (default **3313**, a nod to 33⅓ RPM); `EXPOSE 3313` in the Dockerfile. Required because mDNS needs `network_mode: host`, which removes port remapping.
- **Setup wizard "Home Assistant & Integrations" step** — independent **mDNS** (on by default, zero-config) and **MQTT** (opt-in) toggles, replacing the MQTT-only step.
- **History enrichment columns** — nullable `isrc`, `genre`, `release_year` on the `plays` table, populated best-effort from the recognition result, paving the way for listening analytics. Idempotent `ALTER TABLE` migration; old rows stay valid.
- First-party HACS integration repository: [ycsgc1/homeassistant-spinsense](https://github.com/ycsgc1/homeassistant-spinsense).
- Installation, Setup, and Usage sections in the README.
- **Prebuilt multi-arch images** (amd64 + arm64) published to GHCR (`ghcr.io/ycsgc1/spinsense`) by a GitHub Actions workflow — `:main` on every commit, `:latest` + the version tag on each release. The reference `docker-compose.yml` now pulls the image, so `docker compose pull` (and Dockge's Update button) just works.

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
