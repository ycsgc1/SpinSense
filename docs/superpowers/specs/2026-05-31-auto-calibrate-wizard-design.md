# Auto-Calibrate in Setup Wizard + dB Display Everywhere

**Date:** 2026-05-31
**Status:** Drafted, awaiting approval
**Scope:** Two paired changes landing together:
1. **Auto-calibrate** — new two-phase guided flow in the setup wizard's threshold step (noise floor capture → music capture → suggested threshold) so users don't have to guess a number.
2. **dBFS display everywhere** — the wizard, Settings, and Dashboard all show the volume threshold + live RMS meter in dB instead of linear RMS. Internal storage stays linear; this is purely a display/input concern.

## Context

Today's wizard Step 2 hands the user a slider from 0.0 to 0.05 (linear RMS) with no guidance about what number is right. For a typical turntable line input (peak RMS around 0.0002–0.001), the entire useful range is squashed against zero on the slider. The user can't even *see* their working threshold, let alone fine-tune it. A hotter interface plugged in later would have the opposite problem.

Auto-calibrate was in the original Phase 4 wizard spec but got dropped during implementation. We're putting it back, and at the same time fixing the underlying display issue by switching every volume control to dBFS — the unit audio people actually think in, and one that gives uniform precision across every input level a user might plug in.

## Goals

- Wizard Step 2 splits the calibrate UX into Auto vs Manual paths via a chooser screen.
- Auto path captures 5 s of noise floor, then 5 s of music after the user signals a song has started, computes a suggested threshold via a "quarter above noise floor" formula in dB, and lands on a result screen with the slider pre-filled.
- Both paths converge on a slider + number input + live meter component, all in dB.
- Settings page Volume Threshold control switches to dB (slider + linked number input), no auto-calibrate button.
- Dashboard live RMS bar switches to dB scale with the threshold tick mark in the right place.
- New backend endpoints `POST /api/calibrate/start`, `GET /api/calibrate/status`, `POST /api/calibrate/clear` orchestrate captures by sending JSON commands to the engine over a new reverse-direction UDS socket.
- Engine's audio callback piggybacks sample collection onto its existing RMS computation; detection is suppressed during the 5 s capture window.

## Non-goals

- A migration path for the stored `Audio.Volume_Threshold` value. It stays linear in `config.json`; only the display flips. Existing installs keep their saved threshold and just see it rendered as dB on next page load.
- Auto-calibrate button in Settings. Users who want to re-calibrate use the existing "Re-run setup wizard" link.
- Higher sample rates from the engine. We accept the audio callback's natural cadence (~22 Hz at the default 1024-frame buffer @ 48 kHz, giving ~110 samples per 5 s window).
- JS unit tests for the new wizard FSM. Deferred — no JS test runner exists yet, and the manual test plan covers it for now.
- A longer "calibration session" lock that suppresses detection in the gaps *between* captures. Detection-suppression is scoped to the 5 s capture window only. Edge case is documented.
- Touching MQTT topics / discovery dead-code, pydantic v1 `.dict()` migration. Out of scope as in the prior pass.

## Wizard Step 2 — sub-screens

Step 2 becomes a small in-place state machine inside the existing `<section data-step="2">`. Sub-screens swap; no new wizard dots.

**Screen 2A — "Pick how to calibrate"**
Two buttons: **Auto-calibrate** (primary) and **Set manually** (secondary). One-line copy explaining the choice. "Back" returns to mic step.

**Screen 2B — "Step 1 of 2: Capture the noise floor"** (auto path)
Copy: *"Drop the needle on the runout groove — the silent section at the end or beginning of a side. Don't play a song yet. If your room is quiet, you can usually hear the music at the needle itself without speakers, so this is also how you know you're in a quiet section."* **"Start 5 s capture"** button. On press: countdown ring 5→1, live dB meter animates from the WS feed. Auto-advance to 2C on completion. A "Re-do this step" link appears alongside the result before advancing.

