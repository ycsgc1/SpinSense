# Detection Tuning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lower the dB threshold floor to −120 dB, stop brief audio dips from triggering rescans (make `New_Song_Silence_Interval` actually gate rescans), and escalate the sample length across consecutive failed identifications.

**Architecture:** Three mostly-independent changes. (1) Centralize the dB floor constant in `db_utils.js` and widen it to −120. (2) Gate the rescan decision in `core_engine._scan_decision` on `New_Song_Silence_Interval` instead of `silence_counter > 0`. (3) Turn the existing 3-attempt retry loop in `recognize_audio()` into an escalating ladder (base × 1, × 2, × 3) with a configurable wait between attempts. A new `Rescan_Wait_Interval` config setting backs change 3.

**Tech Stack:** Python 3.12 (`unittest`, `asyncio`, `sounddevice`), Pydantic config validation, vanilla JS + Jinja2 templates, Tailwind utility classes.

Reference spec: `docs/superpowers/specs/2026-06-09-detection-tuning-design.md`

**Agreed defaults:** `Rescan_Wait_Interval = 5.0 s`, `New_Song_Silence_Interval = 3.0 s`.

---

### Task 1: Widen the dB floor to −120 (Python mirror + db_utils.js)

The dB floor is duplicated as a magic number. This task moves the canonical floor into `db_utils.js` as `FLOOR_DB` and widens it from −80 to −120. The Python mirror test (`gui/tests/test_db_utils.py`) pins the contract, so we drive the change from that test.

**Files:**
- Test: `gui/tests/test_db_utils.py`
- Modify: `gui/static/db_utils.js`

- [ ] **Step 1: Update the test assertions to expect −120 (leave the mirror function at −80 for now)**

In `gui/tests/test_db_utils.py`, replace the two floor-clamp test methods and add a new one. Do **not** touch the `rms_to_db` mirror function yet — leaving it at −80 is what makes this step go red.

```python
    def test_zero_clamps_to_floor(self):
        self.assertEqual(rms_to_db(0.0), -120.0)
        self.assertEqual(rms_to_db(-0.5), -120.0)

    def test_very_small_clamps_to_floor(self):
        # 10^(-120/20) = 1e-6 — anything quieter than this floors out
        self.assertEqual(rms_to_db(1e-12), -120.0)

    def test_quiet_music_below_old_floor_is_representable(self):
        # -100 dB (rms 1e-5) used to clamp to -80; now it round-trips.
        self.assertAlmostEqual(rms_to_db(1e-5), -100.0, places=6)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest gui/tests/test_db_utils.py -v`
Expected: FAIL — `test_zero_clamps_to_floor` / `test_very_small_clamps_to_floor` expect −120.0 but the mirror still returns −80.0.

- [ ] **Step 3: Widen the floor in the mirror function AND db_utils.js**

First, update the `rms_to_db` mirror at the top of `gui/tests/test_db_utils.py`:

```python
def rms_to_db(rms: float) -> float:
    if rms <= 0:
        return -120.0
    return max(-120.0, 20.0 * math.log10(rms))
```

Then replace the body of `gui/static/db_utils.js` `window.SpinSense.db` with a centralized `FLOOR_DB` (the JS the mirror tracks):

```javascript
(function () {
  if (!window.SpinSense) window.SpinSense = {};
  window.SpinSense.db = {
    // Canonical dB display/threshold floor. Anything quieter clamps here.
    // The Python mirror in gui/tests/test_db_utils.py pins this value.
    FLOOR_DB: -120,
    rmsToDb(rms) {
      if (rms <= 0) return this.FLOOR_DB;
      return Math.max(this.FLOOR_DB, 20 * Math.log10(rms));
    },
    dbToRms(db) {
      return Math.pow(10, db / 20);
    },
    formatDb(db) {
      return `${db.toFixed(1)} dB`;
    },
  };
})();
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest gui/tests/test_db_utils.py -v`
Expected: PASS (all tests, including the new −100 dB representability test).

