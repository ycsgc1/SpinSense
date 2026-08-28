"""Engine-side wiring for track-end prediction. The arithmetic lives in
test_track_clock.py; this file only pins the seams — that the offset survives
the recognition path, that the clock anchors where it should, and that an
end-check miss is non-destructive."""
import asyncio
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

import core_engine  # noqa: E402
import track_clock  # noqa: E402


class FakeShazam:
    def __init__(self, response):
        self.response = response

    async def recognize(self, _wav):
        return self.response


class IdentifyShazamOffsetTest(unittest.TestCase):
    """Shazam is the only backend that reports a playhead; the other two must
    say so explicitly rather than leaving the key absent."""

    def setUp(self):
        self._orig = core_engine.shazam

    def tearDown(self):
        core_engine.shazam = self._orig

    def _identify(self, response):
        core_engine.shazam = FakeShazam(response)
        return asyncio.run(core_engine._identify_shazam(b""))

    def test_offset_is_carried_into_the_normalized_track(self):
        track = self._identify({
            "matches": [{"offset": 42.0}],
            "track": {"title": "T", "subtitle": "A"},
        })
        self.assertEqual(track["match_offset_secs"], 42.0)

    def test_missing_offset_is_none_not_an_error(self):
        track = self._identify({"matches": [], "track": {"title": "T", "subtitle": "A"}})
        self.assertIsNone(track["match_offset_secs"])

    def test_audd_and_acoustid_declare_no_playhead(self):
        audd = core_engine._audd_to_normalized({"title": "T", "artist": "A"})
        self.assertIsNone(audd["match_offset_secs"])
        acoustid = core_engine._acoustid_to_normalized(
            [{"score": 1.0, "recordings": [{"title": "T", "artists": [{"name": "A"}]}]}])
        self.assertIsNone(acoustid["match_offset_secs"])


class HandleMatchClockTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig = (core_engine.fetch_itunes_metadata,
                      core_engine.fetch_image_base64,
                      core_engine._publish_phase,
                      core_engine.publish_state)
        self.duration = 213

        async def fake_itunes(artist, title):
            return "Album", "", self.duration

        async def fake_art(url):
            return ""

        async def fake_phase(phase):
            return None

        core_engine.fetch_itunes_metadata = fake_itunes
        core_engine.fetch_image_base64 = fake_art
        core_engine._publish_phase = fake_phase
        core_engine.publish_state = lambda *a, **k: None

        core_engine._clear_track_state(set_backoff=False)
        core_engine.state["capture_mono"] = 1000.0
        core_engine.state["capture_wall"] = 1_756_338_000
        core_engine.runtime["track_end_grace"] = 20.0

    def tearDown(self):
        (core_engine.fetch_itunes_metadata, core_engine.fetch_image_base64,
         core_engine._publish_phase, core_engine.publish_state) = self._orig
        core_engine._clear_track_state(set_backoff=False)

    def track(self, title="T", offset=42.0):
        return {"title": title, "artist": "A", "album": None, "art_url": None,
                "isrc": None, "genre": None, "release_year": None,
                "duration_secs": None, "match_offset_secs": offset}

    async def test_clock_anchors_to_the_capture_not_the_match(self):
        # The capture stamps are set well before _handle_match runs; anchoring
        # to them is what keeps recognition latency out of the estimate.
        await core_engine._handle_match(self.track())
        clock = core_engine.state["clock"]
        self.assertEqual(clock.anchor_mono, 1000.0)
        self.assertEqual(clock.anchor_wall, 1_756_338_000)
        self.assertEqual(clock.position_secs, 42.0)

    async def test_frame_carries_the_play_clock(self):
        await core_engine._handle_match(self.track())
        payload = core_engine.build_status_payload(
            "playing", 0.5, core_engine.state)["payload"]
        self.assertEqual(payload["play_clock"]["started_at"], 1_756_338_000 - 42)
        self.assertEqual(payload["play_clock"]["duration_secs"], 213)

    async def test_no_play_clock_when_nothing_is_playing(self):
        payload = core_engine.build_status_payload(
            "listening", 0.0, core_engine.state)["payload"]
        self.assertIsNone(payload["play_clock"])

    async def test_clearing_the_track_drops_the_clock(self):
        await core_engine._handle_match(self.track())
        self.assertIsNotNone(core_engine.state["clock"])
        core_engine._clear_track_state(set_backoff=True)
        self.assertIsNone(core_engine.state["clock"])

    async def test_end_check_on_the_same_track_spends_budget(self):
        await core_engine._handle_match(self.track(), reason="track_end")
        first = core_engine.state["clock"].rescans
        await core_engine._handle_match(self.track(), reason="track_end")
        self.assertEqual(core_engine.state["clock"].rescans, first + 1)

    async def test_ordinary_match_on_the_same_track_resets_budget(self):
        # A same-track confirmation after a real detected gap means detection
        # is working; the budget shouldn't stay spent for the rest of the side.
        await core_engine._handle_match(self.track(), reason="track_end")
        await core_engine._handle_match(self.track(), reason="track_end")
        self.assertGreater(core_engine.state["clock"].rescans, 0)
        await core_engine._handle_match(self.track(), reason="onset")
        self.assertEqual(core_engine.state["clock"].rescans, 0)

    async def test_a_different_track_starts_a_fresh_budget(self):
        await core_engine._handle_match(self.track(), reason="track_end")
        await core_engine._handle_match(self.track(), reason="track_end")
        await core_engine._handle_match(self.track(title="Other"), reason="track_end")
        self.assertEqual(core_engine.state["clock"].rescans, 0)

    async def test_a_track_without_a_duration_is_never_end_checked(self):
        self.duration = None
        await core_engine._handle_match(self.track())
        self.assertIsNone(core_engine.state["clock"].deadline_mono)