**Screen 2C — "Step 2 of 2: Capture a song"**
Copy: *"Now move the needle to a song. When you hear it start playing, click the button — we'll listen for 5 seconds."* Live dB meter visible throughout so the user can confirm sound is reaching the engine. **"Music started — capture now"** button → 5 s countdown.

**Screen 2D — "Your optimum threshold"**
Heading lands with a small scale-in animation: *"✨ Your optimum threshold is **−68 dB**"* (number computed per the formula below). Below: three labeled numbers ("Silence (noise floor)", "Music (quiet parts)", "Threshold") + the dB slider + linked number input pre-filled at the computed value. Live dB meter underneath with a vertical tick at the current threshold so the user can see noise, music, and threshold together. Buttons: **"Looks good — continue"**, **"Re-run calibration"** (back to 2B), **"Set manually instead"** (jump to 2E).

**Screen 2E — "Set threshold manually"** (manual path, or escape from 2D)
Same slider + number input + live meter as 2D, framed with today's "Drop the needle, watch the meter, nudge the slider just above the peak" copy. Continue / Back.

**"Re-do" button scope** (three separate affordances; each restarts a different amount of work):
- "Re-do this step" on **2B** after a noise capture completes: re-runs the noise floor phase only, overwrites the noise sample.
- "Re-do this step" on **2C** after a music capture completes (and on the bad-capture error popup): re-runs the music phase only, preserves the noise sample.
- "Re-run calibration" on **2D**: throws out both samples and returns to 2B for a full re-run.

## dB math + threshold formula

**Conversion helper.** New `gui/static/db_utils.js` (or inline in `shell.js`):
```js
window.SpinSense.db = {
  rmsToDb(rms)  { return rms <= 0 ? -80 : Math.max(-80, 20 * Math.log10(rms)); },
  dbToRms(db)   { return Math.pow(10, db / 20); },
  formatDb(db)  { return `${db.toFixed(1)} dB`; },
};
```
Display range: **-80 to 0 dB**, slider step **0.5 dB**. Python mirror in a test file for parity assertions.

**Storage stays linear.** `Audio.Volume_Threshold` in `config.json` remains a linear RMS float. Engine reads it unchanged. The wizard / Settings convert dB → linear on save, linear → dB on load.

**Per-phase anchors** (computed by the engine, returned in the `stats` blob):
- **Noise floor anchor** = `p99(noise_samples)` in dB — the loudest rumble blip during silence, not the mean. Threshold must clear this so rumble doesn't trigger detection.
- **Music anchor** = `p10(music_samples)` in dB — the quiet parts of music, not the mean. Threshold must sit below this so song intros / quiet passages still cross it.

**Threshold formula** ("quarter above silent" in dB space):
```
threshold_dB  = noise_p99_dB + 0.25 * (music_p10_dB - noise_p99_dB)
threshold_rms = 10 ** (threshold_dB / 20)
```

Worked example with realistic numbers (noise peak ≈ -72 dB, quiet music ≈ -30 dB):
`threshold_dB = -72 + 0.25 * 42 = -61.5 dB` → linear `0.000841`. Sits ~10 dB above noise (room for rumble) and ~30 dB below quiet music (lots of headroom for quiet intros).

**Safety clamps:**
- If `noise_p99_dB >= music_p10_dB` (bad capture, music quieter than silence): return error to wizard. Message: *"Your silence sample was as loud as your music sample. Try again — make sure a song is actually playing during the second step."* The "Re-do this step" button on the result screen restarts the music phase only (noise floor capture is preserved).
- If computed `threshold_dB` is within 2 dB of `noise_p99_dB`: bump to `noise_p99_dB + 2 dB`. Minimum breathing room.
- Final value clamped to `-80 ≤ threshold_dB ≤ 0`.

## Engine-side capture

**New state.** Module-level in `core_engine.py`:
```python
calibration: dict | None = None
# When active: {"phase": "noise_floor"|"music",
#               "samples": collections.deque,
#               "started_at": float,
#               "duration": 5.0,
#               "status": "running"|"done",
#               "stats": dict|None}
```

