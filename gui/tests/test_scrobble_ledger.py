"""The play clock as it lands in SQLite, and the scrobble ledger built on it.

The engine's arithmetic is tested in core/tests/test_track_clock.py. This file
covers the GUI half: the migration, the frame -> row path, and the Last.fm
eligibility rule a future scrobbler will consume verbatim."""
import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import ipc_manager  # noqa: E402
import play_history  # noqa: E402


class _TempDb(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db_path)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass


class PlayClockColumnsTest(_TempDb):
    def test_migration_adds_the_columns_to_an_existing_db(self):
        play_history.init_db(db_path=self.db_path)  # idempotent re-run
        cols = {r[1] for r in
                sqlite3.connect(self.db_path).execute("PRAGMA table_info(plays)")}
        self.assertIn("started_at", cols)
        self.assertIn("join_offset_secs", cols)

    def test_round_trip(self):
        play_history.record_play(
            "T", "A", "Al", None, db_path=self.db_path,
            duration_secs=213, started_at=1_756_338_000, join_offset_secs=42)
        row = play_history.recent_plays(db_path=self.db_path)[0]
        self.assertEqual(row["started_at"], 1_756_338_000)
        self.assertEqual(row["join_offset_secs"], 42)
        self.assertEqual(row["duration_secs"], 213)

    def test_pre_feature_rows_stay_null_not_zero(self):
        # NULL means "we don't know"; 0 would claim we joined at the top.
        play_history.record_play("T", "A", None, None, db_path=self.db_path)
        row = play_history.recent_plays(db_path=self.db_path)[0]
        self.assertIsNone(row["started_at"])
        self.assertIsNone(row["join_offset_secs"])


class PlayClockFrameTest(unittest.TestCase):
    """_play_clock_fields reads an optional, best-effort block off a frame that
    an older engine build may not send at all."""

    def test_reads_a_well_formed_block(self):
        self.assertEqual(
            ipc_manager._play_clock_fields(
                {"started_at": 1_756_338_000, "join_offset_secs": 42}),
            (1_756_338_000, 42))

    def test_absent_or_malformed_blocks_yield_nulls(self):
        for block in (None, {}, "nope", 5, {"started_at": None},
                      {"started_at": "soon", "join_offset_secs": []}):
            with self.subTest(block=block):
                self.assertEqual(ipc_manager._play_clock_fields(block), (None, None))

    def test_partial_block_keeps_what_it_can(self):
        self.assertEqual(
            ipc_manager._play_clock_fields({"started_at": 1_756_338_000}),
            (1_756_338_000, None))


class RecordIfNewCarriesTheClockTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db_path)
        self._orig_db_path = play_history.DB_PATH
        play_history.DB_PATH = self.db_path
        ipc_manager._last_recorded_key = None
        ipc_manager._last_play_id = None

    def tearDown(self):
        play_history.DB_PATH = self._orig_db_path
        ipc_manager._last_recorded_key = None
        ipc_manager._last_play_id = None
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def _feed(self, track, play_clock):
        asyncio.run(ipc_manager._record_if_new(track, play_clock))

    def test_clock_reaches_the_row(self):
        self._feed({"title": "T", "artist": "A", "duration_secs": 213},
                   {"started_at": 1_756_338_000, "join_offset_secs": 42})
        row = play_history.recent_plays(db_path=self.db_path)[0]
        self.assertEqual(row["started_at"], 1_756_338_000)
        self.assertEqual(row["join_offset_secs"], 42)

    def test_edition_evidence_reaches_the_row(self):
        # Gathered in the engine at enrichment time; reconciliation runs much
        # later and cannot re-derive it, so it has to travel with the play.
        self._feed({"title": "T", "artist": "A", "album": "SOUR (Deluxe)",
                    "album_exclusive": True}, None)
        row = play_history.recent_plays(db_path=self.db_path)[0]
        self.assertEqual(row["album_exclusive"], 1)

    def test_no_evidence_is_recorded_as_zero_not_null(self):
        self._feed({"title": "T", "artist": "A", "album": "SOUR"}, None)
        row = play_history.recent_plays(db_path=self.db_path)[0]
        self.assertEqual(row["album_exclusive"], 0)

    def test_a_frame_without_a_clock_still_records(self):
        # An engine on an older build sends no play_clock at all.
        self._feed({"title": "T", "artist": "A"}, None)
        rows = play_history.recent_plays(db_path=self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["started_at"])


