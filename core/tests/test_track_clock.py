import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

import track_clock  # noqa: E402


class ExtractMatchOffsetTest(unittest.TestCase):
    """The offset comes off a third-party payload we don't control, so every
    shape that isn't a usable number must be a clean None, never a raise."""

    def test_reads_first_match_offset(self):
        raw = {"matches": [{"offset": 42.5}, {"offset": 99.0}], "track": {}}
        self.assertEqual(track_clock.extract_match_offset(raw), 42.5)

    def test_integer_offset_becomes_float(self):
        self.assertEqual(track_clock.extract_match_offset({"matches": [{"offset": 7}]}), 7.0)

    def test_missing_and_malformed_shapes_are_none(self):
        for raw in (
            None, {}, "not a dict", {"matches": None}, {"matches": []},
            {"matches": ["nope"]}, {"matches": [{}]},
            {"matches": [{"offset": None}]}, {"matches": [{"offset": "12"}]},
            {"matches": [{"offset": float("nan")}]},
            {"matches": [{"offset": float("inf")}]},
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(track_clock.extract_match_offset(raw))

    def test_bool_is_not_a_number(self):
        # True would otherwise sneak through as 1.0 and claim a playhead.
        self.assertIsNone(track_clock.extract_match_offset({"matches": [{"offset": True}]}))


class ResolvePositionTest(unittest.TestCase):
    def test_plausible_offset_is_trusted(self):
        pos, source = track_clock.resolve_position(42.0, 213)
        self.assertEqual(pos, 42.0)
        self.assertEqual(source, track_clock.POSITION_FROM_OFFSET)

    def test_missing_offset_assumes_the_top_of_the_track(self):
        pos, source = track_clock.resolve_position(None, 213)
        self.assertEqual(pos, 0.0)
        self.assertEqual(source, track_clock.POSITION_ASSUMED)

    def test_negative_offset_is_rejected(self):
        self.assertEqual(track_clock.resolve_position(-1.0, 213)[1],
                         track_clock.POSITION_ASSUMED)

    def test_offset_beyond_duration_plus_margin_is_rejected(self):
        # Well past the end means we don't understand the number; assuming the
        # top makes the prediction fire late, which is the safe direction.
        self.assertEqual(track_clock.resolve_position(400.0, 213)[1],
                         track_clock.POSITION_ASSUMED)

    def test_offset_inside_the_sanity_margin_is_clamped_not_rejected(self):
        # A small overshoot is normal (different masters, sample straddling
        # the end) — believe it, but don't let the playhead exceed the track.
        pos, source = track_clock.resolve_position(215.0, 213)
        self.assertEqual(pos, 213.0)
        self.assertEqual(source, track_clock.POSITION_FROM_OFFSET)

    def test_offset_without_a_duration_is_kept_unclamped(self):
        pos, source = track_clock.resolve_position(42.0, None)
        self.assertEqual(pos, 42.0)
        self.assertEqual(source, track_clock.POSITION_FROM_OFFSET)


class GraceWindowTest(unittest.TestCase):
    def test_flat_floor_governs_short_tracks(self):
        # 10% of 2:30 is 15s; the 20s floor is larger and wins.
        self.assertEqual(track_clock.grace_window(150, 20.0), 20.0)

    def test_percentage_governs_long_tracks(self):
        # 10% of a 9-minute track is 54s, well past the flat floor.
        self.assertAlmostEqual(track_clock.grace_window(540, 20.0), 54.0)

    def test_percentage_is_capped(self):
        self.assertEqual(track_clock.grace_window(3000, 20.0),
                         track_clock.GRACE_CAP_SECS)

    def test_no_duration_falls_back_to_the_floor(self):
        self.assertEqual(track_clock.grace_window(None, 20.0), 20.0)

    def test_negative_floor_is_clamped_to_zero(self):
        self.assertEqual(track_clock.grace_window(None, -5.0), 0.0)


class StartClockTest(unittest.TestCase):
    def test_deadline_is_remaining_time_plus_grace_from_the_anchor(self):
        clock = track_clock.start_clock(
            duration_secs=213, match_offset_secs=42.0,
            anchor_mono=1000.0, anchor_wall=1_756_338_000, grace_floor_secs=20.0)
        # 171s left, grace = max(20, 21.3) = 21.3
        self.assertAlmostEqual(clock.deadline_mono, 1000.0 + 171.0 + 21.3)

    def test_no_duration_disarms_the_prediction(self):
        clock = track_clock.start_clock(
            duration_secs=None, match_offset_secs=42.0,
            anchor_mono=1000.0, anchor_wall=1, grace_floor_secs=20.0)
        self.assertIsNone(clock.deadline_mono)
        self.assertIsNone(clock.duration_secs)

    def test_fresh_clock_starts_with_a_full_rescan_budget(self):
        clock = track_clock.start_clock(213, None, 1000.0, 1, 20.0)
        self.assertEqual(clock.rescans, 0)

    def test_same_track_re_arm_inherits_and_increments_the_budget(self):
        # An end-check landing on the same track means the prediction was
        # wrong; the budget must carry over or a bad duration re-scans forever.
        first = track_clock.start_clock(213, None, 1000.0, 1, 20.0)
        second = track_clock.start_clock(213, None, 1200.0, 2, 20.0, previous=first)
        self.assertEqual(second.rescans, 1)
        self.assertIsNotNone(second.deadline_mono)

    def test_re_arming_past_the_cap_disarms(self):
        clock = track_clock.start_clock(213, None, 1000.0, 1, 20.0)
        for _ in range(track_clock.MAX_END_RESCANS):
            clock = track_clock.start_clock(213, None, 1000.0, 1, 20.0, previous=clock)
            self.assertIsNotNone(clock.deadline_mono)
        clock = track_clock.start_clock(213, None, 1000.0, 1, 20.0, previous=clock)
        self.assertGreater(clock.rescans, track_clock.MAX_END_RESCANS)
        self.assertIsNone(clock.deadline_mono)


class ShouldCheckEndTest(unittest.TestCase):
    def setUp(self):
        self.clock = track_clock.start_clock(213, 0.0, 1000.0, 1, 20.0)
        self.deadline = self.clock.deadline_mono
        self.gates = dict(enabled=True, in_song=True, backing_off=False,
                          gap_qualified=False)

    def check(self, now, **overrides):
        return track_clock.should_check_end(
            self.clock, now, **{**self.gates, **overrides})

    def test_fires_once_the_deadline_passes(self):
        self.assertFalse(self.check(self.deadline - 0.1))
        self.assertTrue(self.check(self.deadline))
        self.assertTrue(self.check(self.deadline + 100))

    def test_every_gate_suppresses_it(self):
        for gate in ("enabled", "in_song"):
            with self.subTest(gate=gate):
                self.assertFalse(self.check(self.deadline + 100, **{gate: False}))
        for gate in ("backing_off", "gap_qualified"):
            with self.subTest(gate=gate):
                self.assertFalse(self.check(self.deadline + 100, **{gate: True}))

    def test_qualified_gap_stands_down(self):
        # The ordinary path scans as soon as audio returns, so an end-check
        # here buys nothing — and would otherwise burn the budget on a runout.
        self.assertFalse(self.check(self.deadline + 600, gap_qualified=True))

    def test_no_clock_never_fires(self):
        self.clock = None
        self.assertFalse(self.check(1e9))

    def test_disarmed_clock_never_fires(self):
        self.clock.deadline_mono = None
        self.assertFalse(self.check(1e9))


class DeferTest(unittest.TestCase):
    def setUp(self):
        self.clock = track_clock.start_clock(213, 0.0, 1000.0, 1, 20.0)

    def test_each_deferral_pushes_the_deadline_further_out(self):
        gaps = []
        now = 2000.0
        for _ in range(track_clock.MAX_END_RESCANS):
            track_clock.defer(self.clock, now)
            gaps.append(self.clock.deadline_mono - now)
        self.assertEqual(gaps, sorted(gaps))
        self.assertLess(gaps[0], gaps[-1])

    def test_deferral_is_capped(self):
        self.clock.grace_secs = 1000.0
        track_clock.defer(self.clock, 2000.0)
        self.assertLessEqual(self.clock.deadline_mono - 2000.0,
                             track_clock.BACKOFF_CAP_SECS)

    def test_exhausting_the_budget_disarms(self):
        for _ in range(track_clock.MAX_END_RESCANS):
            track_clock.defer(self.clock, 2000.0)
            self.assertIsNotNone(self.clock.deadline_mono)
        track_clock.defer(self.clock, 2000.0)
        self.assertIsNone(self.clock.deadline_mono)

    def test_deferring_a_disarmed_or_missing_clock_is_a_no_op(self):
        track_clock.defer(None, 2000.0)  # must not raise
        self.clock.deadline_mono = None
        track_clock.defer(self.clock, 2000.0)
        self.assertIsNone(self.clock.deadline_mono)
        self.assertEqual(self.clock.rescans, 0)


class PlayClockPayloadTest(unittest.TestCase):
    def test_started_at_walks_the_anchor_back_by_the_playhead(self):
        clock = track_clock.start_clock(213, 42.0, 1000.0, 1_756_338_000, 20.0)
        payload = track_clock.play_clock_payload(clock)
        self.assertEqual(payload, {
            "started_at": 1_756_338_000 - 42,
            "join_offset_secs": 42,
            "duration_secs": 213,
            "position_source": track_clock.POSITION_FROM_OFFSET,
        })

    def test_joining_at_the_top_reports_the_anchor_itself(self):
        clock = track_clock.start_clock(213, None, 1000.0, 1_756_338_000, 20.0)
        payload = track_clock.play_clock_payload(clock)
        self.assertEqual(payload["started_at"], 1_756_338_000)
        self.assertEqual(payload["join_offset_secs"], 0)
        self.assertEqual(payload["position_source"], track_clock.POSITION_ASSUMED)

    def test_no_duration_still_reports_a_start(self):
        clock = track_clock.start_clock(None, None, 1000.0, 1_756_338_000, 20.0)
        payload = track_clock.play_clock_payload(clock)
        self.assertIsNone(payload["duration_secs"])
        self.assertEqual(payload["started_at"], 1_756_338_000)

    def test_no_clock_is_none(self):
        self.assertIsNone(track_clock.play_clock_payload(None))


if __name__ == "__main__":
    unittest.main()
