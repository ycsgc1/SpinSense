import asyncio
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

import core_engine  # noqa: E402


class BuildStatusPayloadTest(unittest.TestCase):
    def test_playing_carries_track_and_phase(self):
        st = {"in_song": True, "title": "T", "artist": "A", "album": "Al",
              "art_url": "u", "isrc": "i", "genre": "g", "release_year": 2001}
        msg = core_engine.build_status_payload("playing", 0.4, st)
        self.assertEqual(msg["type"], "live_status")
        p = msg["payload"]
        self.assertEqual(p["phase"], "playing")
        self.assertEqual(p["status_msg"], "Playing")
        self.assertEqual(p["rms_level"], 0.4)
        self.assertEqual(p["track"]["title"], "T")
        self.assertEqual(p["track"]["release_year"], 2001)

    def test_scanning_keeps_current_track_but_marks_phase(self):
        # Invariant: phase frames keep the existing track so dedupe is unaffected.
        st = {"in_song": True, "title": "T", "artist": "A", "album": "Al", "art_url": "u"}
        p = core_engine.build_status_payload("scanning", 0.2, st)["payload"]
        self.assertEqual(p["phase"], "scanning")
        self.assertEqual(p["track"]["title"], "T")

    def test_listening_when_not_in_song(self):
        p = core_engine.build_status_payload("listening", 0.0, {"in_song": False})["payload"]
        self.assertEqual(p["status_msg"], "Listening")
        self.assertEqual(p["track"]["title"], "")


class RescanCommandTest(unittest.TestCase):
    def setUp(self):
        core_engine.state["force_scan"] = False
        core_engine.state["back_off"] = True

    def test_rescan_sets_force_and_clears_backoff(self):
        reply = asyncio.run(core_engine._handle_command({"cmd": "rescan"}))
        self.assertEqual(reply, {"ok": True})
        self.assertTrue(core_engine.state["force_scan"])
        self.assertFalse(core_engine.state["back_off"])

    def test_unknown_cmd_still_rejected(self):
        reply = asyncio.run(core_engine._handle_command({"cmd": "bogus"}))
        self.assertFalse(reply["ok"])


class RecognizeRetryTest(unittest.TestCase):
    def setUp(self):
        self.phases = []
        self.handled = []
        # Capture phase publishes instead of hitting the socket.
        async def fake_publish(phase):
            self.phases.append(phase)
        async def fake_capture():
            return b""
        async def fake_handle(track):
            self.handled.append(track)
            core_engine.state["in_song"] = True
            core_engine.state["back_off"] = False
        self._orig = (core_engine._publish_phase, core_engine._capture_sample,
                      core_engine._handle_match, core_engine._identify)
        core_engine._publish_phase = fake_publish
        core_engine._capture_sample = fake_capture
        core_engine._handle_match = fake_handle
        core_engine.state["back_off"] = False
        core_engine.state["in_song"] = False

    def tearDown(self):
        (core_engine._publish_phase, core_engine._capture_sample,
         core_engine._handle_match, core_engine._identify) = self._orig

    def test_all_miss_sets_no_match_and_backoff(self):
        async def always_none(_wav):
            return None
        core_engine._identify = always_none
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(
            self.phases,
            ["scanning", "identifying", "scanning", "retrying",
             "scanning", "retrying", "no_match"],
        )
        self.assertEqual(self.handled, [])
        self.assertTrue(core_engine.state["back_off"])
        self.assertFalse(core_engine.state["in_song"])

    def test_match_on_third_attempt_handles_and_no_backoff(self):
        self.calls = 0
        async def third(_wav):
            self.calls += 1
            return {"title": "Hit"} if self.calls == 3 else None
        core_engine._identify = third
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.handled, [{"title": "Hit"}])
        self.assertFalse(core_engine.state["back_off"])
        self.assertNotIn("no_match", self.phases)