**Audio callback piggyback** (currently 2 lines, gains one branch):
```python
def audio_callback(indata, frames, time, status):
    rms = float(np.sqrt(np.mean(indata ** 2)))
    state["current_rms"] = rms
    if calibration is not None and calibration["status"] == "running":
        calibration["samples"].append(rms)
```
`deque.append` is atomic in CPython, safe to call from the sounddevice audio thread.

**Detection suppression.** In `audio_monitor_loop`, the existing detection branch is wrapped: if `calibration is not None and calibration["status"] == "running"`, skip the `vol > runtime["threshold"]` branch entirely. RMS still publishes to the GUI over UDS so the live meter keeps moving.

**Timer task.** When a `start_calibration` command arrives, the handler creates the `calibration` dict and spawns a one-shot `asyncio.create_task` that sleeps for `duration`, then computes stats from the collected samples and flips status to `"done"`. Stats computed once at completion (min, max, mean, p10, p50, p99, samples_count, duration_s).

**Command IPC — new direction.** Today's `/tmp/spinsense.sock` is engine → backend. Add a second socket `/tmp/spinsense-cmd.sock` that the engine listens on for backend commands. JSON-per-line, short-lived connection per command:

| Command | Engine action |
|---|---|
| `{"cmd": "start_calibration", "phase": "noise_floor"\|"music"}` | If `calibration` already running, reply `{"ok": false, "detail": "already running"}`. Otherwise allocate dict, spawn timer task, reply `{"ok": true, "duration_s": 5.0}`. |
| `{"cmd": "get_calibration"}` | Reply `{"status": "running"\|"done"\|"none", "samples_count": N, "stats": dict\|null}`. |
| `{"cmd": "clear_calibration"}` | Set `calibration = None`. Reply `{"ok": true}`. |

A new async task `command_listener_loop()` is started from `audio_monitor_loop()` alongside `connect_mqtt_loop()` and `config_watch_loop()`. Parse errors and unknown commands return `{"ok": false, "detail": "..."}` and the engine continues.

## Backend API + frontend orchestration

**New endpoints** in `backend_main.py`, all thin wrappers around a small `_send_cmd(payload, timeout=2.0)` helper that connects to `/tmp/spinsense-cmd.sock`, writes one JSON line, reads one JSON line, closes:

| Endpoint | Behavior |
|---|---|
| `POST /api/calibrate/start` body `{"phase": "noise_floor"\|"music"}` | Validates phase. Forwards as `start_calibration`. Returns `{"ok": true, "duration_s": 5.0}` on engine ack, `{"ok": false, "detail": "..."}` otherwise. Returns HTTP 503 if engine socket unreachable. |
| `GET /api/calibrate/status` | Forwards as `get_calibration`. Returns engine's reply as-is. 503 if unreachable. |
| `POST /api/calibrate/clear` | Forwards as `clear_calibration`. Returns `{"ok": true}`. 503 if unreachable. |

**Frontend FSM** (in `setup.js`):
```
substep ∈ { "choose", "noise_capture", "music_capture", "result", "manual" }
captures = { noise: stats|null, music: stats|null }
```

Per-phase capture:
1. User clicks "Start 5s capture" → `POST /api/calibrate/start { phase }`.
2. On 200, start client-side 5 s countdown (visual ring + dB meter from WS feed keep animating).
3. After 5 s elapsed, poll `GET /api/calibrate/status` every 250 ms until `status === "done"` (max 3 s of extra slack). Stash `stats` into `captures[phase]`.
4. On any error or 503: render inline error + "Re-do this step" button, no auto-advance.

Entering Screen 2D:
- Run the threshold formula locally on `captures.noise.p99` and `captures.music.p10`.
- Apply safety clamps; display the three numbers + pre-fill slider/number input.
- Fire `POST /api/calibrate/clear` (best-effort; ignore failure).

Cancellation (Back, X-close, navigate away): wizard fires `POST /api/calibrate/clear`. Engine's 5 s timer task self-completes regardless; clear just frees the dict.