- [ ] **Step 5: Commit**

```bash
git add gui/tests/test_db_utils.py gui/static/db_utils.js
git commit -m "feat(audio): widen dB floor to -120, centralize as FLOOR_DB"
```

---

### Task 2: Point JS + HTML consumers at the centralized floor

`settings.js`, `setup.js`, and `dashboard.js` each hardcode `const DB_MIN = -80`, and `settings.html` / `setup.html` hardcode `min="-80"`. Point them all at the new floor. There is no JS test runner in this repo, so verification is the Python mirror (Task 1) plus a manual page-load check.

**Files:**
- Modify: `gui/static/settings.js:16`
- Modify: `gui/static/setup.js:60`
- Modify: `gui/static/dashboard.js:40`
- Modify: `gui/templates/settings.html:40,44`
- Modify: `gui/templates/setup.html:177,181,213,217`

- [ ] **Step 1: Reference FLOOR_DB in settings.js**

In `gui/static/settings.js`, replace line 16:

```javascript
  const DB_MIN = -80;
```

with:

```javascript
  const DB_MIN = window.SpinSense.db.FLOOR_DB;
```

Then, so the HTML inputs share one source of truth, add these two lines immediately after `const THRESHOLD_NUMBER = document.getElementById("volume-threshold-number");` (currently line 19):

```javascript
  THRESHOLD_NUMBER.min = String(DB_MIN);
  THRESHOLD_SLIDER.min = String(DB_MIN);
```

- [ ] **Step 2: Reference FLOOR_DB in setup.js and dashboard.js**

In `gui/static/setup.js`, replace line 60 `const DB_MIN = -80;` with `const DB_MIN = window.SpinSense.db.FLOOR_DB;`.

In `gui/static/dashboard.js`, replace line 40 `const DB_MIN = -80;` with `const DB_MIN = window.SpinSense.db.FLOOR_DB;`.

(`db_utils.js` is loaded before every page script via `_layout.html`, so `window.SpinSense.db` is always defined here.)

- [ ] **Step 3: Update the HTML min attributes to −120**

In `gui/templates/settings.html`, change the two threshold inputs (lines 40 and 44) from `min="-80"` to `min="-120"`. Leave `max="0"`, `step`, and `value="-40"` unchanged. For reference, line 44 becomes:

```html
          <input type="range" id="volume-threshold"
                 min="-120" max="0" step="0.5" value="-40"
                 class="w-full accent-primary">
```

In `gui/templates/setup.html`, change all four threshold inputs (lines 177, 181, 213, 217) from `min="-80"` to `min="-120"`. Leave their `max`, `step`, and `value="-40"` unchanged.

- [ ] **Step 4: Verify nothing else still hardcodes −80**

Run: `grep -rn "DB_MIN = -80\|min=\"-80\"\|-80, 20 \* Math\|return -80" gui/static gui/templates`
Expected: no matches.

- [ ] **Step 5: Commit**

```bash
git add gui/static/settings.js gui/static/setup.js gui/static/dashboard.js gui/templates/settings.html gui/templates/setup.html
git commit -m "feat(audio): point threshold UI floor at FLOOR_DB (-120)"
```

---

### Task 3: Config — align New_Song_Silence default + add Rescan_Wait_Interval

Add the `Rescan_Wait_Interval` setting (default 5.0) and align the `New_Song_Silence_Interval` default to 3.0 across both config sources: the Pydantic model (`gui/config_manager.py`) and the engine's `DEFAULT_CONFIG` + runtime mirror (`core/core_engine.py`). Drive the Pydantic side with `test_config_round_trip.py`.

**Files:**
- Test: `gui/tests/test_config_round_trip.py`
- Modify: `gui/config_manager.py:19-24`
- Modify: `core/core_engine.py:28-33` (DEFAULT_CONFIG), `:80-91` (runtime), `:94-100` (_populate_runtime)

- [ ] **Step 1: Write failing config tests**

