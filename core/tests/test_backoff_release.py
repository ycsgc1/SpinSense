"""Getting out of the back-off gate when the record never goes quiet.

After a failed identification the engine stops scanning until a qualifying gap
appears — the right rule on a studio LP, where a gap is exactly what separates
one track from the next. A live album has no gaps at all: applause runs into
crowd work runs into the next song. Observed in the field as several hundred
consecutive back-off ticks, the engine deaf for the rest of the side while music
played, with manual rescans the only way to get anything at all.

So audio has to be able to out-wait the gate. Slowly, because the usual reason
nothing identified is that nothing *can* be — an interlude, crowd noise, a
locked groove — and a record we cannot read should cost a few calls an hour.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

import core_engine  # noqa: E402


class BackoffWindowTest(unittest.TestCase):
    def test_the_first_wait_is_minutes_not_seconds(self):
        # Short enough to recover within a track, long enough not to hammer the
        # recognizer with audio it has already failed on.
        self.assertGreaterEqual(core_engine.backoff_window(0), 60)
        self.assertLessEqual(core_engine.backoff_window(0), 300)

    def test_each_failure_waits_longer(self):
        windows = [core_engine.backoff_window(n) for n in range(4)]
        self.assertEqual(windows, sorted(windows))
        self.assertGreater(windows[-1], windows[0])

    def test_the_wait_is_capped(self):
        self.assertEqual(core_engine.backoff_window(99),
                         core_engine.BACKOFF_RETRY_CAP_SECS)

    def test_it_does_not_expire_before_its_window(self):
        w = core_engine.backoff_window(0)
        self.assertFalse(core_engine.backoff_expired(1000.0, 1000.0 + w - 1, 0))

    def test_it_expires_once_the_window_has_passed(self):
        w = core_engine.backoff_window(0)
        self.assertTrue(core_engine.backoff_expired(1000.0, 1000.0 + w, 0))

    def test_an_unset_stamp_never_expires(self):
        # Fail closed: a missing reading must not open the gate by accident.
        self.assertFalse(core_engine.backoff_expired(0.0, 1e9, 0))

    def test_a_clock_that_went_backwards_never_expires(self):
        self.assertFalse(core_engine.backoff_expired(2000.0, 1000.0, 0))


class BackoffStateTest(unittest.TestCase):
    def setUp(self):
        core_engine._clear_track_state(set_backoff=False)

    def tearDown(self):
        core_engine._clear_track_state(set_backoff=False)

    def test_a_failed_identification_arms_the_gate_and_starts_its_clock(self):
        core_engine._clear_track_state(set_backoff=True)
        self.assertTrue(core_engine.state["back_off"])
        self.assertGreater(core_engine.state["back_off_since"], 0)

    def test_a_silence_stop_clears_the_gate_and_its_escalation(self):
        core_engine._clear_track_state(set_backoff=True)
        core_engine.state["back_off_attempts"] = 3
        core_engine._clear_track_state(set_backoff=False)
        self.assertFalse(core_engine.state["back_off"])
        self.assertEqual(core_engine.state["back_off_attempts"], 0)

    def test_audio_alone_opens_the_gate_once_it_has_waited(self):
        # The whole point: on a record with no gaps, this is the only way out.
        core_engine._clear_track_state(set_backoff=True)
        core_engine.state["back_off_since"] = 1000.0
        opened = core_engine.release_backoff_if_expired(
            1000.0 + core_engine.backoff_window(0))
        self.assertTrue(opened)
        self.assertFalse(core_engine.state["back_off"])

    def test_it_stays_shut_until_then(self):
        core_engine._clear_track_state(set_backoff=True)
        core_engine.state["back_off_since"] = 1000.0
        self.assertFalse(core_engine.release_backoff_if_expired(1001.0))
        self.assertTrue(core_engine.state["back_off"])

    def test_each_release_costs_an_escalation_step(self):
        # A record that cannot be identified should be asked about less and
        # less often, not on a fixed timer for the rest of the side.
        core_engine._clear_track_state(set_backoff=True)
        core_engine.state["back_off_since"] = 1000.0
        core_engine.release_backoff_if_expired(1000.0 + core_engine.backoff_window(0))
        self.assertEqual(core_engine.state["back_off_attempts"], 1)

    def test_a_gap_releasing_the_gate_resets_the_escalation(self):
        core_engine.state["back_off"] = True
        core_engine.state["back_off_attempts"] = 3
        core_engine.apply_backoff(False)
        self.assertEqual(core_engine.state["back_off_attempts"], 0)

    def test_a_gate_that_stays_shut_keeps_its_escalation(self):
        core_engine.state["back_off"] = True
        core_engine.state["back_off_attempts"] = 3
        core_engine.apply_backoff(True)
        self.assertEqual(core_engine.state["back_off_attempts"], 3)

    def test_a_real_gap_is_still_the_preferred_release(self):
        # The time-based escape is a fallback for records with no gaps. Where a
        # gap does exist it remains the signal, and it resets the escalation.
        counter, back_off, _stop = core_engine._silence_step(
            silence_counter=1, in_song=True, back_off=True,
            new_song_silence=2, stopped_silence=10)
        self.assertEqual(counter, 2)
        self.assertFalse(back_off)


if __name__ == "__main__":
    unittest.main()