Engine-down handling: Screen 2A mounts with a `GET /api/calibrate/status` reachability probe. On 503, the Auto-calibrate button is disabled with tooltip *"Audio engine not running — restart the container or use 'Set manually'"*. Manual path remains selectable (meter just sits at 0 if engine is down — slider/number input still work).

Saving: wizard's existing `buildPayload` writes `Audio.Volume_Threshold` from the slider. No change needed — slider value is still the linear RMS (dB is a display layer over it). Final write happens at "Save and finish" on Step 4, as today.

## Settings + Dashboard ripple

**`gui/templates/settings.html` + `gui/static/settings.js`:**
- Volume Threshold slider attrs change to `min="-80" max="0" step="0.5"`. Displayed value formats as dB.
- A linked number input sits beside the slider (also in dB). Typing in either control updates the other.
- On `/api/config` load: `rmsToDb(stored)` populates both controls.
- On save: `dbToRms(input)` converted before POST.
- Live RMS preview bar's x-axis becomes -80 to 0 dB; threshold tick uses the same scale.
- No auto-calibrate button. "Re-run setup wizard" link unchanged.

**`gui/templates/dashboard.html` + `gui/static/dashboard.js`:**
- Live RMS bar's x-axis: -80 to 0 dB.
- Threshold marker reads `Audio.Volume_Threshold` from config, converts via `rmsToDb`, positions on the bar.
- Display-only — no interactive controls.

**`gui/static/setup.js`:**
- The `RMS_CEILING = 0.05` constant is deleted; all percentage math goes through `rmsToDb` mapped to -80..0.
- Threshold slider on Screens 2D + 2E uses the shared `min="-80" max="0" step="0.5"` attrs.

## Config schema changes

**Engine + pydantic default tweak only.** `Audio.Volume_Threshold` default changes from `0.015` (≈ -36 dB, an awkward fraction) to `0.01` (= -40 dB exactly). Existing installs keep their stored value; only fresh installs see the new default. This is the only schema-adjacent change.

No new fields. No `Setup_Wizard_State` change. No migration code needed.

## Architecture summary

**Engine (`core/core_engine.py`):**
- New module-level `calibration` state dict.
- Audio callback gains a 2-line append branch.
- `audio_monitor_loop` gains a detection-skip guard during running calibration.
- New `command_listener_loop()` async task started alongside the other loops.
- Default `Volume_Threshold` updated from 0.015 to 0.01.

**Backend (`gui/backend_main.py`):**
- New `_send_cmd` UDS helper.
- New routes: `POST /api/calibrate/start`, `GET /api/calibrate/status`, `POST /api/calibrate/clear`.

**Config (`gui/config_manager.py`):**
- `Volume_Threshold` default updated to match the engine.

**Templates (`gui/templates/`):**
- `setup.html` — Step 2 section restructured into the 5 sub-screens. Markup additions for sub-screen containers, capture buttons + countdown ring, result screen with the three numbers, and the shared slider + number input component.
- `settings.html` — threshold input pair (slider + number) replaces today's slider.
- `dashboard.html` — minimal — bar attribute changes only.

**Static (`gui/static/`):**
- `db_utils.js` (new) or extension to `shell.js` — `rmsToDb`, `dbToRms`, `formatDb`.
- `setup.js` — new sub-step FSM, capture orchestration, polling loop, threshold formula application, clear-on-cancel wiring. The biggest delta in the change set.
- `settings.js` — slider attrs, linked number input wiring, dB ↔ linear conversion on load/save.
- `dashboard.js` — bar scale + threshold tick conversion.

**Tests (`gui/tests/`):**
- `test_db_utils.py` (new) — Python mirror of the JS helper, round-trip + clamp behavior.
- `test_calibrate_api.py` (new) — endpoint tests with a fake UDS listener fixture. Happy path, engine-unreachable → 503, already-running conflict, clear behavior.
- `test_calibrate_engine.py` (new) — direct unit tests of the engine's `calibration` state machine + stats computation, with synthetic RMS samples (no audio device).
- `test_config_round_trip.py` (existing) — add one assertion for the new default.

## Acceptance criteria

