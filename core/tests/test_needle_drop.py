"""Rejecting the scan a needle drop triggers.

The reported failure: lowering the needle spiked the meter, the engine scanned
the lead-in groove, and the half second of music that reached the end of the
sample was enough for the recognizer to answer — with the wrong song. Nothing
corrected it afterwards, because no gap follows the first track of a side.

A needle drop and a song look nothing alike once you ask how much of the
sample had sound in it, which costs no recognition call to find out.
"""
import asyncio
import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

import core_engine  # noqa: E402

RATE = core_engine.SAMPLE_RATE
PEAK = core_engine._INT16_PEAK


def _secs(n: float) -> int:
    return int(RATE * n)


def silence(secs: float):
    return np.zeros(_secs(secs), dtype=np.int16)


def tone(secs: float, amplitude: float = 0.3):
    """A steady sine at `amplitude` of full scale — a stand-in for music."""
    t = np.arange(_secs(secs), dtype=np.float32) / RATE
    return (np.sin(2 * np.pi * 440 * t) * amplitude * PEAK).astype(np.int16)


def needle_drop(total_secs: float = 5.0, thump_secs: float = 0.15):
    """A loud transient, then the lead-in groove."""
    return np.concatenate([tone(thump_secs, 0.9), silence(total_secs - thump_secs)])


class ActiveRatioTest(unittest.TestCase):
    THRESHOLD = 0.01

    def ratio(self, samples):
        return core_engine.active_audio_ratio(samples, self.THRESHOLD)

    def test_a_full_sample_of_music_is_entirely_active(self):
        self.assertEqual(self.ratio(tone(5.0)), 1.0)

    def test_silence_is_entirely_inactive(self):
        self.assertEqual(self.ratio(silence(5.0)), 0.0)

    def test_a_needle_drop_is_almost_all_silence(self):
        # The thump is 0.15s of 5s: a few percent, nowhere near the cutoff.
        self.assertLess(self.ratio(needle_drop()), core_engine.MIN_ACTIVE_SAMPLE_RATIO)

    def test_a_needle_drop_that_catches_the_song_starting_is_still_rejected(self):
        # The exact reported case: the last half second is real music, and it
        # is real enough to be identified — from the wrong half of the record.
        sample = np.concatenate([tone(0.15, 0.9), silence(4.35), tone(0.5)])
        self.assertLess(self.ratio(sample), core_engine.MIN_ACTIVE_SAMPLE_RATIO)

    def test_a_song_with_a_quiet_passage_still_passes(self):
        # Music is not uniformly loud, and dropping a real song would cost a
        # play. Two seconds of near-silence inside five still reads as music.
        sample = np.concatenate([tone(1.5), silence(2.0), tone(1.5)])
        self.assertGreaterEqual(self.ratio(sample), core_engine.MIN_ACTIVE_SAMPLE_RATIO)

    def test_a_quiet_pressing_counts_as_music(self):
        # The whole point of the boost feature is that quiet is not silent.
        # Anything above the listening threshold is audio, however far above.
        quiet = tone(5.0, amplitude=0.02)
        self.assertEqual(self.ratio(quiet), 1.0)

    def test_audio_below_the_listening_threshold_is_not_counted(self):
        # Symmetry with the monitor loop: the same threshold, the same verdict.
        under = tone(5.0, amplitude=0.001)
        self.assertEqual(self.ratio(under), 0.0)

    def test_an_empty_or_unmeasurable_capture_reads_zero(self):
        self.assertEqual(self.ratio(np.zeros(0, dtype=np.int16)), 0.0)
        self.assertEqual(self.ratio(None), 0.0)
        self.assertEqual(self.ratio(tone(0.01)), 0.0)  # shorter than one frame

    def test_a_column_shaped_recording_is_handled(self):
        # sd.rec() returns (frames, channels), not a flat array.
        self.assertEqual(self.ratio(tone(5.0).reshape(-1, 1)), 1.0)


