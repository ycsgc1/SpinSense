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