In `gui/tests/test_config_round_trip.py`, add these two methods to the `ConfigRoundTripTest` class (after `test_retrigger_on_track_change_round_trips`):

```python
    def test_new_song_silence_default_is_3(self):
        defaults = config_manager.get_default_config()
        self.assertEqual(defaults["Audio"]["New_Song_Silence_Interval"], 3.0)

    def test_rescan_wait_interval_default_and_round_trips(self):
        defaults = config_manager.get_default_config()
        self.assertEqual(defaults["Audio"]["Rescan_Wait_Interval"], 5.0)

        cfg = config_manager.get_default_config()
        cfg["Audio"]["Rescan_Wait_Interval"] = 7.5
        self.assertTrue(config_manager.save_config(cfg))
        loaded = config_manager.load_config()
        self.assertAlmostEqual(loaded["Audio"]["Rescan_Wait_Interval"], 7.5)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest gui/tests/test_config_round_trip.py -v`
Expected: FAIL — `New_Song_Silence_Interval` defaults to 10.0 (not 3.0) and `Rescan_Wait_Interval` key does not exist (`KeyError`).

- [ ] **Step 3: Update the Pydantic AudioConfig**

In `gui/config_manager.py`, replace the `AudioConfig` class (lines 19-24) with:

```python
class AudioConfig(BaseModel):
    Volume_Threshold: float = 0.01
    Song_Sample_Length: float = 10.0
    New_Song_Silence_Interval: float = 3.0
    Stopped_Silence_Interval: float = 30.0
    Rescan_Wait_Interval: float = 5.0
    Retrigger_On_Track_Change: bool = False
```

