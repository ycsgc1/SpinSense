# Setup Wizard + Tier 3 Hot-Reload (Phase 4)

**Date:** 2026-05-27
**Status:** Drafted, awaiting approval
**Scope:** Final pass of the four-phase frontend redesign. Two threads landing together:
1. **Tier 3 hot-reload** — the engine watches `config.json` and re-applies every setting (audio thresholds, MQTT broker, mic device) without a container restart.
2. **Setup Wizard** — first-run guided onboarding at `/setup` with pending/skipped/completed state.

## Context

Phases 1–3 landed in PRs #2 and #3. Settings + History are live; the "restart required" banner in Settings is technically correct today but becomes obsolete the moment hot-reload lands. With MQTT having moved to Settings in Phase 2, the Wizard's job shrinks to "guide the user through their first calibration."

The hot-reload work is paired with the Wizard for two reasons: (a) the Wizard's threshold-calibration step is much more usable when the engine actually reacts to slider movement, and (b) deferring the "restart required" banner removal would otherwise leave the Settings page lying about a constraint we no longer have.

## Goals

- File-watch hot-reload in `core_engine.py`. Every config field that the engine reads now lives in a mutable `runtime` dict; a 2-second mtime poll on `config.json` re-reads and applies changes.
- Audio thresholds (`Volume_Threshold`, `Song_Sample_Length`, `New_Song_Silence_Interval`, `Stopped_Silence_Interval`) update instantly — picked up on the audio loop's next iteration.
- MQTT broker fields trigger an auto-reconnect: disconnect the current client, reconnect with new host/port/auth, re-announce HA discovery. Brief connection blip is acceptable.
- Mic device change triggers an audio-stream rebuild: stop and close the current `sd.InputStream`, open a new one with the new device. Audio loop pauses for a beat, no crash.
- New `/setup` page replaces the Phase 1 stub with a multi-step guided wizard.
- New config field `System.Setup_Wizard_State` ∈ `{"pending", "skipped", "completed"}` drives the auto-show behavior.
- Settings page loses the "Restart required" banner and gains a "Re-run setup wizard" link in the Hardware section.

## Non-goals

- A migration story for old `config.json` files that lack `Setup_Wizard_State`. Default to `"pending"` on missing — that's the desired behavior anyway (existing users see the wizard once).
- Process restart of the engine. Even if hot-reload fails mid-flight, the engine stays up; failures surface in logs.
- Visual polish on the wizard beyond what Phase 1's shell already provides. Stick to the M3 dark tokens already in `_layout.html`.
- Touching the hardcoded MQTT.Topics + Discovery_Topic dead-code paths in the engine. Still out of scope.
- Migrating `config_manager.py` off pydantic v1 `.dict()` calls. Still deprecated; still out of scope.

## Engine hot-reload (Tier 3)

### Runtime config dict

Replace the module-level `THRESHOLD`, `MIC_DEVICE`, `MQTT_HOST`, etc. constants with a single mutable dict initialised from config at startup:

```python
runtime = {
    "threshold": ...,
    "sample_len": ...,
    "new_song_silence": ...,
    "stopped_silence": ...,
    "mic_device": ...,
    "mqtt_host": ...,
    "mqtt_port": ...,
    "mqtt_user": ...,
    "mqtt_pass": ...,
}
```

All read sites in the engine (audio loop, `recognize_audio`, `connect_mqtt_loop`) move from `THRESHOLD` to `runtime["threshold"]`, etc. This is a search-and-replace inside `core_engine.py` only — no other module touches these constants.

### File watcher

A new async task `config_watch_loop()` started alongside `connect_mqtt_loop()` and `audio_monitor_loop()`. Logic:

```
last_mtime = os.path.getmtime(CONFIG_PATH)
loop:
    await asyncio.sleep(2)
    try:
        m = os.path.getmtime(CONFIG_PATH)
    except OSError:
        continue
    if m == last_mtime:
        continue
    new_cfg = load_json(CONFIG_PATH)
    apply_config_diff(new_cfg)  # see below
    last_mtime = m
```

### `apply_config_diff(new_cfg)`

Diffs the new config against the current `runtime` dict and dispatches changes by category:

- **Audio fields** — just update `runtime[*]`. The audio loop picks them up on its next iteration. Log: `"⚙️ Audio thresholds reloaded"`.
- **MQTT broker fields** — if any of host/port/user/pass changed, set `MQTT_ENABLED = False`, call `mqtt_client.loop_stop()` and `mqtt_client.disconnect()`, update `runtime[*]`, call `mqtt_client.username_pw_set(new_user, new_pass)` if creds are non-empty, then re-enter `connect_mqtt_loop()` via `asyncio.create_task`. Log: `"📡 MQTT broker changed, reconnecting…"`.
- **Mic device** — set a module-level `asyncio.Event` `mic_change_event`. The audio loop checks this event each iteration; when set, it stops and closes the current stream, reads `runtime["mic_device"]`, opens a new stream, and clears the event. Log: `"🎤 Mic device changed to <name>, restarting audio stream"`.

