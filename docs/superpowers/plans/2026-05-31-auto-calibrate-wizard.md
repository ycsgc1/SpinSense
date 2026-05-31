# Auto-Calibrate Wizard + dBFS Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a two-phase auto-calibrate flow to setup wizard Step 2 (noise-floor capture → music capture → suggested threshold) and switch every volume threshold UI in the app from linear RMS to dBFS display, while keeping the stored value in `config.json` linear.

**Architecture:** A new module-level `calibration` state in `core_engine.py` accumulates RMS samples piggybacked off the existing audio callback; a 5 s timer task computes percentile stats; a new UDS socket `/tmp/spinsense-cmd.sock` lets the backend send `start_calibration` / `get_calibration` / `clear_calibration` commands. The wizard's Step 2 becomes a five-substep FSM (chooser → noise capture → music capture → result → manual). dB conversion lives in one JS helper used by the wizard, Settings, and Dashboard.

**Tech Stack:** Python 3.11 (asyncio, sounddevice, pydantic v1-style models on v2), FastAPI, vanilla JS, Tailwind utility classes (already loaded by `_layout.html`), unittest for Python tests.

---

## File Structure

**Engine (`core/`):**
- Modify `core_engine.py` — add `calibration` state + helpers, audio-callback append branch, detection-suppression guard, `command_listener_loop()`, update default `Volume_Threshold`.
- Create `core/tests/__init__.py`, `core/tests/test_calibrate_engine.py` — direct unit tests of calibration state machine + stats.

**Backend (`gui/`):**
- Modify `backend_main.py` — add `_send_cmd` helper + module-level `CMD_SOCKET_PATH`, three new `/api/calibrate/*` endpoints.
- Modify `config_manager.py` — bump `Volume_Threshold` default to match engine.

**Frontend templates (`gui/templates/`):**
- Modify `setup.html` — replace Step 2 section with five sub-screens.
- Modify `settings.html` — swap threshold slider to dB range, add linked number input.
- Modify `dashboard.html` — no markup changes (display already exists; only JS scale shifts).

**Frontend static (`gui/static/`):**
- Create `db_utils.js` — `rmsToDb`, `dbToRms`, `formatDb` on `window.SpinSense.db`.
- Modify `_layout.html` to load `db_utils.js` before page scripts (one-line `<script>` add). Note: `_layout.html` is a template; loading is template-side, not static-side, but the file is the right place.
- Modify `setup.js` — rewrite Step 2 control flow as substep FSM with capture orchestration.
- Modify `settings.js` — switch threshold display + persist conversion.
- Modify `dashboard.js` — switch RMS bar to dB scale; remove local `rmsToDb` duplicate.

**Tests (`gui/tests/`):**
- Create `test_db_utils.py` — Python mirror of the JS helper functions with round-trip + clamp assertions.
- Create `test_calibrate_api.py` — endpoint tests against a fake UDS listener fixture.
- Modify `test_config_round_trip.py` — add one assertion for the new default value.

---

## Task 1: dB conversion helpers + tests

**Files:**
- Create: `gui/static/db_utils.js`
- Create: `gui/tests/test_db_utils.py`
- Modify: `gui/templates/_layout.html` — add `<script src="/static/db_utils.js"></script>` before the existing `shell.js` script tag.

- [ ] **Step 1: Write the failing Python test (mirror of the JS helper)**

