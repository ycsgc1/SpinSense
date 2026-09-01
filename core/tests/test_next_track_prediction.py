"""What is playing when nothing could be recognised.

A live album has no silence between songs — applause and crowd work run
straight into the next number — so gap detection never fires and the play clock
is the only transition signal there is. When recognition fails too, the old
behaviour kept showing the previous track and, after a few deferrals, disarmed
the clock entirely: on a side with no silence, that meant showing one song for
the rest of the record.

Knowing the record gives a better answer than "no idea". The next thing on the
platter is the next thing on the tracklist.
"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

import core_engine  # noqa: E402
from spinsense import itunes  # noqa: E402

LIVE_ID = 1868793244
ART = "http://cover/live/100x100bb.jpg"


def t(name, secs):
    return {"trackName": name, "artistName": "AJR", "artistId": 359553651,
            "trackTimeMillis": secs * 1000, "artworkUrl100": ART}


# Long enough after "Karma" that the streak cap, not the end of the record, is
# what stops a run of predictions.
LIVE_TRACKS = [
    t("Way Less Sad (Live from the Hollywood Bowl)", 209),
    t("Karma (Live from the Hollywood Bowl)", 293),
    t("Yes I'm A Mess (Live from the Hollywood Bowl)", 164),
    t("The Good Part (Live from the Hollywood Bowl)", 195),
    t("The Big Goodbye (Live from the Hollywood Bowl)", 342),
    t("100 Bad Days (Live from the Hollywood Bowl)", 244),
    t("Bang! (Live from the Hollywood Bowl)", 194),
]
LAST_TRACK = LIVE_TRACKS[-1]["trackName"]


class PredictNextTrackTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.phases = []

        async def fake_tracks(collection_id, timeout_secs=8.0):
            return LIVE_TRACKS if collection_id == LIVE_ID else []

        async def fake_event(level, message):
            self.events.append((level, message))

        async def fake_phase(phase):
            self.phases.append(phase)

        self._orig = (itunes.album_tracks, core_engine.emit_event,
                      core_engine._publish_phase)
        itunes.album_tracks = fake_tracks
        core_engine.emit_event = fake_event
        core_engine._publish_phase = fake_phase

        core_engine._tracklist_cache.clear()
        core_engine._clear_track_state(set_backoff=False)
        core_engine.album_context = {"id": LIVE_ID,
                                     "name": "Live from the Hollywood Bowl",
                                     "at": time.monotonic()}
        core_engine.state["in_song"] = True
        core_engine.state["title"] = "Karma"
        core_engine.state["artist"] = "AJR"
        core_engine.state["last_song"] = "AJR - Karma"

    def tearDown(self):
        (itunes.album_tracks, core_engine.emit_event,
         core_engine._publish_phase) = self._orig
        core_engine._tracklist_cache.clear()
        core_engine.album_context = None
        core_engine._clear_track_state(set_backoff=False)

    async def test_it_advances_to_the_next_track_on_the_record(self):
        # The song the stress test never identified: Shazam couldn't hear it
        # through the applause, but the record knows what follows Karma.
        self.assertTrue(await core_engine._advance_to_next_track())
        self.assertEqual(core_engine.state["title"],
                         "Yes I'm A Mess (Live from the Hollywood Bowl)")

    async def test_it_takes_that_tracks_own_duration(self):
        await core_engine._advance_to_next_track()
        self.assertEqual(core_engine.state["duration_secs"], 164)

    async def test_it_arms_a_fresh_clock_so_the_side_keeps_moving(self):
        # The point of the whole thing: without a clock, a side with no silence
        # never advances again.
        await core_engine._advance_to_next_track()
        clock = core_engine.state["clock"]
        self.assertIsNotNone(clock.deadline_mono)
        self.assertGreater(clock.deadline_mono, time.monotonic())

    async def test_it_claims_only_that_the_track_just_started(self):
        # No offset is trusted: the claim is "this began now", nothing more.
        await core_engine._advance_to_next_track()
        self.assertEqual(core_engine.state["clock"].position_secs, 0.0)

    async def test_it_keeps_the_record_it_inferred_from(self):
        await core_engine._advance_to_next_track()
        self.assertEqual(core_engine.state["album"], "Live from the Hollywood Bowl")

    async def test_it_says_so_rather_than_pretending_to_have_heard_it(self):
        await core_engine._advance_to_next_track()
        self.assertTrue(any("plays" in m and "next" in m for _lvl, m in self.events))

    async def test_it_walks_the_side_one_track_at_a_time(self):
        await core_engine._advance_to_next_track()
        await core_engine._advance_to_next_track()
        self.assertEqual(core_engine.state["title"],
                         "The Good Part (Live from the Hollywood Bowl)")

    # --- and where it must not reach ---

    async def test_it_stops_at_the_end_of_the_record(self):
        core_engine.state["title"] = LAST_TRACK
        self.assertFalse(await core_engine._advance_to_next_track())

    async def test_it_gives_up_rather_than_guessing_forever(self):
        # Each hop re-anchors on its own duration, but the gap between tracks —
        # applause, crowd work — is not in the tracklist, so the estimate
        # drifts. A few hops is inference; a dozen is fiction.
        hops = 0
        while await core_engine._advance_to_next_track():
            hops += 1
        self.assertEqual(hops, core_engine.MAX_PREDICTED_TRACKS)

    async def test_a_real_identification_clears_the_budget(self):
        core_engine.state["predicted_streak"] = core_engine.MAX_PREDICTED_TRACKS
        self.assertFalse(await core_engine._advance_to_next_track())
        core_engine.state["predicted_streak"] = 0        # what _handle_match does
        self.assertTrue(await core_engine._advance_to_next_track())

    async def test_an_unknown_record_predicts_nothing(self):
        core_engine.album_context = None
        self.assertFalse(await core_engine._advance_to_next_track())

    async def test_a_stale_record_predicts_nothing(self):
        core_engine.album_context["at"] = time.monotonic() - 10_000
        self.assertFalse(await core_engine._advance_to_next_track())

    async def test_a_track_not_on_the_record_predicts_nothing(self):
        # We don't know where we are, so we can't know what comes next.
        core_engine.state["title"] = "Some Other Song"
        self.assertFalse(await core_engine._advance_to_next_track())


class EndCheckFallsBackToPredictionTest(unittest.IsolatedAsyncioTestCase):
    """The wiring: a track-end check that identifies nothing should try the
    record before settling for showing a track that has finished."""

    def setUp(self):
        self.advanced = []

        async def fake_phase(phase):
            return None

        async def fake_capture(sample_len):
            return b""

        async def none_found(_wav):
            return None

        self._orig = (core_engine._publish_phase, core_engine._capture_sample,
                      core_engine._identify_shazam, core_engine._identify_fallback,
                      core_engine._advance_to_next_track, core_engine._rescan_pause)
        core_engine._publish_phase = fake_phase
        core_engine._capture_sample = fake_capture
        core_engine._identify_shazam = none_found
        core_engine._identify_fallback = none_found

        async def fake_pause(_s):
            return None
        core_engine._rescan_pause = fake_pause
        core_engine.state["in_song"] = True
        core_engine.state["title"] = "Karma"

    def tearDown(self):
        (core_engine._publish_phase, core_engine._capture_sample,
         core_engine._identify_shazam, core_engine._identify_fallback,
         core_engine._advance_to_next_track, core_engine._rescan_pause) = self._orig
        core_engine._clear_track_state(set_backoff=False)

    async def test_a_missed_end_check_asks_the_record(self):
        async def fake_advance():
            self.advanced.append(True)
            return True
        core_engine._advance_to_next_track = fake_advance
        await core_engine.recognize_audio(preserve_on_miss=True, reason="track_end")
        self.assertEqual(len(self.advanced), 1)

    async def test_it_still_keeps_the_track_when_the_record_is_unknown(self):
        async def no_advance():
            return False
        core_engine._advance_to_next_track = no_advance
        core_engine.state["clock"] = None
        await core_engine.recognize_audio(preserve_on_miss=True, reason="track_end")
        self.assertTrue(core_engine.state["in_song"])
        self.assertEqual(core_engine.state["title"], "Karma")

    async def test_an_ordinary_miss_never_predicts(self):
        # Not a track-end check: nothing says the current track is over.
        async def fake_advance():
            self.advanced.append(True)
            return True
        core_engine._advance_to_next_track = fake_advance
        await core_engine.recognize_audio()
        self.assertEqual(self.advanced, [])


if __name__ == "__main__":
    unittest.main()
