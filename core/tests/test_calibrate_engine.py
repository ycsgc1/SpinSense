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