Create `gui/tests/test_db_utils.py`:
```python
"""Python mirror of gui/static/db_utils.js, used to assert the conversion
behavior we depend on across the wizard, Settings, and Dashboard. The JS
implementation is not directly executable here; we mirror its math and
test the mirror. Any change to db_utils.js must be paralleled here."""
import math
import unittest


def rms_to_db(rms: float) -> float:
    if rms <= 0:
        return -80.0
    return max(-80.0, 20.0 * math.log10(rms))


def db_to_rms(db: float) -> float:
    return 10.0 ** (db / 20.0)


def format_db(db: float) -> str:
    return f"{db:.1f} dB"


class DbUtilsTest(unittest.TestCase):
    def test_zero_clamps_to_floor(self):
        self.assertEqual(rms_to_db(0.0), -80.0)
        self.assertEqual(rms_to_db(-0.5), -80.0)

    def test_very_small_clamps_to_floor(self):
        # 10^(-80/20) = 1e-4 — anything quieter than this floors out
        self.assertEqual(rms_to_db(1e-9), -80.0)

    def test_unity_is_zero_db(self):
        self.assertAlmostEqual(rms_to_db(1.0), 0.0, places=6)

    def test_known_conversions(self):
        # 0.0002 ≈ -73.98 dB (the user's working threshold)
        self.assertAlmostEqual(rms_to_db(0.0002), -73.9794, places=3)
        # 0.01 = -40 dB exactly
        self.assertAlmostEqual(rms_to_db(0.01), -40.0, places=6)
        # 0.1 = -20 dB exactly
        self.assertAlmostEqual(rms_to_db(0.1), -20.0, places=6)

    def test_round_trip(self):
        for db in (-80.0, -60.0, -40.0, -20.0, -1.0, 0.0):
            self.assertAlmostEqual(rms_to_db(db_to_rms(db)), db, places=6)

    def test_format_db(self):
        self.assertEqual(format_db(-61.5), "-61.5 dB")
        self.assertEqual(format_db(0.0), "0.0 dB")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the failing test**

Run: `cd /home/ubuntu/SpinSense/SpinSense && python -m unittest gui.tests.test_db_utils -v`
Expected: PASS (the test is self-contained — its assertions are against the in-test helper functions). This step exists to confirm the test file is importable and the math is correct before we hand-write the JS twin.

- [ ] **Step 3: Create the JS helper to match**

Create `gui/static/db_utils.js`:
```js
// db_utils.js — shared dBFS conversion. Loaded before any page-specific
// script via _layout.html so window.SpinSense.db is always available.
//
// The Python mirror in gui/tests/test_db_utils.py pins the contract.
(function () {
  if (!window.SpinSense) window.SpinSense = {};
  window.SpinSense.db = {
    rmsToDb(rms) {
      if (rms <= 0) return -80;
      return Math.max(-80, 20 * Math.log10(rms));
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

- [ ] **Step 4: Wire it into the layout**

Read `gui/templates/_layout.html` first to find the existing `<script src="/static/shell.js"></script>` tag, then add a new line directly above it:
```html
<script src="/static/db_utils.js"></script>
<script src="/static/shell.js"></script>
```
(Order matters — page scripts run after `shell.js`, and `shell.js` does not currently use `db`, but other scripts will.)

- [ ] **Step 5: Re-run tests + commit**

Run: `cd /home/ubuntu/SpinSense/SpinSense && python -m unittest gui.tests.test_db_utils -v`
Expected: PASS

```bash
cd /home/ubuntu/SpinSense/SpinSense
git add gui/static/db_utils.js gui/tests/test_db_utils.py gui/templates/_layout.html
git commit -m "feat: dBFS conversion helper + Python mirror tests"
```

---

## Task 2: Align Volume_Threshold default to 0.01 (= -40 dB)

**Files:**
- Modify: `core/core_engine.py` (DEFAULT_CONFIG and the `runtime` initializer)
- Modify: `gui/config_manager.py` (AudioConfig default)
- Modify: `gui/tests/test_config_round_trip.py` (add assertion)

- [ ] **Step 1: Write the failing test**

Add to `gui/tests/test_config_round_trip.py` inside `ConfigRoundTripTest`:
```python
    def test_default_volume_threshold_is_minus_40_db(self):
        # 0.01 = -40 dB exactly; cleaner than 0.0062 / 0.015 once we display in dB.
        defaults = config_manager.get_default_config()
        self.assertEqual(defaults["Audio"]["Volume_Threshold"], 0.01)
```

- [ ] **Step 2: Run the test, confirm it fails**

Run: `cd /home/ubuntu/SpinSense/SpinSense && python -m unittest gui.tests.test_config_round_trip.ConfigRoundTripTest.test_default_volume_threshold_is_minus_40_db -v`
Expected: FAIL with `AssertionError: 0.0062 != 0.01`

- [ ] **Step 3: Change the pydantic default**

Edit `gui/config_manager.py`, in `class AudioConfig`:
```python
class AudioConfig(BaseModel):
    Volume_Threshold: float = 0.01
    Song_Sample_Length: float = 10.0
    New_Song_Silence_Interval: float = 10.0
    Stopped_Silence_Interval: float = 30.0
```
(Only the first line changes.)

- [ ] **Step 4: Change the engine default to match**

Edit `core/core_engine.py`, in `DEFAULT_CONFIG`:
```python
    "Audio": {
        "Volume_Threshold": 0.01,
        "Song_Sample_Length": 5.0,
        "New_Song_Silence_Interval": 2.0,
        "Stopped_Silence_Interval": 5.0,
    },
```
(Only the first numeric value changes.)

And update the `runtime` dict initial value at the top of the file:
```python
runtime = {
    "threshold": 0.01,
    "sample_len": 5.0,
    ...
}
```

- [ ] **Step 5: Re-run all config tests + commit**

Run: `cd /home/ubuntu/SpinSense/SpinSense && python -m unittest gui.tests.test_config_round_trip -v`
Expected: All tests PASS, including the new one.

```bash
cd /home/ubuntu/SpinSense/SpinSense
git add core/core_engine.py gui/config_manager.py gui/tests/test_config_round_trip.py
git commit -m "feat: bump Volume_Threshold default to 0.01 (-40 dB)"
```

---

## Task 3: Engine — calibration state + audio-callback piggyback

**Files:**
- Modify: `core/core_engine.py` — add `from collections import deque`, module-level `calibration` var, extend `audio_callback`.
- Create: `core/tests/__init__.py` (empty file, makes the directory a package).
- Create: `core/tests/test_calibrate_engine.py` — direct unit tests.

- [ ] **Step 1: Create the test package + write failing tests**

Create `core/tests/__init__.py` (empty).

Create `core/tests/test_calibrate_engine.py`:
```python
"""Unit tests for the calibration state machine in core_engine.py.

These tests instantiate the module and poke its globals directly; the engine
isn't designed for instance-based testing, so we keep tests serial (no
parallelism) and reset state in tearDown."""
import os
import sys
import unittest
from collections import deque

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

import core_engine  # noqa: E402


class CalibrationStateTest(unittest.TestCase):
    def setUp(self):
        core_engine.calibration = None

    def tearDown(self):
        core_engine.calibration = None

    def test_callback_does_not_append_when_calibration_is_none(self):
        core_engine.calibration = None
        indata = np.array([[0.5], [0.5]], dtype=np.float32)
        core_engine.audio_callback(indata, 2, None, None)
        # No crash, no state change.
        self.assertIsNone(core_engine.calibration)

    def test_callback_appends_when_running(self):
        core_engine.calibration = {
            "phase": "noise_floor",
            "samples": deque(),
            "started_at": 0.0,
            "duration": 5.0,
            "status": "running",
            "stats": None,
        }
        indata = np.array([[0.5], [0.5]], dtype=np.float32)
        core_engine.audio_callback(indata, 2, None, None)
        self.assertEqual(len(core_engine.calibration["samples"]), 1)
        self.assertAlmostEqual(core_engine.calibration["samples"][0], 0.5, places=4)

    def test_callback_does_not_append_when_status_done(self):
        core_engine.calibration = {
            "phase": "noise_floor",
            "samples": deque(),
            "started_at": 0.0,
            "duration": 5.0,
            "status": "done",
            "stats": {"samples_count": 0},
        }
        indata = np.array([[0.5], [0.5]], dtype=np.float32)
        core_engine.audio_callback(indata, 2, None, None)
        self.assertEqual(len(core_engine.calibration["samples"]), 0)

    def test_callback_still_updates_state_current_rms(self):
        """The existing live-meter RMS publish must keep working independently
        of calibration state."""
        core_engine.calibration = None
        indata = np.array([[0.3], [0.3], [0.3]], dtype=np.float32)
        core_engine.audio_callback(indata, 3, None, None)
        self.assertAlmostEqual(core_engine.state["current_rms"], 0.3, places=4)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the failing tests**

Run: `cd /home/ubuntu/SpinSense/SpinSense && python -m unittest core.tests.test_calibrate_engine -v`
Expected: FAIL with `AttributeError: module 'core_engine' has no attribute 'calibration'` (and `audio_callback` is also currently nested inside `audio_monitor_loop` — it won't be importable yet).

- [ ] **Step 3: Add the calibration state + extract audio_callback**

Edit `core/core_engine.py`. At the top, add to existing imports:
```python
from collections import deque
```

Below the `mic_change_event = asyncio.Event()` line, add:
```python
# Active calibration session, or None. The audio callback appends per-buffer
# RMS to ["samples"] when status == "running"; a one-shot timer task flips
# status to "done" after ["duration"] seconds and populates ["stats"].
# Cleared by the wizard via the clear_calibration command after the user
# reads the result.
calibration: dict | None = None
```

Currently `audio_callback` is defined *inside* `audio_monitor_loop`. Move it to module scope so tests can import it. Find this block in `audio_monitor_loop`:
```python
    def audio_callback(indata, frames, time, status):
        rms = np.sqrt(np.mean(indata ** 2))
        state["current_rms"] = float(rms)
```
Cut it. Add at module scope (above `audio_monitor_loop` — between `_open_input_stream` and `audio_monitor_loop` is a good spot):
```python
def audio_callback(indata, frames, time, status):
    """Runs on the sounddevice audio thread. Updates the GUI's live RMS
    reading every buffer, and — when a calibration session is collecting —
    appends the per-buffer RMS to its samples deque. deque.append is atomic
    in CPython, safe to call from this thread."""
    rms = float(np.sqrt(np.mean(indata ** 2)))
    state["current_rms"] = rms
    if calibration is not None and calibration["status"] == "running":
        calibration["samples"].append(rms)
```

Inside `audio_monitor_loop`, leave the call sites intact (`_open_input_stream(audio_callback)` still works — it's now a module-level reference).

- [ ] **Step 4: Re-run the tests**

Run: `cd /home/ubuntu/SpinSense/SpinSense && python -m unittest core.tests.test_calibrate_engine -v`
Expected: All four tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/ubuntu/SpinSense/SpinSense
git add core/core_engine.py core/tests/__init__.py core/tests/test_calibrate_engine.py
git commit -m "feat(engine): calibration state + audio callback sample piggyback"
```

---

## Task 4: Engine — _compute_stats + _finish_calibration timer task

**Files:**
- Modify: `core/core_engine.py` — add `_compute_stats` (pure function) and `_finish_calibration` (async coroutine).
- Modify: `core/tests/test_calibrate_engine.py` — tests for the stats math.

- [ ] **Step 1: Write the failing stats tests**

Add to `core/tests/test_calibrate_engine.py`, alongside `CalibrationStateTest`:
```python
class ComputeStatsTest(unittest.TestCase):
    def test_empty_samples_returns_zeros(self):
        stats = core_engine._compute_stats([])
        self.assertEqual(stats["samples_count"], 0)
        self.assertEqual(stats["min"], 0.0)
        self.assertEqual(stats["max"], 0.0)
        self.assertEqual(stats["mean"], 0.0)
        self.assertEqual(stats["p10"], 0.0)
        self.assertEqual(stats["p50"], 0.0)
        self.assertEqual(stats["p99"], 0.0)

    def test_known_samples(self):
        samples = list(range(1, 11))  # 1..10
        stats = core_engine._compute_stats([float(s) for s in samples])
        self.assertEqual(stats["samples_count"], 10)
        self.assertEqual(stats["min"], 1.0)
        self.assertEqual(stats["max"], 10.0)
        self.assertEqual(stats["mean"], 5.5)
        # linear-interp percentiles on 1..10 (indices 0..9):
        # p10 -> 0.9 -> 1.0 + 0.9*(2-1) = 1.9
        # p50 -> 4.5 -> 5 + 0.5*(6-5) = 5.5
        # p99 -> 8.91 -> 9.91
        self.assertAlmostEqual(stats["p10"], 1.9, places=6)
        self.assertAlmostEqual(stats["p50"], 5.5, places=6)
        self.assertAlmostEqual(stats["p99"], 9.91, places=6)

    def test_single_sample(self):
        stats = core_engine._compute_stats([0.42])
        self.assertEqual(stats["samples_count"], 1)
        self.assertEqual(stats["min"], 0.42)
        self.assertEqual(stats["max"], 0.42)
        self.assertEqual(stats["mean"], 0.42)
        self.assertEqual(stats["p10"], 0.42)
        self.assertEqual(stats["p99"], 0.42)
```

And a test for the finish coroutine:
```python
import asyncio


class FinishCalibrationTest(unittest.TestCase):
    def setUp(self):
        core_engine.calibration = None

    def tearDown(self):
        core_engine.calibration = None

    def test_finish_marks_done_and_computes_stats(self):
        session = {
            "phase": "noise_floor",
            "samples": deque([0.001, 0.002, 0.003, 0.004, 0.005]),
            "started_at": 0.0,
            "duration": 0.01,  # tiny so the test runs fast
            "status": "running",
            "stats": None,
        }
        core_engine.calibration = session
        asyncio.run(core_engine._finish_calibration(session))
        self.assertEqual(session["status"], "done")
        self.assertIsNotNone(session["stats"])
        self.assertEqual(session["stats"]["samples_count"], 5)
        self.assertEqual(session["stats"]["min"], 0.001)
        self.assertEqual(session["stats"]["max"], 0.005)

    def test_finish_no_op_if_session_already_replaced(self):
        """If clear_calibration ran (or a new session started) while we were
        sleeping, _finish_calibration must not write to the old session or
        the new state."""
        old_session = {
            "phase": "noise_floor",
            "samples": deque([0.1]),
            "duration": 0.01,
            "status": "running",
            "stats": None,
        }
        new_session = {
            "phase": "music",
            "samples": deque([0.2]),
            "duration": 0.01,
            "status": "running",
            "stats": None,
        }
        core_engine.calibration = new_session
        asyncio.run(core_engine._finish_calibration(old_session))
        # The OLD session passed in is stale; nothing should have changed.
        self.assertEqual(old_session["status"], "running")
        self.assertIsNone(old_session["stats"])
        # The current session is untouched.
        self.assertIs(core_engine.calibration, new_session)
        self.assertEqual(new_session["status"], "running")
```

- [ ] **Step 2: Run the failing tests**

Run: `cd /home/ubuntu/SpinSense/SpinSense && python -m unittest core.tests.test_calibrate_engine -v`
Expected: FAIL on `_compute_stats` and `_finish_calibration` not existing.

- [ ] **Step 3: Add the stats helper**

Edit `core/core_engine.py`. Add below the `calibration` declaration:
```python
def _compute_stats(samples: list[float]) -> dict:
    """Reduce raw RMS samples into the stats blob returned to the wizard.
    Pure function; no engine state. Percentiles use linear interpolation on
    the sorted samples (matches numpy.percentile's default 'linear' method)."""
    if not samples:
        return {
            "samples_count": 0,
            "min": 0.0,
            "max": 0.0,
            "mean": 0.0,
            "p10": 0.0,
            "p50": 0.0,
            "p99": 0.0,
        }
    arr = sorted(samples)
    n = len(arr)

    def percentile(q: float) -> float:
        idx = (n - 1) * q
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return arr[lo] * (1 - frac) + arr[hi] * frac

    return {
        "samples_count": n,
        "min": arr[0],
        "max": arr[-1],
        "mean": sum(arr) / n,
        "p10": percentile(0.10),
        "p50": percentile(0.50),
        "p99": percentile(0.99),
    }
```

- [ ] **Step 4: Add the finish coroutine**

Below `_compute_stats`:
```python
async def _finish_calibration(session: dict) -> None:
    """Sleep through the session's capture window, then snapshot the samples
    into stats and flip status to 'done'. If the active calibration has been
    replaced (via clear_calibration or a new start_calibration) while we
    slept, this is a no-op — identity check guards against writing into a
    stale session."""
    await asyncio.sleep(session["duration"])
    if calibration is not session:
        return
    session["stats"] = _compute_stats(list(session["samples"]))
    session["status"] = "done"
```

- [ ] **Step 5: Re-run tests**

Run: `cd /home/ubuntu/SpinSense/SpinSense && python -m unittest core.tests.test_calibrate_engine -v`
Expected: All tests in CalibrationStateTest + ComputeStatsTest + FinishCalibrationTest PASS.

- [ ] **Step 6: Commit**

```bash
cd /home/ubuntu/SpinSense/SpinSense
git add core/core_engine.py core/tests/test_calibrate_engine.py
git commit -m "feat(engine): _compute_stats + _finish_calibration timer task"
```

---

## Task 5: Engine — command listener loop on /tmp/spinsense-cmd.sock

**Files:**
- Modify: `core/core_engine.py` — add `CMD_SOCKET_PATH`, `command_listener_loop`, command dispatch handlers; start the listener from `audio_monitor_loop`.
- Modify: `core/tests/test_calibrate_engine.py` — integration test with a real UDS connection on a tempfile path.

- [ ] **Step 1: Write the failing listener test**

Add to `core/tests/test_calibrate_engine.py`:
```python
import json
import tempfile


class CommandListenerTest(unittest.TestCase):
    """Integration test: spawn the listener on a tempfile socket, send each
    command type over a real UDS connection, verify responses + side effects."""

    def setUp(self):
        core_engine.calibration = None
        self.tmpdir = tempfile.mkdtemp()
        self.socket_path = os.path.join(self.tmpdir, "spinsense-cmd.sock")
        self._orig_path = core_engine.CMD_SOCKET_PATH
        core_engine.CMD_SOCKET_PATH = self.socket_path

    def tearDown(self):
        core_engine.CMD_SOCKET_PATH = self._orig_path
        core_engine.calibration = None
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)
        os.rmdir(self.tmpdir)

    async def _send(self, payload: dict) -> dict:
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
        writer.close()
        await writer.wait_closed()
        return json.loads(line.decode())

    async def _run_scenario(self):
        server_task = asyncio.create_task(core_engine.command_listener_loop())
        try:
            # Wait briefly for the listener to bind.
            for _ in range(50):
                if os.path.exists(self.socket_path):
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(os.path.exists(self.socket_path),
                            "listener did not bind socket in time")

            # start_calibration -> creates session, returns ok + duration
            reply = await self._send({"cmd": "start_calibration", "phase": "noise_floor"})
            self.assertTrue(reply["ok"])
            self.assertEqual(reply["duration_s"], 5.0)
            self.assertIsNotNone(core_engine.calibration)
            self.assertEqual(core_engine.calibration["phase"], "noise_floor")

            # second start while running -> rejected
            reply = await self._send({"cmd": "start_calibration", "phase": "music"})
            self.assertFalse(reply["ok"])
            self.assertIn("already", reply["detail"].lower())

            # get_calibration -> running
            reply = await self._send({"cmd": "get_calibration"})
            self.assertEqual(reply["status"], "running")
            self.assertEqual(reply["samples_count"], 0)
            self.assertIsNone(reply["stats"])

            # Inject a few samples and force completion by mutating the session.
            core_engine.calibration["samples"].append(0.001)
            core_engine.calibration["samples"].append(0.002)
            core_engine.calibration["stats"] = core_engine._compute_stats(
                list(core_engine.calibration["samples"])
            )
            core_engine.calibration["status"] = "done"

            reply = await self._send({"cmd": "get_calibration"})
            self.assertEqual(reply["status"], "done")
            self.assertEqual(reply["samples_count"], 2)
            self.assertIsNotNone(reply["stats"])

            # clear_calibration -> nulls the session
            reply = await self._send({"cmd": "clear_calibration"})
            self.assertTrue(reply["ok"])
            self.assertIsNone(core_engine.calibration)

            # get_calibration on cleared -> status "none"
            reply = await self._send({"cmd": "get_calibration"})
            self.assertEqual(reply["status"], "none")

            # unknown command -> ok: false
            reply = await self._send({"cmd": "no_such_cmd"})
            self.assertFalse(reply["ok"])
        finally:
            server_task.cancel()
            try:
                await server_task
            except (asyncio.CancelledError, Exception):
                pass

    def test_full_command_lifecycle(self):
        asyncio.run(self._run_scenario())
```

- [ ] **Step 2: Run the failing test**

Run: `cd /home/ubuntu/SpinSense/SpinSense && python -m unittest core.tests.test_calibrate_engine.CommandListenerTest -v`
Expected: FAIL with `AttributeError: module 'core_engine' has no attribute 'CMD_SOCKET_PATH'`.

- [ ] **Step 3: Add the constant + handlers + listener loop**

Edit `core/core_engine.py`. Add below the `calibration` declaration (and below `_finish_calibration`):

```python
CMD_SOCKET_PATH = '/tmp/spinsense-cmd.sock'


async def _handle_command(payload: dict) -> dict:
    """Dispatch one command. Pure-ish — only side effect is mutating the
    module-level `calibration` and scheduling the finish timer task."""
    global calibration
    cmd = payload.get("cmd")

    if cmd == "start_calibration":
        if calibration is not None and calibration["status"] == "running":
            return {"ok": False, "detail": "calibration already running"}
        phase = payload.get("phase")
        if phase not in ("noise_floor", "music"):
            return {"ok": False, "detail": f"invalid phase: {phase!r}"}
        session = {
            "phase": phase,
            "samples": deque(maxlen=500),
            "started_at": asyncio.get_event_loop().time(),
            "duration": 5.0,
            "status": "running",
            "stats": None,
        }
        calibration = session
        asyncio.create_task(_finish_calibration(session))
        return {"ok": True, "duration_s": 5.0}

    if cmd == "get_calibration":
        if calibration is None:
            return {"status": "none", "samples_count": 0, "stats": None}
        return {
            "status": calibration["status"],
            "samples_count": len(calibration["samples"]),
            "stats": calibration["stats"],
        }

    if cmd == "clear_calibration":
        calibration = None
        return {"ok": True}

    return {"ok": False, "detail": f"unknown cmd: {cmd!r}"}


async def _command_client_handler(reader, writer):
    """One JSON-line in, one JSON-line out. Connections are short-lived."""
    try:
        line = await reader.readline()
        if not line:
            return
        try:
            payload = json.loads(line.decode())
        except Exception as e:
            response = {"ok": False, "detail": f"json parse error: {e}"}
        else:
            try:
                response = await _handle_command(payload)
            except Exception as e:
                response = {"ok": False, "detail": f"handler error: {e}"}
        writer.write((json.dumps(response) + "\n").encode())
        await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def command_listener_loop():
    """Bind CMD_SOCKET_PATH and serve commands until cancelled. Removes a
    pre-existing socket file (matches the pattern used by the backend's
    /tmp/spinsense.sock listener)."""
    if os.path.exists(CMD_SOCKET_PATH):
        os.remove(CMD_SOCKET_PATH)
    server = await asyncio.start_unix_server(
        _command_client_handler, path=CMD_SOCKET_PATH,
    )
    print(f"🎛️ Command listener bound on {CMD_SOCKET_PATH}")
    async with server:
        await server.serve_forever()
```

Then start the listener from `audio_monitor_loop`. Find this block at the top of `audio_monitor_loop`:
```python
async def audio_monitor_loop():
    global _mqtt_task
    _mqtt_task = asyncio.create_task(connect_mqtt_loop())
    asyncio.create_task(config_watch_loop())
    print("--- VINYL SCROBBLER ALPHA ACTIVE ---")
```
Add the listener task alongside the others:
```python
async def audio_monitor_loop():
    global _mqtt_task
    _mqtt_task = asyncio.create_task(connect_mqtt_loop())
    asyncio.create_task(config_watch_loop())
    asyncio.create_task(command_listener_loop())
    print("--- VINYL SCROBBLER ALPHA ACTIVE ---")
```

- [ ] **Step 4: Re-run the listener test**

Run: `cd /home/ubuntu/SpinSense/SpinSense && python -m unittest core.tests.test_calibrate_engine.CommandListenerTest -v`
Expected: PASS

- [ ] **Step 5: Re-run all engine tests**

Run: `cd /home/ubuntu/SpinSense/SpinSense && python -m unittest core.tests.test_calibrate_engine -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
cd /home/ubuntu/SpinSense/SpinSense
git add core/core_engine.py core/tests/test_calibrate_engine.py
git commit -m "feat(engine): command_listener_loop + UDS protocol for calibrate"
```

---

## Task 6: Engine — detection suppression during active calibration

No new automated tests (the audio loop is a long-running coroutine with side effects against the real audio device — covered by the manual test plan instead).

**Files:**
- Modify: `core/core_engine.py` — wrap the detection branch in `audio_monitor_loop`.

- [ ] **Step 1: Locate the detection branch**

In `core/core_engine.py`, inside `audio_monitor_loop`, find this block:
```python
        if vol > runtime["threshold"]:
            if not state["in_song"] or state["silence_counter"] > 0:
                stream.stop()
                stream.close()
                await recognize_audio()
                stream = _open_input_stream(audio_callback)
                state["current_rms"] = 0.0
            else:
                print(".", end="", flush=True)
        else:
            if state["in_song"]:
                state["silence_counter"] += 1
                ...
```

- [ ] **Step 2: Wrap in calibration-aware guard**

Replace the block with:
```python
        # Suppress detection during an active calibration capture window.
        # The audio callback still appends samples + still updates the live
        # meter — only the recognize/silence-tracking logic is paused.
        if calibration is not None and calibration["status"] == "running":
            await asyncio.sleep(1)
            continue

        if vol > runtime["threshold"]:
            if not state["in_song"] or state["silence_counter"] > 0:
                stream.stop()
                stream.close()
                await recognize_audio()
                stream = _open_input_stream(audio_callback)
                state["current_rms"] = 0.0
            else:
                print(".", end="", flush=True)
        else:
            if state["in_song"]:
                state["silence_counter"] += 1
                print("s", end="", flush=True)
                if state["silence_counter"] >= runtime["stopped_silence"]:
                    print(f"\n[ STOPPED ] {runtime['stopped_silence']}s silence limit reached.")
                    publish_state("stopped")
                    state["in_song"] = False
                    state["last_song"] = ""
                    state["artist"] = ""
                    state["title"] = ""
                    state["album"] = ""
                    state["art_url"] = ""
                    state["silence_counter"] = 0
```
(The change is the new `if calibration is not None ...` guard at the top; everything else is unchanged.)

- [ ] **Step 3: Re-run the engine tests (sanity check)**

Run: `cd /home/ubuntu/SpinSense/SpinSense && python -m unittest core.tests.test_calibrate_engine -v`
Expected: All PASS (no regression).

- [ ] **Step 4: Commit**

```bash
cd /home/ubuntu/SpinSense/SpinSense
git add core/core_engine.py
git commit -m "feat(engine): suppress detection while calibration is running"
```

---

## Task 7: Backend — _send_cmd helper + /api/calibrate/* endpoints

**Files:**
- Modify: `gui/backend_main.py` — add `CMD_SOCKET_PATH`, `_send_cmd`, three new endpoints.
- Create: `gui/tests/test_calibrate_api.py` — endpoint tests with a fake listener fixture.

- [ ] **Step 1: Write the failing endpoint tests**

Create `gui/tests/test_calibrate_api.py`:
```python
"""Integration tests for /api/calibrate/{start,status,clear} against a
controllable fake UDS listener that stands in for core_engine."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import asynccontextmanager

from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import backend_main  # noqa: E402


class FakeEngine:
    """Tiny UDS listener that responds to one command at a time per a
    user-controlled scripted-responses queue. Runs in a background thread
    on its own event loop so we don't deadlock the TestClient's loop."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.responses: list[dict] = []
        self.received: list[dict] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.AbstractServer | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def queue(self, response: dict) -> None:
        self.responses.append(response)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3)

    def stop(self) -> None:
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._thread:
            self._thread.join(timeout=2)
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)

    def _run(self) -> None:
        async def main():
            self._server = await asyncio.start_unix_server(
                self._handle, path=self.socket_path,
            )
            self._ready.set()
            async with self._server:
                await self._server.serve_forever()

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(main())
        except asyncio.CancelledError:
            pass

    async def _handle(self, reader, writer):
        line = await reader.readline()
        try:
            payload = json.loads(line.decode())
        except Exception:
            payload = {"_parse_error": True}
        self.received.append(payload)
        response = self.responses.pop(0) if self.responses else {"ok": False, "detail": "no response queued"}
        writer.write((json.dumps(response) + "\n").encode())
        await writer.drain()
        writer.close()


class CalibrateApiTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.socket_path = os.path.join(self.tmpdir, "spinsense-cmd.sock")
        self._orig_path = backend_main.CMD_SOCKET_PATH
        backend_main.CMD_SOCKET_PATH = self.socket_path
        self.client = TestClient(backend_main.app)

    def tearDown(self):
        backend_main.CMD_SOCKET_PATH = self._orig_path
        self.client.close()
        if os.path.exists(self.tmpdir):
            try:
                os.rmdir(self.tmpdir)
            except OSError:
                pass

    def test_start_returns_engine_ack(self):
        fake = FakeEngine(self.socket_path)
        fake.queue({"ok": True, "duration_s": 5.0})
        fake.start()
        try:
            res = self.client.post("/api/calibrate/start", json={"phase": "noise_floor"})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json(), {"ok": True, "duration_s": 5.0})
            self.assertEqual(fake.received[0]["cmd"], "start_calibration")
            self.assertEqual(fake.received[0]["phase"], "noise_floor")
        finally:
            fake.stop()

    def test_start_rejects_invalid_phase(self):
        # No fake listener needed — validation happens before dispatch.
        res = self.client.post("/api/calibrate/start", json={"phase": "garbage"})
        self.assertEqual(res.status_code, 400)

    def test_start_503_when_engine_unreachable(self):
        # No fake listener running; socket doesn't exist.
        res = self.client.post("/api/calibrate/start", json={"phase": "noise_floor"})
        self.assertEqual(res.status_code, 503)
        self.assertIn("detail", res.json())

    def test_status_returns_engine_response(self):
        fake = FakeEngine(self.socket_path)
        fake.queue({"status": "done", "samples_count": 100, "stats": {"p10": 0.001}})
        fake.start()
        try:
            res = self.client.get("/api/calibrate/status")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertEqual(body["status"], "done")
            self.assertEqual(body["samples_count"], 100)
            self.assertEqual(body["stats"]["p10"], 0.001)
            self.assertEqual(fake.received[0]["cmd"], "get_calibration")
        finally:
            fake.stop()

    def test_clear_returns_engine_ack(self):
        fake = FakeEngine(self.socket_path)
        fake.queue({"ok": True})
        fake.start()
        try:
            res = self.client.post("/api/calibrate/clear")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json(), {"ok": True})
            self.assertEqual(fake.received[0]["cmd"], "clear_calibration")
        finally:
            fake.stop()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the failing tests**

Run: `cd /home/ubuntu/SpinSense/SpinSense/gui && python -m unittest tests.test_calibrate_api -v`
Expected: FAIL with `AttributeError: module 'backend_main' has no attribute 'CMD_SOCKET_PATH'` (or similar — none of the new endpoints exist yet).

- [ ] **Step 3: Add helper + constant**

Edit `gui/backend_main.py`. Below the `_SETUP_ALLOWED_PREFIXES = (...)` line, add:
```python
CMD_SOCKET_PATH = '/tmp/spinsense-cmd.sock'


async def _send_cmd(payload: dict, timeout: float = 2.0) -> dict:
    """Open a short-lived connection to the engine's command socket, write
    one JSON line, read one JSON line, close. Returns the parsed reply.

    Raises FileNotFoundError if the socket doesn't exist, ConnectionRefusedError
    if the engine isn't listening, asyncio.TimeoutError on either side."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(CMD_SOCKET_PATH),
        timeout=timeout,
    )
    try:
        writer.write((json.dumps(payload) + '\n').encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        return json.loads(line.decode())
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
```

Add the `import json` at the top of the file if it's not already there (the existing code doesn't appear to import json directly; check first and only add if missing).

- [ ] **Step 4: Add the three endpoints**

Below the existing `/api/mqtt/test` endpoint, add:
```python
@app.post("/api/calibrate/start")
async def calibrate_start(request: Request):
    body = await request.json()
    phase = body.get("phase")
    if phase not in ("noise_floor", "music"):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "detail": f"invalid phase: {phase!r}"},
        )
    try:
        reply = await _send_cmd({"cmd": "start_calibration", "phase": phase})
    except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError):
        return JSONResponse(
            status_code=503,
            content={"ok": False, "detail": "Engine not reachable"},
        )
    return reply


@app.get("/api/calibrate/status")
async def calibrate_status():
    try:
        reply = await _send_cmd({"cmd": "get_calibration"})
    except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError):
        return JSONResponse(
            status_code=503,
            content={"status": "none", "samples_count": 0, "stats": None, "detail": "Engine not reachable"},
        )
    return reply


@app.post("/api/calibrate/clear")
async def calibrate_clear():
    try:
        reply = await _send_cmd({"cmd": "clear_calibration"})
    except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError):
        return JSONResponse(
            status_code=503,
            content={"ok": False, "detail": "Engine not reachable"},
        )
    return reply
```

- [ ] **Step 5: Re-run the API tests**

Run: `cd /home/ubuntu/SpinSense/SpinSense/gui && python -m unittest tests.test_calibrate_api -v`
Expected: All five tests PASS.

- [ ] **Step 6: Commit**

```bash
cd /home/ubuntu/SpinSense/SpinSense
git add gui/backend_main.py gui/tests/test_calibrate_api.py
git commit -m "feat(api): /api/calibrate/{start,status,clear} endpoints"
```

---

## Task 8: Settings — dB display + linked number input

No automated tests (manual verification in browser per the spec's test plan).

**Files:**
- Modify: `gui/templates/settings.html` — slider attrs, add number input.
- Modify: `gui/static/settings.js` — dB ↔ linear conversion on load/save, link slider + number input, update live meter scale.

- [ ] **Step 1: Update the slider markup + add number input**

Edit `gui/templates/settings.html`. Replace this block (lines ~36-51):
```html
        <div>
          <div class="flex justify-between items-center mb-1">
            <label for="volume-threshold" class="text-body-sm text-on-surface">Volume threshold</label>
            <span id="volume-threshold-value" class="text-label-sm text-on-surface-variant tabular-nums">0.0000</span>
          </div>
          <input type="range" id="volume-threshold" name="Audio.Volume_Threshold"
                 min="0" max="0.05" step="0.0001" value="0.0062"
                 class="w-full accent-primary">
          <div class="relative h-2 mt-2 bg-surface-container-highest rounded-full overflow-hidden border border-outline-variant/30">
            <div id="rms-preview-bar" class="absolute inset-y-0 left-0 bg-primary/60 transition-[width] duration-200 ease-linear" style="width: 0%;"></div>
            <div id="rms-threshold-tick" class="absolute inset-y-0 w-0.5 bg-tertiary shadow-[0_0_4px_rgba(255,180,171,0.8)]" style="left: 12%;"></div>
          </div>
          <p class="text-label-sm text-on-surface-variant mt-1">
            Spin a silent record. Nudge the slider just above the peak of the bar.
          </p>
        </div>
```
With:
```html
        <div>
          <div class="flex justify-between items-center mb-1">
            <label for="volume-threshold" class="text-body-sm text-on-surface">Volume threshold</label>
            <input type="number" id="volume-threshold-number"
                   min="-80" max="0" step="0.5"
                   class="form-input w-24 text-right tabular-nums py-1">
          </div>
          <input type="range" id="volume-threshold"
                 min="-80" max="0" step="0.5" value="-40"
                 class="w-full accent-primary">
          <div class="relative h-2 mt-2 bg-surface-container-highest rounded-full overflow-hidden border border-outline-variant/30">
            <div id="rms-preview-bar" class="absolute inset-y-0 left-0 bg-primary/60 transition-[width] duration-200 ease-linear" style="width: 0%;"></div>
            <div id="rms-threshold-tick" class="absolute inset-y-0 w-0.5 bg-tertiary shadow-[0_0_4px_rgba(255,180,171,0.8)]" style="left: 50%;"></div>
          </div>
          <p class="text-label-sm text-on-surface-variant mt-1">
            Spin a silent record. Nudge the slider just above the peak of the bar.
          </p>
        </div>
```
Note: the slider's `name="Audio.Volume_Threshold"` attribute was removed — `settings.js` now handles the field manually because it needs to do dB↔linear conversion on save. The form's automatic `[name]` iteration would otherwise post the dB value to the engine.

- [ ] **Step 2: Update settings.js — replace RMS_CEILING constant block + add a hidden input handler**

Edit `gui/static/settings.js`. Replace the constant block at the top:
```js
  // Visual ceiling for the RMS bar + threshold tick. ~10x the default threshold
  // so the tick lands at a readable position and there's headroom for a loud
  // record to push the bar past it.
  const RMS_CEILING = 0.05;
```
With:
```js
  // dB display range. Threshold value posted to the backend is linear RMS;
  // the slider + number input both operate in dB and we convert on the way
  // in/out.
  const DB_MIN = -80;
  const DB_MAX = 0;
  const dbUtil = window.SpinSense.db;
  const THRESHOLD_NUMBER = document.getElementById("volume-threshold-number");
```

Replace `updateThresholdTick`:
```js
  function updateThresholdTick() {
    const db = Number(THRESHOLD_SLIDER.value);
    const pct = ((db - DB_MIN) / (DB_MAX - DB_MIN)) * 100;
    RMS_TICK.style.left = pct + "%";
    if (document.activeElement !== THRESHOLD_NUMBER) {
      THRESHOLD_NUMBER.value = db.toFixed(1);
    }
  }
```
(`THRESHOLD_VALUE` is gone — its DOM element no longer exists; the number input is now both display and input.)

Find the existing `THRESHOLD_VALUE` declaration near the top:
```js
  const THRESHOLD_VALUE = document.getElementById("volume-threshold-value");
```
Delete that line.

Replace `populateForm`:
```js
  function populateForm(config) {
    initialConfig = JSON.parse(JSON.stringify(config));
    FORM.querySelectorAll("[name]").forEach((el) => {
      const value = getNested(config, el.name);
      if (value === undefined || value === null) return;
      if (el === MIC_SELECT) return;
      el.value = value;
    });
    // Threshold is stored linear; display as dB.
    const storedRms = getNested(config, "Audio.Volume_Threshold");
    if (typeof storedRms === "number") {
      const db = dbUtil.rmsToDb(storedRms);
      THRESHOLD_SLIDER.value = db.toFixed(1);
      THRESHOLD_NUMBER.value = db.toFixed(1);
    }
    updateThresholdTick();
    setDirty(false);
    setToast("");
  }
```

Replace `readForm`:
```js
  function readForm() {
    const formObj = {};
    FORM.querySelectorAll("[name]").forEach((el) => {
      let value = el.value;
      if (el.type === "number" || el.type === "range") {
        value = value === "" ? 0 : Number(value);
      }
      setNested(formObj, el.name, value);
    });
    // The threshold inputs aren't part of FORM iteration (no name attribute).
    // Convert dB back to linear RMS and write it explicitly.
    const db = Number(THRESHOLD_SLIDER.value);
    setNested(formObj, "Audio.Volume_Threshold", dbUtil.dbToRms(db));
    return mergeDeep(JSON.parse(JSON.stringify(initialConfig)), formObj);
  }
```

Replace the live-meter subscribe block:
```js
  // Wire up RMS preview off the shell's WS pub/sub.
  if (window.SpinSense && typeof window.SpinSense.onFrame === "function") {
    window.SpinSense.onFrame((payload) => {
      const rms = payload && typeof payload.rms_level === "number" ? payload.rms_level : 0;
      const db = dbUtil.rmsToDb(rms);
      const pct = Math.max(0, Math.min(100, ((db - DB_MIN) / (DB_MAX - DB_MIN)) * 100));
      RMS_BAR.style.width = pct + "%";
    });
  }
```

Replace the slider input listener:
```js
  THRESHOLD_SLIDER.addEventListener("input", () => {
    updateThresholdTick();
    setDirty(true);
  });

  THRESHOLD_NUMBER.addEventListener("input", () => {
    const v = Math.max(DB_MIN, Math.min(DB_MAX, Number(THRESHOLD_NUMBER.value)));
    THRESHOLD_SLIDER.value = v;
    updateThresholdTick();
    setDirty(true);
  });
```

The existing form-level `input` listener already filters out `THRESHOLD_SLIDER`. Add `THRESHOLD_NUMBER` to the filter:
```js
  FORM.addEventListener("input", (ev) => {
    if (ev.target === THRESHOLD_SLIDER) return;
    if (ev.target === THRESHOLD_NUMBER) return;
    setDirty(true);
  });
```

- [ ] **Step 3: Manual smoke test**

This is a UI change — no automated test. The Acceptance Criteria section at the bottom of this plan lists the manual checks to run on a live container.

- [ ] **Step 4: Commit**

```bash
cd /home/ubuntu/SpinSense/SpinSense
git add gui/templates/settings.html gui/static/settings.js
git commit -m "feat(settings): show volume threshold in dB with linked number input"
```

---

## Task 9: Dashboard — RMS meter in dB, threshold tick

No automated tests (UI; manual verification).

**Files:**
- Modify: `gui/static/dashboard.js` — switch input meter scale to dB, use the shared `db_utils`, add a threshold marker.
- Modify: `gui/templates/dashboard.html` — add a threshold tick element overlaid on the input meter bar.

- [ ] **Step 1: Add the threshold tick to the input meter**

Edit `gui/templates/dashboard.html`. Find the input meter block (around lines 35-44):
```html
        <div>
          <div class="flex items-center justify-between mb-1">
            <span class="text-label-sm text-outline tracking-widest uppercase">Input</span>
            <span id="input-meter-text" class="text-label-sm text-outline tabular-nums">0.000</span>
          </div>
          <div class="h-2 bg-surface-container-highest rounded-full overflow-hidden border border-outline-variant/30">
            <div id="input-meter" class="h-full bg-primary/80 rounded-r-full shadow-[0_0_10px_rgba(221,183,255,0.35)] transition-[width] duration-200 linear"
                 style="width: 0%;"></div>
          </div>
        </div>
```
Replace with:
```html
        <div>
          <div class="flex items-center justify-between mb-1">
            <span class="text-label-sm text-outline tracking-widest uppercase">Input</span>
            <span id="input-meter-text" class="text-label-sm text-outline tabular-nums">&minus;&infin; dB</span>
          </div>
          <div class="relative h-2 bg-surface-container-highest rounded-full overflow-hidden border border-outline-variant/30">
            <div id="input-meter" class="h-full bg-primary/80 rounded-r-full shadow-[0_0_10px_rgba(221,183,255,0.35)] transition-[width] duration-200 linear"
                 style="width: 0%;"></div>
            <div id="input-meter-threshold" class="absolute inset-y-0 w-0.5 bg-tertiary shadow-[0_0_4px_rgba(255,180,171,0.8)]" style="left: 50%;"></div>
          </div>
        </div>
```
(Two changes: outer wrapper got `relative`, added the threshold tick div, and the meter text now defaults to `-∞ dB` instead of `0.000`.)

- [ ] **Step 2: Update dashboard.js — switch to dB scale**

Edit `gui/static/dashboard.js`. Replace this block:
```js
  let volumeThreshold = 0.05;  // overwritten by /api/config on load
  let lastSeenTitle   = "";
```
With:
```js
  // dB display window for the input meter on this page. Threshold tick is
  // computed from Audio.Volume_Threshold (linear RMS in config -> dB here).
  const DB_MIN = -80;
  const DB_MAX = 0;
  const dbUtil = window.SpinSense.db;
  let volumeThresholdDb = -40;  // overwritten by /api/config on load
  let lastSeenTitle   = "";
```

Delete the local `rmsToDb` function (it duplicates the shared helper):
```js
  function rmsToDb(rms) {
    if (!rms || rms <= 0) return null; // -infinity
    const db = 20 * Math.log10(rms);
    return Math.max(-60, Math.min(0, db));
  }
```
Replace with nothing (deletion).

Add a function to place the threshold tick:
```js
  const meterThreshold = $("input-meter-threshold");

  function placeThresholdTick() {
    if (!meterThreshold) return;
    const pct = ((volumeThresholdDb - DB_MIN) / (DB_MAX - DB_MIN)) * 100;
    meterThreshold.style.left = `${Math.max(0, Math.min(100, pct))}%`;
  }
```

Inside `handleFrame`, replace the RMS input meter block:
```js
    // RMS input meter (against configured threshold)
    const rms = typeof payload.rms_level === "number" ? payload.rms_level : 0;
    if (meterBar && meterText) {
      const pct = Math.max(0, Math.min(100, (rms / volumeThreshold) * 100));
      meterBar.style.width = `${pct}%`;
      meterText.textContent = rms.toFixed(4);
    }
```
With:
```js
    // Input meter in dB, with a tick at the configured threshold.
    const rms = typeof payload.rms_level === "number" ? payload.rms_level : 0;
    if (meterBar && meterText) {
      const db = dbUtil.rmsToDb(rms);
      const pct = Math.max(0, Math.min(100, ((db - DB_MIN) / (DB_MAX - DB_MIN)) * 100));
      meterBar.style.width = `${pct}%`;
      meterText.textContent = rms <= 0 ? "−∞ dB" : `${db.toFixed(1)} dB`;
    }
```

Replace the System Health "Input Level" block (which still calls the deleted local `rmsToDb`):
```js
    // System Health: Input Level (dB)
    const db = rmsToDb(rms);
    if (levelBar && levelText) {
      if (db === null) {
        levelBar.style.width = "0%";
        levelText.innerHTML  = "&minus;&infin; dB";
      } else {
        levelBar.style.width = `${((db + 60) / 60) * 100}%`;
        levelText.textContent = `${Math.round(db)} dB`;
      }
    }
```
With:
```js
    // System Health: Input Level (dB) — narrower visual window (-60..0)
    // because that's where useful signal lives on this widget.
    if (levelBar && levelText) {
      if (rms <= 0) {
        levelBar.style.width = "0%";
        levelText.innerHTML  = "&minus;&infin; dB";
      } else {
        const db = dbUtil.rmsToDb(rms);
        const clamped = Math.max(-60, Math.min(0, db));
        levelBar.style.width = `${((clamped + 60) / 60) * 100}%`;
        levelText.textContent = `${Math.round(clamped)} dB`;
      }
    }
```

Update `loadConfig`:
```js
  async function loadConfig() {
    try {
      const res = await fetch("/api/config");
      if (!res.ok) return;
      const cfg = await res.json();
      const v = cfg && cfg.Audio && cfg.Audio.Volume_Threshold;
      if (typeof v === "number" && v > 0) {
        volumeThresholdDb = dbUtil.rmsToDb(v);
        placeThresholdTick();
      }
    } catch (_) { /* fallback default already set */ }
  }
```

- [ ] **Step 3: Manual smoke test**

UI change — no automated test. Verify post-implementation on a live container per the Acceptance Criteria.

- [ ] **Step 4: Commit**

```bash
cd /home/ubuntu/SpinSense/SpinSense
git add gui/templates/dashboard.html gui/static/dashboard.js
git commit -m "feat(dashboard): input meter in dB with configured threshold tick"
```

---

## Task 10: Wizard — Step 2 markup restructure + sub-step navigation

This commit produces visible-but-incomplete UI: the new sub-step layout works (click between substeps), but capture-start buttons don't do anything yet. Captures land in Task 11.

**Files:**
- Modify: `gui/templates/setup.html` — replace the Step 2 `<section>` with the five sub-screens.

- [ ] **Step 1: Replace the Step 2 section in setup.html**

Find this block in `gui/templates/setup.html` (lines ~68-101):
```html
    <!-- Step 2: Threshold calibration -->
    <section class="wizard-step hidden" data-step="2">
      <span class="material-symbols-outlined text-primary block mb-sm" style="font-size: 36px;">graphic_eq</span>
      <h2 class="font-headline text-headline-md text-on-background">Calibrate your threshold</h2>
      ...
    </section>
```
(the entire `<section data-step="2">…</section>` block).

Replace with:
```html
    <!-- Step 2: Threshold calibration (5 sub-screens, in-place swap) -->
    <section class="wizard-step hidden" data-step="2">
      <span class="material-symbols-outlined text-primary block mb-sm" style="font-size: 36px;">graphic_eq</span>
      <h2 class="font-headline text-headline-md text-on-background">Calibrate your threshold</h2>

      <!-- 2A: chooser -->
      <div class="wizard-substep" data-substep="choose">
        <p class="text-body-sm text-on-surface-variant mt-sm">
          Two ways to get the right threshold:
        </p>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-md mt-md">
          <button type="button" id="calibrate-auto-btn"
                  class="bg-primary text-on-primary font-medium px-lg py-md rounded-xl hover:opacity-90 transition-opacity text-left">
            <div class="flex items-center gap-xs mb-1">
              <span class="material-symbols-outlined">auto_fix_high</span>
              <span class="text-label-md font-semibold">Auto-calibrate</span>
            </div>
            <span class="text-body-sm opacity-90">Recommended. Capture silence and a song; we compute the threshold for you.</span>
          </button>
          <button type="button" id="calibrate-manual-btn"
                  class="bg-surface-container-high border border-outline-variant/40 text-on-surface font-medium px-lg py-md rounded-xl hover:bg-surface-container-highest transition-colors text-left">
            <div class="flex items-center gap-xs mb-1">
              <span class="material-symbols-outlined">tune</span>
              <span class="text-label-md font-semibold">Set manually</span>
            </div>
            <span class="text-body-sm opacity-90">Drag a slider while watching the live meter.</span>
          </button>
        </div>
        <p id="calibrate-auto-warning" class="hidden text-label-sm text-error mt-sm" aria-live="polite"></p>
        <div class="flex justify-between items-center mt-lg">
          <button type="button" data-wizard-back
                  class="text-label-md text-on-surface-variant hover:text-on-surface transition-colors">Back</button>
        </div>
      </div>

      <!-- 2B: noise floor capture -->
      <div class="wizard-substep hidden" data-substep="noise_capture">
        <p class="text-body-md text-on-surface mt-sm font-medium">Step 1 of 2: Capture the noise floor</p>
        <p class="text-body-sm text-on-surface-variant mt-1">
          Drop the needle on the runout groove — the silent section at the end or beginning of a side. Don't play a song yet. If your room is quiet, you can usually hear the music at the needle itself without speakers, so this is also how you know you're in a quiet section.
        </p>
        <div class="mt-md text-center">
          <button type="button" id="calibrate-noise-start"
                  class="bg-primary text-on-primary font-medium px-xl py-md rounded-full hover:opacity-90 transition-opacity">
            Start 5s capture
          </button>
          <p id="calibrate-noise-status" class="text-label-md text-on-surface-variant mt-sm" aria-live="polite"></p>
        </div>
        <div class="mt-md">
          <p class="text-label-sm text-on-surface-variant mb-1">Live level</p>
          <div class="relative h-3 bg-surface-container-highest rounded-full overflow-hidden border border-outline-variant/30">
            <div id="calibrate-noise-bar" class="absolute inset-y-0 left-0 bg-primary/60 transition-[width] duration-200 ease-linear" style="width: 0%;"></div>
          </div>
        </div>
        <div class="flex justify-between items-center mt-lg">
          <button type="button" data-substep-back="choose"
                  class="text-label-md text-on-surface-variant hover:text-on-surface transition-colors">Back</button>
        </div>
      </div>

      <!-- 2C: music capture -->
      <div class="wizard-substep hidden" data-substep="music_capture">
        <p class="text-body-md text-on-surface mt-sm font-medium">Step 2 of 2: Capture a song</p>
        <p class="text-body-sm text-on-surface-variant mt-1">
          Now move the needle to a song. When you hear it start playing, click the button — we'll listen for 5 seconds.
        </p>
        <div class="mt-md text-center">
          <button type="button" id="calibrate-music-start"
                  class="bg-primary text-on-primary font-medium px-xl py-md rounded-full hover:opacity-90 transition-opacity">
            Music started — capture now
          </button>
          <p id="calibrate-music-status" class="text-label-md text-on-surface-variant mt-sm" aria-live="polite"></p>
        </div>
        <div class="mt-md">
          <p class="text-label-sm text-on-surface-variant mb-1">Live level</p>
          <div class="relative h-3 bg-surface-container-highest rounded-full overflow-hidden border border-outline-variant/30">
            <div id="calibrate-music-bar" class="absolute inset-y-0 left-0 bg-primary/60 transition-[width] duration-200 ease-linear" style="width: 0%;"></div>
          </div>
        </div>
        <div class="flex justify-between items-center mt-lg">
          <button type="button" data-substep-back="noise_capture"
                  class="text-label-md text-on-surface-variant hover:text-on-surface transition-colors">Back</button>
        </div>
      </div>

      <!-- 2D: result -->
      <div class="wizard-substep hidden" data-substep="result">
        <p class="text-body-md text-on-surface mt-sm">
          <span class="text-primary">✨</span> Your optimum threshold is
          <span id="calibrate-result-headline" class="font-headline text-headline-md text-on-background">—</span>
        </p>
        <dl class="grid grid-cols-3 gap-md mt-md text-center">
          <div>
            <dt class="text-label-sm text-on-surface-variant">Silence (noise floor)</dt>
            <dd id="calibrate-result-noise" class="text-body-md text-on-surface tabular-nums mt-1">—</dd>
          </div>
          <div>
            <dt class="text-label-sm text-on-surface-variant">Music (quiet parts)</dt>
            <dd id="calibrate-result-music" class="text-body-md text-on-surface tabular-nums mt-1">—</dd>
          </div>
          <div>
            <dt class="text-label-sm text-on-surface-variant">Threshold</dt>
            <dd id="calibrate-result-threshold" class="text-body-md text-primary font-semibold tabular-nums mt-1">—</dd>
          </div>
        </dl>
        <div class="mt-md">
          <div class="flex justify-between items-center mb-1">
            <label for="wizard-threshold" class="text-body-sm text-on-surface">Volume threshold</label>
            <input type="number" id="wizard-threshold-number"
                   min="-80" max="0" step="0.5"
                   class="form-input w-24 text-right tabular-nums py-1">
          </div>
          <input type="range" id="wizard-threshold"
                 min="-80" max="0" step="0.5" value="-40"
                 class="w-full accent-primary">
          <div class="relative h-3 mt-2 bg-surface-container-highest rounded-full overflow-hidden border border-outline-variant/30">
            <div id="wizard-rms-bar" class="absolute inset-y-0 left-0 bg-primary/60 transition-[width] duration-200 ease-linear" style="width: 0%;"></div>
            <div id="wizard-rms-tick" class="absolute inset-y-0 w-0.5 bg-tertiary shadow-[0_0_4px_rgba(255,180,171,0.8)]" style="left: 50%;"></div>
          </div>
        </div>
        <div class="flex flex-wrap gap-md mt-md text-label-md">
          <button type="button" id="calibrate-rerun"
                  class="text-on-surface-variant hover:text-on-surface transition-colors">Re-run calibration</button>
          <button type="button" data-substep-goto="manual"
                  class="text-on-surface-variant hover:text-on-surface transition-colors">Set manually instead</button>
        </div>
        <div class="flex justify-between items-center mt-lg">
          <button type="button" data-substep-back="choose"
                  class="text-label-md text-on-surface-variant hover:text-on-surface transition-colors">Back</button>
          <button type="button" data-wizard-next
                  class="bg-primary text-on-primary font-medium px-lg py-2 rounded-full hover:opacity-90 transition-opacity">
            Looks good — continue
          </button>
        </div>
      </div>

      <!-- 2E: manual -->
      <div class="wizard-substep hidden" data-substep="manual">
        <p class="text-body-sm text-on-surface-variant mt-sm">
          Drop the needle on a record but don't play music yet. Watch where the bar peaks — that's your noise floor. Nudge the slider just above it.
        </p>
        <div class="mt-md">
          <div class="flex justify-between items-center mb-1">
            <label for="wizard-threshold-manual" class="text-body-sm text-on-surface">Volume threshold</label>
            <input type="number" id="wizard-threshold-manual-number"
                   min="-80" max="0" step="0.5"
                   class="form-input w-24 text-right tabular-nums py-1">
          </div>
          <input type="range" id="wizard-threshold-manual"
                 min="-80" max="0" step="0.5" value="-40"
                 class="w-full accent-primary">
          <div class="relative h-3 mt-2 bg-surface-container-highest rounded-full overflow-hidden border border-outline-variant/30">
            <div id="wizard-rms-bar-manual" class="absolute inset-y-0 left-0 bg-primary/60 transition-[width] duration-200 ease-linear" style="width: 0%;"></div>
            <div id="wizard-rms-tick-manual" class="absolute inset-y-0 w-0.5 bg-tertiary shadow-[0_0_4px_rgba(255,180,171,0.8)]" style="left: 50%;"></div>
          </div>
          <p class="text-label-sm text-on-surface-variant mt-1">
            If the bar isn't moving, the engine isn't getting any audio yet — check your mic selection.
          </p>
        </div>
        <div class="flex justify-between items-center mt-lg">
          <button type="button" data-substep-back="choose"
                  class="text-label-md text-on-surface-variant hover:text-on-surface transition-colors">Back</button>
          <button type="button" data-wizard-next
                  class="bg-primary text-on-primary font-medium px-lg py-2 rounded-full hover:opacity-90 transition-opacity">
            Continue
          </button>
        </div>
      </div>
    </section>
```

- [ ] **Step 2: Add a temporary substep navigation handler in setup.js**

Edit `gui/static/setup.js`. At the bottom, just above `showStep(0); loadConfig();` add a temporary substep router so the markup is interactively navigable for manual inspection:
```js
  // Step 2 substep router. Full capture orchestration lands in the next task.
  function showSubstep(name) {
    document.querySelectorAll(".wizard-substep").forEach((el) => {
      el.classList.toggle("hidden", el.dataset.substep !== name);
    });
  }
  document.querySelectorAll("[data-substep-back]").forEach((b) => {
    b.addEventListener("click", () => showSubstep(b.dataset.substepBack));
  });
  document.querySelectorAll("[data-substep-goto]").forEach((b) => {
    b.addEventListener("click", () => showSubstep(b.dataset.substepGoto));
  });
  document.getElementById("calibrate-auto-btn").addEventListener("click", () => {
    showSubstep("noise_capture");
  });
  document.getElementById("calibrate-manual-btn").addEventListener("click", () => {
    showSubstep("manual");
  });
  showSubstep("choose");
```

The existing `THRESHOLD` references in setup.js will become broken (the old `wizard-threshold` slider attributes changed and the value box is gone). That's expected — Task 11 rewrites this. For now, comment out the broken bits:

Find:
```js
  const THRESHOLD = document.getElementById("wizard-threshold");
  const THRESHOLD_VALUE = document.getElementById("wizard-threshold-value");
  const RMS_BAR = document.getElementById("wizard-rms-bar");
  const RMS_TICK = document.getElementById("wizard-rms-tick");
```
Comment out (don't delete — the next task replaces them):
```js
  // const THRESHOLD = document.getElementById("wizard-threshold");
  // const THRESHOLD_VALUE = document.getElementById("wizard-threshold-value");
  // const RMS_BAR = document.getElementById("wizard-rms-bar");
  // const RMS_TICK = document.getElementById("wizard-rms-tick");
```

Find and comment out the body of `updateThresholdTick`:
```js
  function updateThresholdTick() {
    // const t = Number(THRESHOLD.value);
    // const pct = Math.min(100, (t / RMS_CEILING) * 100);
    // RMS_TICK.style.left = pct + "%";
    // THRESHOLD_VALUE.textContent = t.toFixed(4);
  }
```

Comment out the THRESHOLD-related lines in `loadConfig`:
```js
      // THRESHOLD.value = getNested(initialConfig, "Audio.Volume_Threshold") ?? 0.0062;
```

Comment out the `THRESHOLD` line in `buildPayload`:
```js
    // setNested(payload, "Audio.Volume_Threshold", Number(THRESHOLD.value));
```

Comment out the slider input listener:
```js
  // THRESHOLD.addEventListener("input", updateThresholdTick);
```

Comment out the live-RMS subscribe block at the bottom that references `RMS_BAR`:
```js
  // if (window.SpinSense && typeof window.SpinSense.onFrame === "function") {
  //   window.SpinSense.onFrame((payload) => {
  //     const rms = payload && typeof payload.rms_level === "number" ? payload.rms_level : 0;
  //     const pct = Math.min(100, Math.max(0, (rms / RMS_CEILING) * 100));
  //     RMS_BAR.style.width = pct + "%";
  //   });
  // }
```

Also delete the `RMS_CEILING` constant or leave it; the next task removes it.

- [ ] **Step 3: Manual smoke test**

Open `/setup` in a browser. Step through to Step 2. Verify:
- Two big buttons render.
- Clicking "Auto-calibrate" shows the noise capture sub-screen.
- "Back" from there returns to chooser.
- Clicking "Set manually" shows the manual sub-screen with the new dB slider.

No capture functionality yet — buttons in 2B/2C don't do anything. That's expected.

- [ ] **Step 4: Commit**

```bash
cd /home/ubuntu/SpinSense/SpinSense
git add gui/templates/setup.html gui/static/setup.js
git commit -m "feat(wizard): Step 2 sub-screens markup + substep navigation"
```

---

## Task 11: Wizard — capture orchestration, threshold formula, clear-on-cancel

This task replaces the commented-out blocks from Task 10 with real implementations and wires up the capture/result/save flow.

**Files:**
- Modify: `gui/static/setup.js` — full Step 2 logic.

- [ ] **Step 1: Replace the top-of-file declarations**

Edit `gui/static/setup.js`. Find the commented-out constants block and the `RMS_CEILING` constant. Replace the entire region between the existing `MQTT_TEST = ...`, `POPUP = ...`, and `FINISH_BTN = ...` declarations and the `RMS_CEILING` constant (i.e., the constants section near the top of the IIFE) with this:
```js
  const MIC = document.getElementById("wizard-mic");

  // Step 2 — auto path elements
  const AUTO_BTN = document.getElementById("calibrate-auto-btn");
  const MANUAL_BTN = document.getElementById("calibrate-manual-btn");
  const AUTO_WARNING = document.getElementById("calibrate-auto-warning");

  const NOISE_START = document.getElementById("calibrate-noise-start");
  const NOISE_STATUS = document.getElementById("calibrate-noise-status");
  const NOISE_BAR = document.getElementById("calibrate-noise-bar");

  const MUSIC_START = document.getElementById("calibrate-music-start");
  const MUSIC_STATUS = document.getElementById("calibrate-music-status");
  const MUSIC_BAR = document.getElementById("calibrate-music-bar");

  const RESULT_HEADLINE = document.getElementById("calibrate-result-headline");
  const RESULT_NOISE = document.getElementById("calibrate-result-noise");
  const RESULT_MUSIC = document.getElementById("calibrate-result-music");
  const RESULT_THRESHOLD = document.getElementById("calibrate-result-threshold");
  const RERUN_BTN = document.getElementById("calibrate-rerun");

  // Step 2 — result-screen slider (auto path) and manual-screen slider.
  // Both operate in dB; the engine value in config is linear RMS.
  const THRESHOLD = document.getElementById("wizard-threshold");
  const THRESHOLD_NUMBER = document.getElementById("wizard-threshold-number");
  const RMS_BAR = document.getElementById("wizard-rms-bar");
  const RMS_TICK = document.getElementById("wizard-rms-tick");

  const THRESHOLD_MANUAL = document.getElementById("wizard-threshold-manual");
  const THRESHOLD_MANUAL_NUMBER = document.getElementById("wizard-threshold-manual-number");
  const RMS_BAR_MANUAL = document.getElementById("wizard-rms-bar-manual");
  const RMS_TICK_MANUAL = document.getElementById("wizard-rms-tick-manual");
```

(Keep the existing MQTT_HOST/MQTT_PORT/etc. and POPUP_*/FINISH_BTN declarations as they are.)

Replace `RMS_CEILING` with:
```js
  const DB_MIN = -80;
  const DB_MAX = 0;
  const dbUtil = window.SpinSense.db;
  // Which slider holds the canonical threshold value for save. "result" = auto
  // path slider on Screen 2D; "manual" = Screen 2E slider.
  let activeSlider = "result";
  let captures = { noise: null, music: null };
  let currentSubstep = "choose";
  let captureAbortKey = 0; // bumped on cancel to invalidate in-flight polls
```

- [ ] **Step 2: Replace helpers + add capture orchestration**

Replace the (now-commented) `updateThresholdTick` with a function that handles both screens:
```js
  function placeTick(tickEl, db) {
    if (!tickEl) return;
    const pct = ((db - DB_MIN) / (DB_MAX - DB_MIN)) * 100;
    tickEl.style.left = Math.max(0, Math.min(100, pct)) + "%";
  }

  function syncThresholdControls(which, db) {
    const slider = which === "manual" ? THRESHOLD_MANUAL : THRESHOLD;
    const number = which === "manual" ? THRESHOLD_MANUAL_NUMBER : THRESHOLD_NUMBER;
    const tick = which === "manual" ? RMS_TICK_MANUAL : RMS_TICK;
    const clamped = Math.max(DB_MIN, Math.min(DB_MAX, db));
    slider.value = clamped.toFixed(1);
    if (document.activeElement !== number) {
      number.value = clamped.toFixed(1);
    }
    placeTick(tick, clamped);
  }
```

Replace the (commented-out) live-RMS subscribe block at the bottom with one that updates whichever bar is currently visible:
```js
  if (window.SpinSense && typeof window.SpinSense.onFrame === "function") {
    window.SpinSense.onFrame((payload) => {
      const rms = payload && typeof payload.rms_level === "number" ? payload.rms_level : 0;
      const db = dbUtil.rmsToDb(rms);
      const pct = Math.max(0, Math.min(100, ((db - DB_MIN) / (DB_MAX - DB_MIN)) * 100));
      const widthStr = pct + "%";
      if (NOISE_BAR) NOISE_BAR.style.width = widthStr;
      if (MUSIC_BAR) MUSIC_BAR.style.width = widthStr;
      if (RMS_BAR) RMS_BAR.style.width = widthStr;
      if (RMS_BAR_MANUAL) RMS_BAR_MANUAL.style.width = widthStr;
    });
  }
```

Replace `loadConfig`'s threshold-handling line. Find:
```js
      // THRESHOLD.value = getNested(initialConfig, "Audio.Volume_Threshold") ?? 0.0062;
```
Replace with:
```js
      const storedRms = getNested(initialConfig, "Audio.Volume_Threshold") ?? 0.01;
      const storedDb = dbUtil.rmsToDb(storedRms);
      syncThresholdControls("result", storedDb);
      syncThresholdControls("manual", storedDb);
```

Replace `buildPayload`'s threshold line. Find:
```js
    // setNested(payload, "Audio.Volume_Threshold", Number(THRESHOLD.value));
```
Replace with:
```js
    const sliderDb = Number(
      activeSlider === "manual" ? THRESHOLD_MANUAL.value : THRESHOLD.value
    );
    setNested(payload, "Audio.Volume_Threshold", dbUtil.dbToRms(sliderDb));
```

- [ ] **Step 3: Add the substep navigation + capture flow**

Replace the temporary substep router added in Task 10 (between `// Step 2 substep router...` and `showSubstep("choose");`) with the full implementation:

```js
  function showSubstep(name) {
    currentSubstep = name;
    document.querySelectorAll(".wizard-substep").forEach((el) => {
      el.classList.toggle("hidden", el.dataset.substep !== name);
    });
    if (name === "manual") activeSlider = "manual";
    if (name === "result") activeSlider = "result";
  }

  async function checkEngineReachable() {
    try {
      const res = await fetch("/api/calibrate/status");
      if (res.status === 503) return false;
      return res.ok;
    } catch (_) {
      return false;
    }
  }

  async function clearCalibrationBestEffort() {
    try { await fetch("/api/calibrate/clear", { method: "POST" }); } catch (_) {}
  }

  function applyThresholdFormula(noiseStats, musicStats) {
    // Threshold = noise_p99 + 0.25 * (music_p10 - noise_p99), all in dB.
    // Safety: 2 dB minimum gap above noise; clamp to display range.
    const noiseDb = dbUtil.rmsToDb(noiseStats.p99);
    const musicDb = dbUtil.rmsToDb(musicStats.p10);
    if (noiseDb >= musicDb) {
      return { ok: false, noiseDb, musicDb };
    }
    let thresholdDb = noiseDb + 0.25 * (musicDb - noiseDb);
    if (thresholdDb < noiseDb + 2) thresholdDb = noiseDb + 2;
    thresholdDb = Math.max(DB_MIN, Math.min(DB_MAX, thresholdDb));
    return { ok: true, noiseDb, musicDb, thresholdDb };
  }

  async function runCapture(phase, statusEl, startBtn) {
    const myKey = ++captureAbortKey;
    startBtn.disabled = true;
    statusEl.textContent = "Starting…";

    let startReply;
    try {
      const res = await fetch("/api/calibrate/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phase }),
      });
      startReply = await res.json().catch(() => ({}));
      if (!res.ok || !startReply.ok) {
        statusEl.textContent = (startReply && startReply.detail) || `Start failed (${res.status})`;
        startBtn.disabled = false;
        return null;
      }
    } catch (e) {
      statusEl.textContent = "Network error: " + e.message;
      startBtn.disabled = false;
      return null;
    }

    const duration = (startReply.duration_s || 5) * 1000;
    const startedAt = Date.now();
    while (Date.now() - startedAt < duration) {
      if (captureAbortKey !== myKey) return null;
      const remaining = Math.ceil((duration - (Date.now() - startedAt)) / 1000);
      statusEl.textContent = `Capturing… ${remaining}s`;
      await new Promise((r) => setTimeout(r, 250));
    }

    // Poll status up to 3s extra slack for the engine's timer task to finish.
    statusEl.textContent = "Finishing…";
    const deadline = Date.now() + 3000;
    while (Date.now() < deadline) {
      if (captureAbortKey !== myKey) return null;
      try {
        const res = await fetch("/api/calibrate/status");
        const body = await res.json().catch(() => ({}));
        if (body.status === "done" && body.stats) {
          startBtn.disabled = false;
          statusEl.textContent = `Captured ${body.samples_count} samples.`;
          return body.stats;
        }
      } catch (_) {}
      await new Promise((r) => setTimeout(r, 250));
    }
    statusEl.textContent = "Capture timed out — try again.";
    startBtn.disabled = false;
    return null;
  }

  async function runNoiseCapture() {
    const stats = await runCapture("noise_floor", NOISE_STATUS, NOISE_START);
    if (!stats) return;
    captures.noise = stats;
    showSubstep("music_capture");
  }

  async function runMusicCapture() {
    const stats = await runCapture("music", MUSIC_STATUS, MUSIC_START);
    if (!stats) return;
    captures.music = stats;

    const result = applyThresholdFormula(captures.noise, captures.music);
    if (!result.ok) {
      MUSIC_STATUS.textContent = "Your silence sample was as loud as your music sample. Try again — make sure a song is actually playing.";
      return; // user can click the capture button again to retry music phase only
    }

    RESULT_HEADLINE.textContent = dbUtil.formatDb(result.thresholdDb);
    RESULT_NOISE.textContent = dbUtil.formatDb(result.noiseDb);
    RESULT_MUSIC.textContent = dbUtil.formatDb(result.musicDb);
    RESULT_THRESHOLD.textContent = dbUtil.formatDb(result.thresholdDb);
    syncThresholdControls("result", result.thresholdDb);
    showSubstep("result");
    clearCalibrationBestEffort();
  }

  AUTO_BTN.addEventListener("click", async () => {
    AUTO_WARNING.classList.add("hidden");
    const reachable = await checkEngineReachable();
    if (!reachable) {
      AUTO_WARNING.textContent = "Audio engine not running — restart the container or use 'Set manually'.";
      AUTO_WARNING.classList.remove("hidden");
      return;
    }
    captures = { noise: null, music: null };
    showSubstep("noise_capture");
  });

  MANUAL_BTN.addEventListener("click", () => {
    activeSlider = "manual";
    showSubstep("manual");
  });

  NOISE_START.addEventListener("click", runNoiseCapture);
  MUSIC_START.addEventListener("click", runMusicCapture);

  RERUN_BTN.addEventListener("click", () => {
    captures = { noise: null, music: null };
    showSubstep("noise_capture");
  });

  document.querySelectorAll("[data-substep-back]").forEach((b) => {
    b.addEventListener("click", () => {
      captureAbortKey++;
      clearCalibrationBestEffort();
      showSubstep(b.dataset.substepBack);
    });
  });
  document.querySelectorAll("[data-substep-goto]").forEach((b) => {
    b.addEventListener("click", () => showSubstep(b.dataset.substepGoto));
  });

  // Threshold control wiring for both sliders.
  THRESHOLD.addEventListener("input", () => {
    syncThresholdControls("result", Number(THRESHOLD.value));
  });
  THRESHOLD_NUMBER.addEventListener("input", () => {
    syncThresholdControls("result", Number(THRESHOLD_NUMBER.value));
  });
  THRESHOLD_MANUAL.addEventListener("input", () => {
    syncThresholdControls("manual", Number(THRESHOLD_MANUAL.value));
  });
  THRESHOLD_MANUAL_NUMBER.addEventListener("input", () => {
    syncThresholdControls("manual", Number(THRESHOLD_MANUAL_NUMBER.value));
  });

  // Always start Step 2 on the chooser.
  showSubstep("choose");
```

- [ ] **Step 4: Hook close/X to cancel in-flight captures**

Find the existing `CLOSE_BTN` handler:
```js
  CLOSE_BTN.addEventListener("click", () => {
    // X = leave state as-is; just navigate away. ...
    window.location.href = "/";
  });
```
Update to:
```js
  CLOSE_BTN.addEventListener("click", () => {
    captureAbortKey++;
    clearCalibrationBestEffort();
    window.location.href = "/";
  });
```

And the `data-wizard-skip` handler:
```js
  document.querySelectorAll("[data-wizard-skip]").forEach((b) => {
    b.addEventListener("click", async () => {
      captureAbortKey++;
      clearCalibrationBestEffort();
      if (await saveAndNavigate("skipped")) window.location.href = "/";
    });
  });
```

- [ ] **Step 5: Manual smoke test**

In a browser, walk through:
- Step 2 → Auto → Start 5s capture → wait → auto-advance → Start music capture → wait → land on result with three numbers + slider pre-filled.
- Tweak slider/number — both stay in sync.
- Click "Re-run calibration" — back to noise capture, samples reset.
- From result, click "Set manually instead" — manual screen shows; the slider value persists.
- Click "Looks good — continue" → MQTT step.
- Restart wizard, choose Manual directly — slider works; continue.
- Restart wizard, stop the engine container, choose Auto — warning appears, button does nothing harmful; Manual still works.

- [ ] **Step 6: Re-run all automated tests**

Run: `cd /home/ubuntu/SpinSense/SpinSense && python -m unittest discover -s . -t . -v`
Expected: All pre-existing + new tests PASS.

- [ ] **Step 7: Commit**

```bash
cd /home/ubuntu/SpinSense/SpinSense
git add gui/static/setup.js
git commit -m "feat(wizard): auto-calibrate capture flow + dB result screen"
```

---

## Acceptance Criteria (run after Task 11)

Manual verification on real hardware in Docker, per spec § Manual test plan:

- [ ] Fresh container, no `config.json`. Walk full auto path with a real record. Suggested threshold lands somewhere reasonable for your turntable's noise floor.
- [ ] Auto path, needle stays on runout for phase 2. Error message renders on the music capture screen, no advance. Clicking "Music started — capture now" again restarts music phase only.
- [ ] Auto path, click X mid-noise-capture. `curl http://localhost:8000/api/calibrate/status` returns `{"status": "none", ...}`.
- [ ] Manual path: meter moves in dB; setting threshold and continuing writes `Audio.Volume_Threshold` to `config.json` as a linear RMS value (not dB) — hand-check the file.
- [ ] After wizard, edit threshold in Settings (in dB), save. Engine logs reload within ~2 s.
- [ ] Dashboard input meter shows dB; threshold tick lands at the saved threshold.
- [ ] Auto-calibrate on a hot input (line-level music source through the same mic): result lands in -40 to -20 dB range with slider room on both sides.
- [ ] Pull engine container (or stop the engine process): Auto button shows the "engine not running" warning; Manual still works.

If any of these fail, file a follow-up task rather than amending — the per-task commits are the audit trail.

---

## Self-Review

Spec coverage check (against `docs/superpowers/specs/2026-05-31-auto-calibrate-wizard-design.md`):

- ✅ Wizard Step 2 sub-screens (2A-2E) — Task 10 + 11
- ✅ Three "Re-do" button variants — Task 11 (`RERUN_BTN`, music-only retry via re-click, back buttons clear state)
- ✅ dB conversion helper — Task 1
- ✅ dB range, slider step, display range — Task 1 + 8 + 9 + 10
- ✅ Storage stays linear — Task 11 `buildPayload`
- ✅ noise_p99, music_p10 anchors — Task 4 `_compute_stats`
- ✅ Threshold formula + 2 dB safety clamp + bad-capture error — Task 11 `applyThresholdFormula`
- ✅ Calibration state + audio_callback piggyback — Task 3
- ✅ Detection suppression during running capture — Task 6
- ✅ Timer task with identity-check guard — Task 4
- ✅ Command IPC + 3 commands — Task 5
- ✅ Backend endpoints + 503 on engine-down — Task 7
- ✅ Frontend FSM + 250 ms polling — Task 11
- ✅ Cancellation via clear_calibration — Task 11 (close, skip, back buttons)
- ✅ Engine-down detection on Screen 2A — Task 11 `checkEngineReachable`
- ✅ Settings dB + linked number input — Task 8
- ✅ Dashboard dB scale + threshold tick — Task 9
- ✅ Default Volume_Threshold = 0.01 in both engine and pydantic — Task 2
- ✅ Test files: test_db_utils.py, test_calibrate_api.py, test_calibrate_engine.py, test_config_round_trip.py update — Tasks 1, 2, 3, 4, 5, 7

No placeholders. Identifier consistency checked: `calibration`, `_compute_stats`, `_finish_calibration`, `_handle_command`, `_command_client_handler`, `command_listener_loop`, `CMD_SOCKET_PATH`, `_send_cmd`, `dbUtil`, `syncThresholdControls`, `applyThresholdFormula`, `runCapture`, `runNoiseCapture`, `runMusicCapture` — all defined in one task and consistently referenced in later tasks.