class PreserveOnMissTest(unittest.IsolatedAsyncioTestCase):
    """An end-check runs against a track we're already playing. A miss there
    means 'we couldn't tell', not 'there's nothing here' — wiping now-playing
    on it would make the feature worse than not having it."""

    def setUp(self):
        self.phases = []
        self._orig = (core_engine._publish_phase, core_engine._capture_sample,
                      core_engine._identify_shazam, core_engine._identify_fallback,
                      core_engine._rescan_pause)

        async def fake_phase(phase):
            self.phases.append(phase)

        async def fake_capture(sample_len=None):
            return b""

        async def always_miss(_wav):
            return None

        async def no_pause(_seconds):
            return None

        core_engine._publish_phase = fake_phase
        core_engine._capture_sample = fake_capture
        core_engine._identify_shazam = always_miss
        core_engine._identify_fallback = always_miss
        core_engine._rescan_pause = no_pause

        core_engine.state.update({
            "in_song": True, "last_song": "A - T", "title": "T", "artist": "A",
            "back_off": False,
        })
        core_engine.state["clock"] = track_clock.start_clock(
            213, 0.0, 1000.0, 1_756_338_000, 20.0)

    def tearDown(self):
        (core_engine._publish_phase, core_engine._capture_sample,
         core_engine._identify_shazam, core_engine._identify_fallback,
         core_engine._rescan_pause) = self._orig
        core_engine._clear_track_state(set_backoff=False)

    async def test_miss_keeps_the_track_and_defers_the_clock(self):
        before = core_engine.state["clock"].deadline_mono
        await core_engine.recognize_audio(preserve_on_miss=True, reason="track_end")
        self.assertTrue(core_engine.state["in_song"])
        self.assertEqual(core_engine.state["title"], "T")
        self.assertFalse(core_engine.state["back_off"])
        self.assertNotEqual(core_engine.state["clock"].deadline_mono, before)
        self.assertNotIn("no_match", self.phases)

    async def test_ordinary_miss_still_tears_the_track_down(self):
        await core_engine.recognize_audio()
        self.assertFalse(core_engine.state["in_song"])
        self.assertEqual(core_engine.state["title"], "")
        self.assertTrue(core_engine.state["back_off"])
        self.assertIn("no_match", self.phases)

    async def test_an_end_check_never_leaves_the_deadline_in_the_past(self):
        # The monitor loop fires whenever now >= deadline, so a check that left
        # a stale deadline behind would re-fire on every single tick. Every
        # outcome must either push it forward or disarm.
        for _ in range(track_clock.MAX_END_RESCANS + 2):
            now = time.monotonic()
            await core_engine.recognize_audio(preserve_on_miss=True, reason="track_end")
            deadline = core_engine.state["clock"].deadline_mono
            if deadline is not None:
                self.assertGreater(deadline, now)

    async def test_repeated_end_check_misses_eventually_disarm(self):
        for _ in range(track_clock.MAX_END_RESCANS + 1):
            await core_engine.recognize_audio(preserve_on_miss=True, reason="track_end")
        self.assertIsNone(core_engine.state["clock"].deadline_mono)
        self.assertTrue(core_engine.state["in_song"])


if __name__ == "__main__":
    unittest.main()