class GuardedRecognitionTest(unittest.TestCase):
    """recognize_audio() gives up before spending a call on a needle drop."""

    def setUp(self):
        self.phases = []
        self.identified = 0
        self.events = []

        async def fake_publish(phase):
            self.phases.append(phase)

        async def fake_capture(sample_len):
            core_engine.state["sample_active_ratio"] = self.ratio
            return b""

        async def fake_identify(_wav):
            self.identified += 1
            return {"title": "Wrong Song", "artist": "Wrong Artist"}

        async def fake_event(level, message):
            self.events.append((level, message))

        async def fake_handle(track, reason="onset"):
            core_engine.state["in_song"] = True

        self._orig = (core_engine._publish_phase, core_engine._capture_sample,
                      core_engine._identify_shazam, core_engine._handle_match,
                      core_engine.emit_event)
        core_engine._publish_phase = fake_publish
        core_engine._capture_sample = fake_capture
        core_engine._identify_shazam = fake_identify
        core_engine._handle_match = fake_handle
        core_engine.emit_event = fake_event

        self._orig_guard = core_engine.runtime["needle_drop_guard"]
        self._orig_wait = core_engine.runtime["rescan_wait"]
        core_engine.runtime["needle_drop_guard"] = True
        core_engine.runtime["rescan_wait"] = 0
        core_engine.state["in_song"] = False
        core_engine.state["back_off"] = False
        core_engine.state["sample_active_ratio"] = 1.0
        core_engine.state["needle_drop_streak"] = 0
        self.ratio = 0.05  # a needle drop, unless a test says otherwise

    def tearDown(self):
        (core_engine._publish_phase, core_engine._capture_sample,
         core_engine._identify_shazam, core_engine._handle_match,
         core_engine.emit_event) = self._orig
        core_engine.runtime["needle_drop_guard"] = self._orig_guard
        core_engine.runtime["rescan_wait"] = self._orig_wait
        core_engine.state["in_song"] = False
        core_engine.state["back_off"] = False
        core_engine.state["sample_active_ratio"] = 1.0
        core_engine.state["needle_drop_streak"] = 0

    def test_a_needle_drop_is_never_sent_to_the_recognizer(self):
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.identified, 0)

    def test_a_new_play_starts_the_guard_over(self):
        # _clear_track_state runs on silence and on a given-up recognition;
        # both mean the next thing to happen is a fresh drop of the needle.
        core_engine.state["needle_drop_streak"] = core_engine.MAX_NEEDLE_DROP_ABORTS
        core_engine._clear_track_state(set_backoff=False)
        self.assertEqual(core_engine.state["needle_drop_streak"], 0)

    def test_the_ladder_stops_rather_than_retrying_the_same_silence(self):
        # Retrying would capture the same lead-in groove twice more.
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.phases.count("scanning"), 1)

    def test_it_leaves_the_gate_open_for_the_music(self):
        # Arming the back-off here would be a trap: if the song had already
        # started during the capture, the audio never goes quiet again, the
        # back-off never clears, and the engine sits out the whole first track.
        # The lead-in groove is silence, so the ordinary path already waits.
        asyncio.run(core_engine.recognize_audio())
        self.assertFalse(core_engine.state["back_off"])

    def test_the_song_that_follows_is_identified_normally(self):
        # The scenario the back-off would have broken, end to end: a thump is
        # rejected, then the music that was already playing gets a full sample.
        asyncio.run(core_engine.recognize_audio())
        self.ratio = 0.95
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.identified, 1)

    def test_it_stops_refusing_after_a_few_tries(self):
        # An intermittent input — a click once per revolution, a dusty groove —
        # would otherwise be rejected forever. The guard is a heuristic about a
        # mechanical event, so it gets a bounded number of votes.
        for _ in range(core_engine.MAX_NEEDLE_DROP_ABORTS + 1):
            asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.identified, 1)

    def test_a_real_sample_clears_the_streak(self):
        # Two needle drops half an evening apart are not an intermittent input,
        # and the second one should still be caught.
        asyncio.run(core_engine.recognize_audio())
        self.ratio = 0.95
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(core_engine.state["needle_drop_streak"], 0)

    def test_it_returns_to_listening_rather_than_reporting_no_match(self):
        # no_match arms a different back-off and tells the dashboard we tried
        # and failed. We didn't try, and there was nothing there to fail on.
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.phases[-1], "listening")

    def test_it_says_so_in_the_diagnostics_log(self):
        asyncio.run(core_engine.recognize_audio())
        self.assertTrue(any("needle drop" in m for _lvl, m in self.events))

    def test_a_real_sample_goes_through(self):
        self.ratio = 0.95
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.identified, 1)

    def test_the_guard_can_be_turned_off(self):
        core_engine.runtime["needle_drop_guard"] = False
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.identified, 1)

    def test_a_manual_rescan_is_always_honoured(self):
        # The listener asked. Refusing to look because the room is quiet would
        # make the button appear broken.
        asyncio.run(core_engine.recognize_audio(reason="manual"))
        self.assertEqual(self.identified, 1)

    def test_a_track_end_check_is_not_guarded(self):
        # By definition it runs mid-play, and its miss path is the one that
        # keeps the current track rather than clearing it.
        core_engine.state["in_song"] = True
        asyncio.run(core_engine.recognize_audio(preserve_on_miss=True,
                                                reason="track_end"))
        self.assertEqual(self.identified, 1)

    def test_a_quiet_passage_mid_song_is_not_guarded(self):
        # in_song means we already know what is playing; a quiet sample there
        # is a quiet passage, not a needle drop.
        core_engine.state["in_song"] = True
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.identified, 1)


if __name__ == "__main__":
    unittest.main()