The diff-and-dispatch separation keeps each change-handler isolated and lets us add fields later without growing one giant `if/elif`.

### Audio loop refactor

The audio loop already rebuilds the stream after `recognize_audio()` (lines 288-297 today). Factor that into a `restart_audio_stream()` helper, then call it from both the existing "song finished" path and the new "mic device changed" path. Side benefit: the loop becomes easier to read.

### Concurrency notes

- The `mqtt_client.loop_start()` thread is paho's, not ours. `loop_stop()` joins it. Calling these from the asyncio loop is safe because they don't block on the event loop.
- `sd.InputStream` operations (`stop`, `close`, `start`) are blocking but quick (~50ms). Calling them inline from the asyncio loop is acceptable; the rest of the loop is already on a 1-second cadence so the blocking is invisible.
- `asyncio.Event` is cooperative — only the audio loop polls it, so there's no race.

## Setup Wizard

### State machine

New config field `System.Setup_Wizard_State` of type `str`. The pydantic model adds:

```python
class SystemConfig(BaseModel):
    Auto_Start: bool = False
    Engine_Status: str = "stopped"
    Setup_Wizard_State: Literal["pending", "skipped", "completed"] = "pending"
```

Routing behavior, implemented in a tiny middleware on the FastAPI side:

| State        | Visiting `/`, `/history`, `/settings` | Visiting `/setup` |
|--------------|----------------------------------------|-------------------|
| `pending`    | 307-redirect to `/setup`               | Render wizard     |
| `skipped`    | Render normally                        | Render wizard     |
| `completed`  | Render normally                        | Render wizard     |

`/api/*` and `/static/*` and `/art/*` are always allowed through. The middleware reads the state on each request via `load_config()`. Cheap because the file is local + tiny.

### Wizard structure

Five steps, each a `<section>` that's shown/hidden by `wizard.js`. No multi-page navigation — keeps state cheap and lets the user back-step without losing form values.

1. **Welcome** — title, a sentence about what SpinSense is, a "Get started" button. An "X" close button in the top-right and a "Skip setup" link at the bottom.
2. **Microphone** — same dropdown as Settings, populated from `/api/devices`. "Continue" + "Back".
3. **Threshold calibration** — same slider + live RMS preview as Settings, but at a larger size with instructional copy. "Continue" + "Back".
4. **MQTT** — same four broker fields plus a **"Test connection"** button. Clicking it POSTs the four fields to `/api/mqtt/test` which attempts a paho connect with a 3s timeout in `asyncio.to_thread`. The button shows a spinner while in-flight, then either a green "Connected ✓" line or a red error popup with `{ "Try again", "Skip MQTT" }`. The user can "Continue" any time; the test is opt-in, not gating. A "Skip MQTT" link at the bottom of the step also advances without changing MQTT fields.
5. **Done** — confirmation, "Save and finish" button. Posts the wizard's config + sets `Setup_Wizard_State = "completed"`.

The wizard reuses `/api/config` (already validates). On any step, the user can:
- Click **X** (top-right of the wizard frame) → close wizard, stay on the current page (or redirect to `/` if accessed via auto-redirect). Wizard state is NOT changed — comes back next page load if still pending.
- Click **Skip setup** (footer link) → POST `/api/config` with only `System.Setup_Wizard_State = "skipped"`. Wizard does not auto-show again. Manual re-entry via Settings still works.
- Complete the final step → POST with current form values + `System.Setup_Wizard_State = "completed"`.

### Settings updates

- Remove the "Restart required" banner block from `settings.html`.
- Change the save toast wording: "Saved" → "Saved and applied" on success.
- Add a row at the top of the Hardware section: "First time? Re-run the setup wizard." with a link to `/setup`. The link works regardless of `Setup_Wizard_State`.
- Add a "Test connection" button to the MQTT Broker section that hits the same `/api/mqtt/test` endpoint and renders inline success/error feedback (no popup needed inside Settings, just an adjacent status line).

## Architecture

### File Layout

**Engine (`core/core_engine.py`):**
- Replace module constants with `runtime` dict.
- Add `config_watch_loop()`, `apply_config_diff()`, `restart_audio_stream()`, `mic_change_event`.
- Add `if __name__ == "__main__":` startup that creates all three tasks (`connect_mqtt_loop`, `audio_monitor_loop`, `config_watch_loop`).