(Only `New_Song_Silence_Interval` changes value and `Rescan_Wait_Interval` is added; `Song_Sample_Length` and `Stopped_Silence_Interval` are left as-is — their cross-file default mismatch is pre-existing and out of scope.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest gui/tests/test_config_round_trip.py -v`
Expected: PASS.

- [ ] **Step 5: Update the engine DEFAULT_CONFIG + runtime mirror**

In `core/core_engine.py`, update the `Audio` block of `DEFAULT_CONFIG` (lines 28-33) to:

```python
    "Audio": {
        "Volume_Threshold": 0.01,
        "Song_Sample_Length": 5.0,
        "New_Song_Silence_Interval": 3.0,
        "Stopped_Silence_Interval": 5.0,
        "Rescan_Wait_Interval": 5.0,
    },
```

In the `runtime` dict (lines 80-91), change `"new_song_silence": 2.0,` to `"new_song_silence": 3.0,` and add `"rescan_wait": 5.0,` directly below `"stopped_silence": 5.0,`.

In `_populate_runtime` (lines 94-100), change the `new_song_silence` default and add the `rescan_wait` line:

```python
    runtime["new_song_silence"] = cfg.get('Audio', {}).get('New_Song_Silence_Interval', 3.0)
    runtime["stopped_silence"]  = cfg.get('Audio', {}).get('Stopped_Silence_Interval', 5.0)
    runtime["rescan_wait"]      = cfg.get('Audio', {}).get('Rescan_Wait_Interval', 5.0)
```

- [ ] **Step 6: Verify the engine module still imports cleanly**

Run: `python -c "import sys; sys.path.insert(0,'core'); import core_engine; print(core_engine.runtime['new_song_silence'], core_engine.runtime['rescan_wait'])"`
Expected: prints the loaded values (e.g. `3.0 5.0`, or whatever the on-disk `config.json` carries) with no exception.

- [ ] **Step 7: Commit**

```bash
git add gui/tests/test_config_round_trip.py gui/config_manager.py core/core_engine.py
git commit -m "feat(config): add Rescan_Wait_Interval (5s), align New_Song_Silence default to 3s"
```

---

### Task 4: Gate rescans on New_Song_Silence_Interval (fix dip-rescan bug)

`_scan_decision` currently rescans whenever volume returns above threshold and `silence_counter > 0`, so a 1-second dip triggers a rescan and `New_Song_Silence_Interval` is dead code. Add `new_song_silence` to the decision and only rescan when the gap has lasted at least that long. Reset `silence_counter` on a `tick` so a sub-threshold dip that ends early doesn't accumulate.

**Files:**
- Test: `core/tests/test_recognition_phases.py` (`ScanDecisionTest`)
- Modify: `core/core_engine.py:658-667` (`_scan_decision`), `:717-740` (monitor loop call + tick branch)

- [ ] **Step 1: Update ScanDecisionTest for the new signature + gate**

In `core/tests/test_recognition_phases.py`, replace the entire `ScanDecisionTest` class with:

```python
class ScanDecisionTest(unittest.TestCase):
    def d(self, vol, thr, in_song, sc, new_song_silence, back_off):
        return core_engine._scan_decision(vol, thr, in_song, sc, new_song_silence, back_off)

    def test_loud_idle_scans(self):
        # New onset (not in a song) always scans, regardless of counter.
        self.assertEqual(self.d(0.5, 0.1, False, 0, 3, False), "scan")

    def test_loud_in_song_steady_ticks(self):
        self.assertEqual(self.d(0.5, 0.1, True, 0, 3, False), "tick")

    def test_brief_dip_below_interval_does_not_rescan(self):
        # 2s gap with a 3s interval — treat as the same song, just tick.
        self.assertEqual(self.d(0.5, 0.1, True, 2, 3, False), "tick")

    def test_gap_at_interval_rescans(self):
        # Gap reached the interval — a new song may have started.
        self.assertEqual(self.d(0.5, 0.1, True, 3, 3, False), "scan")

    def test_gap_past_interval_rescans(self):
        self.assertEqual(self.d(0.5, 0.1, True, 5, 3, False), "scan")

    def test_loud_but_backoff_waits_for_gap(self):
        self.assertEqual(self.d(0.5, 0.1, False, 0, 3, True), "wait_gap")

    def test_quiet_is_silence(self):
        self.assertEqual(self.d(0.0, 0.1, True, 0, 3, False), "silence")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest core/tests/test_recognition_phases.py::ScanDecisionTest -v`
Expected: FAIL — `_scan_decision()` takes 5 positional args but 6 are given (`TypeError`).

- [ ] **Step 3: Add the gate to _scan_decision**

In `core/core_engine.py`, replace `_scan_decision` (lines 658-667) with:

```python
def _scan_decision(vol, threshold, in_song, silence_counter, new_song_silence, back_off):
    """Pure: decide what the monitor loop should do this tick.
    Returns 'scan' | 'tick' | 'wait_gap' | 'silence'.

    A rescan fires on a fresh onset (not in_song) or after a gap that has
    lasted at least `new_song_silence` seconds. A briefer sub-threshold dip
    is treated as the same song still playing (tick), so momentary quiet
    passages don't re-trigger identification."""
    if vol > threshold:
        if back_off:
            return "wait_gap"
        if not in_song:
            return "scan"
        if silence_counter >= new_song_silence:
            return "scan"
        return "tick"
    return "silence"
```

- [ ] **Step 4: Pass new_song_silence in the monitor loop + reset counter on tick**

In `core/core_engine.py`, update the `_scan_decision` call (lines 717-720) to pass the runtime value:

```python
        decision = _scan_decision(
            vol, runtime["threshold"], state["in_song"],
            state["silence_counter"], runtime["new_song_silence"],
            state.get("back_off", False),
        )
```

Then update the `tick` branch (currently line 729-730) to reset the silence counter, so a dip that ends before it qualifies doesn't leave a stale partial count:

```python
        elif decision == "tick":
            state["silence_counter"] = 0  # song resumed before the gap qualified
            print(".", end="", flush=True)
```

Leave the `scan`, `wait_gap`, and `silence` branches unchanged.

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest core/tests/test_recognition_phases.py::ScanDecisionTest -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add core/tests/test_recognition_phases.py core/core_engine.py
git commit -m "fix(audio): gate rescans on New_Song_Silence_Interval, ignore brief dips"
```

---

### Task 5: Escalating rescans after failed identification

Turn the existing 3-attempt loop in `recognize_audio()` into an escalating ladder: attempt _k_ records `base × (k+1)` seconds (capped), with a `Rescan_Wait_Interval` pause between attempts. `_capture_sample()` takes an explicit length so the loop can drive it. Normal songs still match on attempt 0 at base length — unchanged.

**Files:**
- Test: `core/tests/test_recognition_phases.py` (`RecognizeRetryTest` + new `EscalatingSampleTest`)
- Modify: `core/core_engine.py:374` (add max constant nearby), `:517-531` (`_capture_sample`), `:608-633` (`recognize_audio`)

- [ ] **Step 1: Update RecognizeRetryTest fakes + add an escalation test**

In `core/tests/test_recognition_phases.py`, the existing `RecognizeRetryTest.setUp` stubs `_capture_sample` with a zero-arg fake; `_capture_sample` will now be called with a length, so the fake must accept it, and we must zero out the wait so the test doesn't sleep. In `RecognizeRetryTest.setUp`, replace:

```python
        async def fake_capture():
            return b""
```

with:

```python
        async def fake_capture(sample_len):
            return b""
```

and add this line at the end of `setUp` (after `core_engine.state["in_song"] = False`):

```python
        self._orig_wait = core_engine.runtime["rescan_wait"]
        core_engine.runtime["rescan_wait"] = 0  # don't sleep in tests
```

In `RecognizeRetryTest.tearDown`, restore it by adding:

```python
        core_engine.runtime["rescan_wait"] = self._orig_wait
```

Then add a new test class after `RecognizeRetryTest`:

```python
class EscalatingSampleTest(unittest.TestCase):
    def setUp(self):
        self.lengths = []
        self.sleeps = []

        async def fake_phase(p):
            return None

        async def fake_capture(sample_len):
            self.lengths.append(sample_len)
            return b""

        async def always_none(_wav):
            return None

        async def fake_pause(seconds):
            self.sleeps.append(seconds)

        self._orig = (core_engine._publish_phase, core_engine._capture_sample,
                      core_engine._identify, core_engine._rescan_pause)
        self._orig_base = core_engine.runtime["sample_len"]
        self._orig_wait = core_engine.runtime["rescan_wait"]
        core_engine._publish_phase = fake_phase
        core_engine._capture_sample = fake_capture
        core_engine._identify = always_none
        core_engine._rescan_pause = fake_pause
        core_engine.runtime["sample_len"] = 5.0
        core_engine.runtime["rescan_wait"] = 5.0
        core_engine.state["back_off"] = False
        core_engine.state["in_song"] = False

    def tearDown(self):
        (core_engine._publish_phase, core_engine._capture_sample,
         core_engine._identify, core_engine._rescan_pause) = self._orig
        core_engine.runtime["sample_len"] = self._orig_base
        core_engine.runtime["rescan_wait"] = self._orig_wait

    def test_sample_length_escalates_1x_2x_3x(self):
        asyncio.run(core_engine.recognize_audio())
        # base 5s -> 5, 10, 15 across the three attempts.
        self.assertEqual(self.lengths, [5.0, 10.0, 15.0])

    def test_wait_runs_between_attempts_not_after_last(self):
        asyncio.run(core_engine.recognize_audio())
        # Two gaps between three attempts; no trailing sleep.
        self.assertEqual(self.sleeps, [5.0, 5.0])

    def test_cap_limits_runaway_length(self):
        core_engine.runtime["sample_len"] = 30.0  # 3x would be 90s
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.lengths, [30.0, 60.0, 60.0])  # capped at 60
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest core/tests/test_recognition_phases.py::EscalatingSampleTest -v`
Expected: FAIL — `_capture_sample` still takes no args / doesn't escalate (`TypeError` or wrong `lengths`).

- [ ] **Step 3: Add a max-sample constant**

In `core/core_engine.py`, just below `RECOGNIZE_ATTEMPTS = 3` (line 374), add:

```python
_MAX_SAMPLE_SECONDS = 60.0  # ceiling for the escalating rescan ladder
```

- [ ] **Step 4: Make _capture_sample take an explicit length**

In `core/core_engine.py`, change the signature and first line of `_capture_sample` (lines 517-519) from:

```python
async def _capture_sample() -> bytes:
    """Record sample_len seconds from the mic and return WAV bytes."""
    sample_len = runtime["sample_len"]
```

to:

```python
async def _capture_sample(sample_len: float | None = None) -> bytes:
    """Record `sample_len` seconds from the mic and return WAV bytes.
    Falls back to the configured base length when called with no argument."""
    if sample_len is None:
        sample_len = runtime["sample_len"]
```

The rest of the function (mic read, WAV packing) is unchanged.

- [ ] **Step 5: Add the _rescan_pause helper**

In `core/core_engine.py`, add this helper directly above `async def recognize_audio():` (after `_clear_track_state`, ~line 606). It's separated out purely so tests can intercept the wait without touching `asyncio`:

```python
async def _rescan_pause(seconds: float) -> None:
    """Wait between escalating rescan attempts. Isolated for testability."""
    if seconds > 0:
        await asyncio.sleep(seconds)
```

- [ ] **Step 6: Escalate inside recognize_audio**

In `core/core_engine.py`, replace the retry loop in `recognize_audio` (lines 613-620, from `track = None` through the `if track: break`) with:

```python
    base = runtime["sample_len"]
    wait = runtime["rescan_wait"]
    track = None
    for attempt in range(RECOGNIZE_ATTEMPTS):
        if attempt > 0:
            await _rescan_pause(wait)
        await _publish_phase("scanning")
        sample_len = min(base * (attempt + 1), _MAX_SAMPLE_SECONDS)
        wav = await _capture_sample(sample_len)
        await _publish_phase("identifying" if attempt == 0 else "retrying")
        track = await _identify(wav)
        if track:
            break
```

The success/failure handling below it (`if track: ... else: ... _clear_track_state(set_backoff=True)` and the final `state["silence_counter"] = 0`) is unchanged.

- [ ] **Step 7: Run the full recognition test file to verify it passes**

Run: `python -m pytest core/tests/test_recognition_phases.py -v`
Expected: PASS — `EscalatingSampleTest` (lengths `[5,10,15]`, sleeps `[5,5]`, cap `[30,60,60]`) and the existing `RecognizeRetryTest` (phase sequence unchanged, now with zero wait).

- [ ] **Step 8: Commit**

```bash
git add core/tests/test_recognition_phases.py core/core_engine.py
git commit -m "feat(audio): escalate sample length (1x/2x/3x) with wait across failed rescans"
```

---

### Task 6: Add the Rescan_Wait_Interval setting to the Settings UI

Surface the new setting in the Settings form, matching the existing interval inputs and their help-tooltip pattern. The form auto-binds any `[name]` input to config via `settings.js`, so only markup is needed.

**Files:**
- Modify: `gui/templates/settings.html` (after the Song sample length block, ~line 58)

- [ ] **Step 1: Add the input + tooltip**

In `gui/templates/settings.html`, immediately after the closing `</label>` of the "Song sample length" block (line 58) and before the "New-song silence interval" block, insert:

```html
        <label class="block">
          <span class="text-body-sm text-on-surface mb-1 block">Rescan wait interval (seconds)<span class="help-tip" tabindex="0" role="note" aria-label="Help: Rescan wait interval"><span class="material-symbols-outlined help-icon">help</span><span class="help-bubble" role="tooltip">When a track can't be identified, SpinSense waits this many seconds before trying again and records a longer sample each time (1x, then 2x, then 3x the sample length). Longer waits sample more of the song; shorter retries faster.</span></span></span>
          <input type="number" name="Audio.Rescan_Wait_Interval" min="0" max="60" step="0.5" class="form-input">
        </label>
```

- [ ] **Step 2: Verify the field is present and named correctly**

Run: `grep -n "Audio.Rescan_Wait_Interval" gui/templates/settings.html`
Expected: one match on the new `<input>` line.

> Manual check (if running the app): load `/settings`, confirm "Rescan wait interval" shows its saved value (5), the "?" bubble appears on hover/focus, editing it enables Save, and a save round-trips (reload shows the new value).

- [ ] **Step 3: Commit**

```bash
git add gui/templates/settings.html
git commit -m "feat(settings): expose Rescan_Wait_Interval with help tooltip"
```

---

### Task 7: Changelog entry

**Files:**
- Modify: `CHANGELOG.md` (add a new `## [Unreleased]` section at the top, above `## [1.2.1.0]`)

- [ ] **Step 1: Add the Unreleased section**

In `CHANGELOG.md`, insert directly after the intro paragraph (line 3) and before `## [1.2.1.0] - 2026-06-07`:

```markdown
## [Unreleased]

### Added
- **Escalating rescans.** When a track can't be identified, SpinSense now waits a configurable `Rescan_Wait_Interval` (default 5 s) and retries with a progressively longer sample — 1×, then 2×, then 3× the sample length (capped at 60 s) — before backing off. New Settings field with help tooltip.

### Changed
- **dB threshold floor lowered from −80 dB to −120 dB** across the threshold slider, level meters, and auto-calibration, so quiet music on low-noise-floor line-level hardware can be distinguished from silence. The floor is now a single `FLOOR_DB` constant in `db_utils.js`.
- **`New_Song_Silence_Interval` default aligned to 3 s** across the engine and the config validator (previously 2 s vs 10 s).

### Fixed
- **Brief audio dips no longer re-trigger identification.** A momentary drop below the threshold (e.g. a quiet passage) was rescanning the track almost immediately, ignoring the configured silence interval. Rescans now fire only after a gap lasting at least `New_Song_Silence_Interval` seconds; `New_Song_Silence_Interval` previously had no effect at all.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for detection tuning (dB floor, dip fix, escalating rescans)"
```

---

### Final verification

- [ ] **Run the full affected test suites**

Run: `python -m pytest core/tests/test_recognition_phases.py gui/tests/test_db_utils.py gui/tests/test_config_round_trip.py -v`
Expected: all PASS.

- [ ] **Confirm no stale −80 floor remains**

Run: `grep -rn "\-80" gui/static/db_utils.js gui/static/settings.js gui/static/setup.js gui/static/dashboard.js gui/templates/settings.html gui/templates/setup.html`
Expected: no matches referencing the floor (a `max="0"` or unrelated text is fine; there should be no `-80` floor literals).

---

## Notes for the implementer

- **Process:** the engine (`core/core_engine.py`) and the GUI (`gui/`) are separate processes that read the same `config.json`. The engine reads via its own `DEFAULT_CONFIG` fallback + `.get(..., default)`; the GUI validates/fills via Pydantic. That's why the new setting and the aligned default are added in **both** places — neither is redundant.
- **Backward compatibility:** existing `config.json` files without `Rescan_Wait_Interval` keep working — the engine falls back to 5.0 via `.get(...)`, and the Pydantic model fills 5.0 on load. Users' existing tuned values are not migrated or overwritten.
- **Out of scope:** the `Song_Sample_Length` (5.0 vs 10.0) and `Stopped_Silence_Interval` (5.0 vs 30.0) default mismatches between `core_engine.DEFAULT_CONFIG` and the Pydantic model are pre-existing and intentionally left untouched.
- **Meter rescale (expected, not a bug):** widening the floor to −120 stretches the live level meters — a −80 dB signal that used to sit at 0% fill now sits at ~33%. This is the agreed behavior (more resolution in the usable range).
