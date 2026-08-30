"""What a manual rescan says about the play it corrects.

Hitting Rescan is the listener telling us the current identification is wrong.
Early in a play the reason is almost always a misfire — the needle-drop thump
started a scan of the lead-in groove — so the play being corrected never really
happened and should be replaced, not bookended. The engine's half of that is
one flag on one frame; the backend decides whether to act on it.
"""
import asyncio
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

import core_engine  # noqa: E402


class SupersedeFlagTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig = (core_engine.fetch_itunes_metadata, core_engine._write_uds)
        self.frames = []

        async def fake_itunes(artist, title):
            return "Album", "", 213, False

        async def fake_write(line):
            import json
            self.frames.append(json.loads(line))

        core_engine.fetch_itunes_metadata = fake_itunes
        core_engine._write_uds = fake_write
        core_engine._clear_track_state(set_backoff=False)
        core_engine.state["capture_mono"] = 1000.0
        core_engine.state["capture_wall"] = 1_756_338_000
        self._orig_retrigger = core_engine.runtime["retrigger_on_track_change"]

    def tearDown(self):
        (core_engine.fetch_itunes_metadata, core_engine._write_uds) = self._orig
        core_engine.runtime["retrigger_on_track_change"] = self._orig_retrigger
        core_engine._clear_track_state(set_backoff=False)
        core_engine.state["supersede_previous"] = False

    def track(self, title="T"):
        return {"title": title, "artist": "A", "album": None, "art_url": None,
                "isrc": None, "genre": None, "release_year": None,
                "duration_secs": None, "match_offset_secs": 0.0}

    def flags(self):
        return [f["payload"].get("supersedes_previous")
                for f in self.frames if f.get("type") == "live_status"]

    async def test_a_rescan_landing_on_a_different_track_flags_the_frame(self):
        await core_engine._handle_match(self.track("First"))
        self.frames.clear()
        await core_engine._handle_match(self.track("Second"), reason="manual")
        self.assertEqual(self.flags(), [True])

    async def test_a_rescan_confirming_the_same_track_flags_nothing(self):
        # Nothing to correct: the identification we would replace is the one
        # the rescan just agreed with.
        await core_engine._handle_match(self.track("First"))
        self.frames.clear()
        await core_engine._handle_match(self.track("First"), reason="manual")
        self.assertEqual(self.flags(), [False])

    async def test_an_ordinary_transition_flags_nothing(self):
        await core_engine._handle_match(self.track("First"))
        self.frames.clear()
        await core_engine._handle_match(self.track("Second"))
        self.assertEqual(self.flags(), [False])

    async def test_a_track_end_check_flags_nothing(self):
        # By then the previous play is minutes old and really did play.
        await core_engine._handle_match(self.track("First"))
        self.frames.clear()
        await core_engine._handle_match(self.track("Second"), reason="track_end")
        self.assertEqual(self.flags(), [False])

    async def test_the_flag_does_not_outlive_its_frame(self):
        # A flag left standing would tell the backend to drop a play that had
        # nothing to do with the rescan.
        await core_engine._handle_match(self.track("Second"), reason="manual")
        self.assertFalse(core_engine.state["supersede_previous"])
        payload = core_engine.build_status_payload(
            "playing", 0.1, core_engine.state)["payload"]
        self.assertFalse(payload["supersedes_previous"])

    async def test_the_idle_blip_is_skipped_when_superseding(self):
        # The blip is an empty-track frame, which the backend reads as the end
        # of the very play the rescan is about to replace.
        core_engine.runtime["retrigger_on_track_change"] = True
        await core_engine._handle_match(self.track("First"))
        self.frames.clear()
        await core_engine._handle_match(self.track("Second"), reason="manual")
        titles = [f["payload"]["track"]["title"] for f in self.frames]
        self.assertNotIn("", titles)

    async def test_the_idle_blip_still_fires_on_a_real_transition(self):
        core_engine.runtime["retrigger_on_track_change"] = True
        await core_engine._handle_match(self.track("First"))
        self.frames.clear()
        await core_engine._handle_match(self.track("Second"))
        titles = [f["payload"]["track"]["title"] for f in self.frames]
        self.assertIn("", titles)


class RescanIsManualTest(unittest.TestCase):
    """The rescan command is the only thing that sets force_scan, which is how
    the monitor loop knows to call recognize_audio(reason="manual")."""

    def test_the_rescan_command_arms_a_forced_scan(self):
        core_engine.state["force_scan"] = False
        core_engine.state["back_off"] = True
        reply = asyncio.run(core_engine._handle_command({"cmd": "rescan"}))
        self.assertTrue(reply["ok"])
        self.assertTrue(core_engine.state["force_scan"])
        self.assertFalse(core_engine.state["back_off"])
        core_engine.state["force_scan"] = False


if __name__ == "__main__":
    unittest.main()