- Fresh install: visiting `/` redirects to `/setup`. Step 2 shows the two-button chooser.
- Auto path captures 5 s of noise floor, then 5 s of music, then lands on the result screen with the three numbers + pre-filled slider in dB.
- Closing the wizard mid-capture fires `clear_calibration`; engine state resets (verifiable via `curl /api/calibrate/status`).
- Displayed threshold matches `noise_p99_dB + 0.25 * (music_p10_dB - noise_p99_dB)`, with the 2 dB safety clamp applied when noise and music sit too close.
- Bad-capture path (music quieter than noise): error message rendered, no auto-advance, "Re-do this step" restarts music phase only.
- Manual path: slider + number input + live meter, all in dB. No auto-calibrate UI present.
- Settings threshold control shows dB; editing + saving propagates to engine via existing hot-reload; engine logs new threshold; behavior changes on next audio-loop tick.
- Dashboard live RMS bar uses dB scale; threshold tick at correct position.
- Engine-down: Auto button disabled with tooltip; Manual still selectable; no crash.
- "Re-run setup wizard" link in Settings opens the wizard with all sub-screens functional.
- Stored `Audio.Volume_Threshold` in `config.json` remains linear RMS — verified with a hand-inspection after a wizard save.

## Manual test plan

(On real hardware in Docker, since this is fundamentally an audio feature.)

- [ ] Fresh container, no `config.json`. Walk full auto path with a real record. Verify the suggested threshold makes sense for your -76 dB-ish noise floor.
- [ ] Auto path with needle on runout the whole time (no music in phase 2). Error message appears, no save, "Re-do this step" restarts music phase only — noise floor capture is preserved.
- [ ] Auto path, click X mid-noise-capture. Wizard closes. Restart wizard. `curl http://localhost:8000/api/calibrate/status` returns `{"status": "none", ...}`.
- [ ] Manual path: drop a record, see the dB meter move, set threshold ~3 dB above the floor, continue.
- [ ] After completing wizard, edit threshold in Settings (in dB), save. Engine logs new value within ~2 s. Stop the record; verify silence detection still works on the new threshold.
- [ ] Auto-calibrate with a deliberately hot input (e.g., laptop mic close to a speaker): result lands in -40 to -20 dB range, slider has room on both sides.
- [ ] Pull container's `/dev/snd` device (or stop the engine inside the container): wizard's Auto button disables with tooltip; Manual path still progresses (meter sits at 0).
- [ ] Open the wizard, complete auto-calibrate, save and finish. Reload Dashboard — threshold tick on the live RMS bar sits at the saved dB value.

## Risks / Notes

- **Detection suppression gap.** Detection is suppressed only during the 5 s `running` window. Between captures, if music starts playing and crosses the old (pre-calibration) threshold, the engine could fire a recognition. The wizard's MQTT step hasn't been completed yet at this point so any spurious publish has no destination. Documented; will revisit if it bites.
- **Audio buffer size sensitivity.** ~22 Hz callback rate at the default 1024-frame buffer gives ~110 samples per 5 s window. Drivers that use larger buffers (e.g., 4096 frames) would drop this to ~25-30. Percentiles still work, but precision degrades. If a user reports thin samples, we'd surface it as a warning rather than tightening the buffer (which would affect detection latency too).
- **Audio-thread → asyncio communication.** `deque.append` from the sounddevice audio thread relies on CPython's atomic-append guarantee. Not portable to non-CPython interpreters. Calling out, not addressing.
- **Reverse UDS socket adds startup ordering.** `command_listener_loop()` needs to bind `/tmp/spinsense-cmd.sock` before the backend starts forwarding commands to it. In practice the engine starts first (it's the `&` background in entrypoint.sh, then uvicorn) so this works out, but worth verifying in the manual test plan with a cold container start.
- **dB default change is the one schema-adjacent change.** Existing installs are unaffected (they keep their stored linear value), but the change must land in both `core_engine.py`'s `DEFAULT_CONFIG` and `config_manager.py`'s pydantic default to stay consistent.
