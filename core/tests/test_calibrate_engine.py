"""Unit tests for the calibration state machine in core_engine.py.

These tests instantiate the module and poke its globals directly; the engine
isn't designed for instance-based testing, so we keep tests serial (no
parallelism) and reset state in tearDown."""
import asyncio
import json
import os
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