**Backend (`gui/backend_main.py`):**
- New middleware function that gates non-setup routes when `Setup_Wizard_State == "pending"`.
- `/api/setup-state` (GET) — returns `{"state": "..."}`. Used by the wizard to know whether to show "Get started" vs "Re-run".
- `/api/mqtt/test` (POST) — accepts `{host, port, user, password}`, tries `paho.Client.connect()` in a thread with a 3-second timeout, returns `{"ok": true}` or `{"ok": false, "detail": "..."}`. The client is created locally inside the handler (separate from the running engine's client) so a test never disturbs an active production connection. Connection is closed regardless of outcome.

**Config (`gui/config_manager.py`):**
- Add `Setup_Wizard_State` field to `SystemConfig` with `Literal["pending", "skipped", "completed"]` default `"pending"`.

**Templates (`gui/templates/`):**
- `setup.html` — replaced. Hosts all five wizard steps; one visible at a time.
- `settings.html` — minor edits: remove restart banner, add re-run wizard row, tweak toast wording.

**Static (`gui/static/`):**
- `setup.js` — new. Step navigation, form binding, save-and-finish, skip, X-close logic.
- `settings.js` — toast wording tweak only.

**Tests (`gui/tests/`):**
- `test_config_round_trip.py` — verify `Setup_Wizard_State` defaults to `"pending"`, accepts the three legal values, rejects others.
- A small unit test for `apply_config_diff()` is tempting but the function is tightly coupled to live MQTT + audio objects; covering it would need extensive mocking. Defer — verify in manual hardware test instead.

## Acceptance Criteria

- Editing any Audio field in `/settings` and clicking Save changes engine behavior on the next audio-loop iteration (verified by watching engine logs and the live RMS preview reflecting the new threshold).
- Editing MQTT host (e.g. swap to a known-bad host) and saving disconnects + reconnects within ~5s. Reverting reconnects again. No container restart.
- Editing the mic device and saving stops the audio stream and restarts with the new device within ~2s. Engine logs the device change.
- Fresh install (no `config.json` or `Setup_Wizard_State` missing): visiting `/`, `/history`, or `/settings` redirects to `/setup`.
- "Skip setup" sets `Setup_Wizard_State` to `"skipped"`. No more auto-redirects.
- Completing the wizard sets state to `"completed"`. No more auto-redirects. Config values from the wizard are saved.
- Closing with X (not Skip) leaves state as `"pending"` and the auto-redirect still fires on next page load.
- Visiting `/setup` directly always renders the wizard, regardless of state.
- The "Re-run setup wizard" link in Settings goes to `/setup`.
- Test suite: 16 existing + ~3 new for `Setup_Wizard_State` validation = 19 total.

## Manual Test Plan

(Inside Docker on real hardware.)

- [ ] Delete `data/config.json`. Container restart. Browser to `/` → redirects to `/setup`. All four steps + Done renders.
- [ ] Walk through the wizard, set mic + threshold + MQTT, click Save and finish. `/` loads normally now. `data/config.json` shows `Setup_Wizard_State: "completed"`.
- [ ] Edit `Volume_Threshold` in `/settings`, save. Engine logs `"⚙️ Audio thresholds reloaded"`. Stop the spinning record; engine logs the new threshold being checked.
- [ ] Edit `MQTT.Broker.Host` to `127.0.0.1` (an unreachable broker). Engine logs disconnect + retry loop. Revert. Reconnect within 10s.
- [ ] Pick a non-default mic in `/settings`, save. Engine logs `"🎤 Mic device changed"` and a fresh stream. RMS preview keeps moving.
- [ ] Visit `/setup` directly. Click X. Lands on `/`. Reload `/` — no redirect (state is still `completed`).
- [ ] On the wizard's MQTT step, type a known-bad host (e.g. `10.255.255.1`) and click "Test connection". Within ~3s a red error popup appears with "Try again" / "Skip MQTT". Clicking "Skip MQTT" advances the wizard without writing MQTT fields.
- [ ] On the same step, type a known-good host and click "Test connection". Green "Connected ✓" appears.
- [ ] Edit `config.json` directly on disk to set `Setup_Wizard_State: "pending"`. Reload `/`. Redirects to `/setup` within ~2s (file watcher).

## Risks / Notes

- Tier 3 hot-reload is real concurrency work. The audio stream rebuild touches OS audio drivers; a transient `OSError` is plausible. The implementation wraps it in try/except and logs; on failure the engine logs and continues with the old device — never crashes.
- The MQTT reconnect path involves disconnecting paho's loop thread. If reconnect immediately fails, the existing `connect_mqtt_loop` retry-with-backoff handles it.
- The new middleware runs on EVERY request, including `/static/*`. Path-prefix check short-circuits before reading the config to keep static asset latency negligible.
- `Literal[...]` in pydantic v2 needs `from typing import Literal`. Already in stdlib since 3.8.
