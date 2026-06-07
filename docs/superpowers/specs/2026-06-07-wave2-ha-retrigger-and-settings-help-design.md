# Wave 2 — HA Track-Change Re-trigger + Settings Help Tooltips

**Date:** 2026-06-07
**Status:** Designed autonomously (overnight batch); pending user review in the morning.
**Scope:** Two independent features. (#4) An opt-in toggle so each new track re-triggers a Home Assistant automation. (Tooltips) A hover "?" help icon explaining each non-MQTT Settings option.

> **Design decisions made without live approval** (user asked to "do both in one go" overnight). Each call is flagged **[DECISION]** for morning review.

---

## Feature #4 — Re-announce each track to Home Assistant

### Problem

On a track change with no silence gap, the HA media_player **stays `playing`** — only the title attribute changes — so automations triggered on "started playing" don't re-fire (e.g. pushing the new title to a Tidbyt). The MQTT entity already pulses `stopped`→`playing` on every new track; the first-party HACS integration does not.

### Key finding (why this is app-side only)

The HACS integration (`ycsgc1/homeassistant-spinsense`) is **WebSocket-driven**, not interval-polling: `SpinSenseAPI._websocket_loop` consumes every `live_status` frame from `/ws/live-status` and updates HA on each one (`_update_state` → `_notify_listeners`). Its `media_player._update_from_api` maps `status_msg`: `"playing"` → `PLAYING`, `"stopped"`/`engine_active=false` → `OFF`, anything else (e.g. `"Listening"`) → `IDLE`. So a single `in_song=False` frame on the WS reliably makes the entity blip **PLAYING → IDLE → PLAYING** — no integration change needed, no polling-miss risk.

### Design **[DECISION]**

- **New config field `Audio.Retrigger_On_Track_Change: bool` (default `False`).** Opt-in because it has HA-wide side effects (any automation watching for the player going idle would fire each track). **[DECISION: default off; placed in the Audio section next to the silence intervals.]**
- **Engine:** in `core/core_engine.py` `_handle_match`, on the new-track branch (`result_str != state["last_song"]`), when the flag is on, emit one brief idle frame on the WS just before the new `playing` frame:
  ```python
  publish_state("stopped")                       # MQTT stop (existing, unchanged)
  if runtime.get("retrigger_on_track_change"):
      await _publish_idle_blip()                  # NEW: WS PLAYING->IDLE so HACS re-triggers
  await asyncio.sleep(0.5)
  publish_state("playing", artist, title, album, art_url, art_base64)
  ```
  with a new helper:
  ```python
  async def _publish_idle_blip() -> None:
      """Emit one in_song=False live_status frame so WS consumers (the HACS
      media_player + the dashboard) see a PLAYING->IDLE transition between
      tracks, re-firing 'started playing' automations."""
      payload = build_status_payload("listening", state.get("current_rms", 0.0), {"in_song": False})
      await _write_uds(json.dumps(payload) + "\n")
  ```
- **Runtime wiring:** add `"retrigger_on_track_change": False` to the `runtime` dict defaults and read it in `_populate_runtime`: `runtime["retrigger_on_track_change"] = cfg.get('Audio', {}).get('Retrigger_On_Track_Change', False)`. Hot-reload already re-runs `_populate_runtime` on Audio config changes.
- **Config model:** add `Retrigger_On_Track_Change: bool = False` to `AudioConfig` in `gui/config_manager.py`.
- **MQTT path is unchanged.** It already pulses on every new track; the toggle only adds the WS/HACS-path blip, so behavior at the default is identical to today.
- **No HACS integration code change.** (Confirmed against the cloned repo.)

### Scrobble-dedupe note

The idle blip carries an empty title, which resets `ipc_manager._last_recorded_title` — same as today's silence path. The subsequent `playing(new title)` records exactly once (new title ≠ ""), so no scrobble is dropped or duplicated.

### Behavior / side effects to surface to the user

- A brief (~0.4s) idle blip is visible on the dashboard between tracks (the "quick stopped indication" the user described) and on the HA entity. **[DECISION: acceptable — it's the desired signal; documented in the tooltip.]**

### Tests

- `core/tests/test_recognition_phases.py`: with the flag on, a new-track `_handle_match` emits an `in_song=False`/idle frame before the `playing` frame; with the flag off, it does not. (Monkeypatch `_write_uds`/`publish_state`/the iTunes+art fetchers; assert frame ordering.)

---

## Feature — Settings help tooltips

### Design **[DECISION]**

A reusable hover/focus "?" help affordance next to each **non-MQTT** Settings option. Pure CSS + a tiny bit of markup — no JS popover library.

- **Markup per option** (added inside each option's label, after the label text):
  ```html
  <span class="help-tip" tabindex="0" role="note" aria-label="Help: <option name>">
    <span class="material-symbols-outlined help-icon">help</span>
    <span class="help-bubble" role="tooltip">…copy…</span>
  </span>
  ```
- **CSS** (`gui/static/styles.css`): `.help-tip` is an inline, relatively-positioned, focusable circle-ish icon (`help` glyph in `text-on-surface-variant`, hover → `text-primary`); `.help-bubble` is absolutely-positioned, hidden by default, shown on `.help-tip:hover .help-bubble` and `.help-tip:focus .help-bubble` / `:focus-within`. Styled as a small dark card (`surface-container-high`, border `outline-variant`, rounded, shadow, ~260px max-width, small body text), positioned above-right of the icon, `z-50`, with `pointer-events:none` so it never blocks clicks. Works on hover AND keyboard focus (tap-focus on touch).
- **Coverage [DECISION]:** Hardware → Microphone; Audio → Volume threshold, Song sample length, New-song silence interval, Stopped silence interval, **and the new Re-announce toggle**. **MQTT section intentionally has none.**
- No settings.js change required for the tooltips themselves (pure CSS/markup). settings.js DOES need a small change for the #4 toggle (below).

### Help copy **[DECISION — verbatim]**

- **Microphone:** "Which audio input SpinSense listens to. Choose the line-in / USB capture device fed by your turntable (through a phono preamp). The setup wizard helps you pick and calibrate it."
- **Volume threshold:** "The loudness above which SpinSense decides music is playing and starts identifying. Set it just above the noise floor of a silent spinning record — too low and silence triggers scans, too high and quiet passages look like silence."
- **Song sample length:** "How many seconds of audio to record before sending it to Shazam. ~5s is usually plenty; longer can help on hard-to-identify tracks but makes each recognition slower."
- **New-song silence interval:** "How long a quiet gap (seconds) must last before SpinSense treats the next audio as a new song rather than a continuation — roughly the gap between tracks on a record."
- **Stopped silence interval:** "How long silence (seconds) must persist before SpinSense marks the record stopped and clears 'now playing'. Longer tolerates quiet passages within a song; shorter marks 'stopped' sooner after the needle lifts."
- **Re-announce each track to Home Assistant:** "When on, each new song briefly drops the Home Assistant player to idle before playing again, so automations that trigger on 'started playing' re-fire on every track (e.g. to push the new title to a display). Off keeps playback smooth with no idle blip. The MQTT entity already re-announces each track regardless."

---

## #4 Settings toggle (shared surface with the tooltips feature)

- **Markup:** in `gui/templates/settings.html`, Audio section, after "Stopped silence interval", add a toggle row bound to `name="Audio.Retrigger_On_Track_Change"` (a checkbox styled as a switch, or a plain accessible checkbox), with its help-tip.
- **settings.js:** extend the load/collect/dirty logic to handle a **checkbox** input (read/write `.checked` as a boolean) — currently it handles select/number/text via `.value`. Boolean must round-trip to `/api/config` as a real JSON boolean (the Pydantic model expects `bool`).

---

## Out of scope

- No change to the HACS integration repo.
- No new version/release in this branch — the user cuts the release after testing.

## Testing summary

- Engine: flag-on emits idle blip before playing; flag-off does not (unit test).
- Full `core` + `gui` suites stay green.
- Config round-trip: the new `Audio.Retrigger_On_Track_Change` boolean validates and persists (covered by the existing `test_config_round_trip` pattern; add a case).
- Settings UI (tooltips + toggle): dogfood — render `/settings`, hover/focus each `?`, toggle the switch, save, reload, confirm it persisted.