class ScrobbleEligibilityTest(unittest.TestCase):
    """Last.fm's published rule: longer than 30s, and played for at least half
    its length or 4 minutes, whichever comes first."""

    def row(self, duration, listened, played_at=1000):
        return {"duration_secs": duration, "played_at": played_at,
                "ended_at": played_at + listened if listened is not None else None}

    def test_half_the_track_qualifies(self):
        self.assertTrue(play_history.scrobble_eligible(self.row(213, 107)))

    def test_just_under_half_does_not(self):
        self.assertFalse(play_history.scrobble_eligible(self.row(213, 106)))

    def test_four_minutes_qualifies_a_long_track_short_of_half(self):
        # 20 minutes long, heard 4 — half would be 10, but 4 min always counts.
        self.assertTrue(play_history.scrobble_eligible(self.row(1200, 240)))
        self.assertFalse(play_history.scrobble_eligible(self.row(1200, 239)))

    def test_tracks_at_or_under_thirty_seconds_never_qualify(self):
        self.assertFalse(play_history.scrobble_eligible(self.row(30, 30)))
        self.assertFalse(play_history.scrobble_eligible(self.row(25, 25)))

    def test_unknown_duration_never_qualifies(self):
        self.assertFalse(play_history.scrobble_eligible(self.row(None, 300)))

    def test_unclosed_play_is_never_estimated(self):
        self.assertFalse(play_history.scrobble_eligible(self.row(213, None)))
        self.assertIsNone(play_history.scrobble_listened_secs(self.row(213, None)))

    def test_negative_span_clamps_to_zero(self):
        row = {"duration_secs": 213, "played_at": 1000, "ended_at": 900}
        self.assertEqual(play_history.scrobble_listened_secs(row), 0)


class ScrobbleCandidatesTest(_TempDb):
    def _play(self, title, played_at, listened, duration, started_at=None):
        pid = play_history.record_play(
            title, "A", None, None, db_path=self.db_path,
            duration_secs=duration, started_at=started_at)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE plays SET played_at = ?, ended_at = ? WHERE id = ?",
                         (played_at, played_at + listened if listened is not None else None, pid))
        return pid

    def test_oldest_first_because_lastfm_wants_chronological_batches(self):
        self._play("second", 2000, 200, 213)
        self._play("first", 1000, 200, 213)
        rows = play_history.scrobble_candidates(db_path=self.db_path)
        self.assertEqual([r["title"] for r in rows], ["first", "second"])

    def test_timestamp_prefers_the_true_start(self):
        self._play("T", 1000, 200, 213, started_at=958)
        row = play_history.scrobble_candidates(db_path=self.db_path)[0]
        self.assertEqual(row["timestamp"], 958)

    def test_timestamp_falls_back_to_played_at(self):
        self._play("T", 1000, 200, 213)
        row = play_history.scrobble_candidates(db_path=self.db_path)[0]
        self.assertEqual(row["timestamp"], 1000)

    def test_unclosed_plays_are_excluded_entirely(self):
        self._play("open", 1000, None, 213)
        self.assertEqual(play_history.scrobble_candidates(db_path=self.db_path), [])

    def test_deleted_plays_are_excluded(self):
        pid = self._play("gone", 1000, 200, 213)
        play_history.delete_play(pid, db_path=self.db_path)
        self.assertEqual(play_history.scrobble_candidates(db_path=self.db_path), [])

    def test_since_filters_older_plays(self):
        self._play("old", 1000, 200, 213)
        self._play("new", 5000, 200, 213)
        rows = play_history.scrobble_candidates(since=2000, db_path=self.db_path)
        self.assertEqual([r["title"] for r in rows], ["new"])

    def test_ineligible_rows_are_returned_flagged_not_dropped(self):
        # The caller decides; the ledger reports.
        self._play("skipped", 1000, 10, 213)
        row = play_history.scrobble_candidates(db_path=self.db_path)[0]
        self.assertFalse(row["eligible"])
        self.assertEqual(row["listened_secs"], 10)


if __name__ == "__main__":
    unittest.main()
