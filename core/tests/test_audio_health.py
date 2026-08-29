"""Sample normalisation and the input-stall watchdog.

Both exist because of field failures: quiet songs that scanned fine but the
recognizer couldn't place, and the engine going deaf twice in a month with the
meter pinned at exactly zero until something restarted it.
"""
import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

import core_engine  # noqa: E402


def tone(peak: int, n: int = 4800):
    return (np.sin(np.linspace(0, 100, n)) * peak).astype(np.int16)


def peak_of(samples) -> int:
    return int(np.max(np.abs(samples.astype(np.int32))))


class NormalizeTest(unittest.TestCase):
    def test_a_quiet_sample_is_brought_up(self):
        quiet = tone(2000)
        louder = core_engine.normalize_pcm(quiet, -3.0)
        self.assertGreater(peak_of(louder), peak_of(quiet) * 5)

    def test_it_reaches_the_target_when_the_gain_cap_allows(self):
        # -3 dBFS of int16 full scale.
        target = 32767 * (10 ** (-3.0 / 20))
        out = core_engine.normalize_pcm(tone(8000), -3.0)
        self.assertAlmostEqual(peak_of(out), target, delta=target * 0.02)

    def test_a_loud_sample_is_never_attenuated(self):
        # Turning a good sample down would be a regression, not a fix.
        loud = tone(32000)
        self.assertTrue(np.array_equal(core_engine.normalize_pcm(loud, -3.0), loud))

    def test_gain_is_capped_so_noise_is_not_amplified_into_garbage(self):
        whisper = tone(2)
        out = core_engine.normalize_pcm(whisper, -3.0)
        cap = 10 ** (core_engine.MAX_NORMALIZE_GAIN_DB / 20)
        self.assertLessEqual(peak_of(out), peak_of(whisper) * cap + 1)
        self.assertLess(peak_of(out), 1000)

    def test_digital_silence_is_returned_untouched(self):
        silence = np.zeros(100, dtype=np.int16)
        self.assertEqual(peak_of(core_engine.normalize_pcm(silence, -3.0)), 0)

    def test_empty_input_is_safe(self):
        empty = np.zeros(0, dtype=np.int16)
        self.assertEqual(len(core_engine.normalize_pcm(empty, -3.0)), 0)

    def test_output_never_overflows_int16(self):
        # numpy wraps on int16 overflow, which would flip a loud transient to
        # full-scale noise of the opposite sign — audible garbage to a matcher.
        out = core_engine.normalize_pcm(tone(30000), 0.0)
        self.assertEqual(out.dtype, np.int16)
        self.assertLessEqual(peak_of(out), 32767)

    def test_a_positive_target_is_clamped_to_full_scale(self):
        out = core_engine.normalize_pcm(tone(1000), 12.0)
        self.assertLessEqual(peak_of(out), 32767)

    def test_the_waveform_keeps_its_shape(self):
        quiet = tone(1000)
        out = core_engine.normalize_pcm(quiet, -3.0)
        # Same zero crossings: we scaled it, we didn't distort it.
        self.assertTrue(np.array_equal(np.sign(quiet), np.sign(out)))


class StallDetectionTest(unittest.TestCase):
    def detect(self, now, last_cb, rms, zero_run=0.0):
        return core_engine.detect_input_stall(now, last_cb, rms, zero_run)

    def test_a_live_input_is_never_flagged(self):
        zero_run, reason = self.detect(now=100.0, last_cb=99.5, rms=0.004)
        self.assertIsNone(reason)
        self.assertEqual(zero_run, 0.0)

    def test_a_silent_record_alone_is_not_a_stall(self):
        # Between sides the RMS is small but not bit-exact zero; that's normal.
        _, reason = self.detect(now=100.0, last_cb=99.9, rms=0.00001)
        self.assertIsNone(reason)

    def test_the_callback_going_quiet_is_a_stall(self):
        # It fires ~22x/sec, so seconds of nothing means the device is gone.
        _, reason = self.detect(
            now=100.0, last_cb=100.0 - core_engine.CALLBACK_TIMEOUT_SECS - 1, rms=0.004)
        self.assertEqual(reason, "no audio callbacks")

    def test_sustained_exact_zero_is_a_stall(self):
        # How the real failure presented: callback alive, handing us silence.
        zero_run = core_engine.ZERO_RMS_STALL_SECS - 1
        zero_run, reason = self.detect(now=100.0, last_cb=99.9, rms=0.0, zero_run=zero_run)
        self.assertEqual(reason, "input silent at exactly zero")

    def test_a_brief_run_of_zeros_is_tolerated(self):
        zero_run, reason = self.detect(now=100.0, last_cb=99.9, rms=0.0, zero_run=3.0)
        self.assertIsNone(reason)
        self.assertEqual(zero_run, 4.0)

    def test_any_real_signal_resets_the_zero_run(self):
        zero_run, reason = self.detect(now=100.0, last_cb=99.9, rms=0.002, zero_run=25.0)
        self.assertEqual(zero_run, 0.0)
        self.assertIsNone(reason)

    def test_the_zero_run_accumulates_to_the_threshold(self):
        zero_run, reason = 0.0, None
        for _ in range(int(core_engine.ZERO_RMS_STALL_SECS)):
            zero_run, reason = self.detect(now=100.0, last_cb=99.9, rms=0.0,
                                           zero_run=zero_run)
        self.assertEqual(reason, "input silent at exactly zero")


class StallWatchResetTest(unittest.TestCase):
    """Recognition closes the stream and zeroes the RMS deliberately. Without a
    reset the watchdog reads its own handiwork as a dead device."""

    def test_reset_rearms_both_signals(self):
        core_engine.state["last_callback_mono"] = 0.0
        core_engine.state["zero_run_secs"] = 99.0
        core_engine._reset_stall_watch()
        self.assertEqual(core_engine.state["zero_run_secs"], 0.0)
        self.assertGreater(core_engine.state["last_callback_mono"], 0.0)

        _, reason = core_engine.detect_input_stall(
            core_engine.state["last_callback_mono"],
            core_engine.state["last_callback_mono"],
            0.0,
            core_engine.state["zero_run_secs"],
        )
        self.assertIsNone(reason)


class StallInFrameTest(unittest.TestCase):
    def test_a_healthy_input_reports_ok(self):
        payload = core_engine.build_status_payload(
            "listening", 0.001, {"in_song": False})["payload"]
        self.assertTrue(payload["input_ok"])

    def test_a_stalled_input_is_visible_to_consumers(self):
        # Otherwise a dead device is indistinguishable from a silent record.
        payload = core_engine.build_status_payload(
            "listening", 0.0, {"in_song": False, "input_stalled": True})["payload"]
        self.assertFalse(payload["input_ok"])


if __name__ == "__main__":
    unittest.main()
