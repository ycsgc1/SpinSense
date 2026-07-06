"""Aggregation tests for gui/stats.py. Seeded temp SQLite; timestamps are
built with datetime so they live in the server-local timezone the module
buckets by."""
import datetime
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import play_history  # noqa: E402
import stats  # noqa: E402


def ts(y, m, d, hh=12):
    return int(datetime.datetime(y, m, d, hh).timestamp())


NOW = ts(2026, 7, 15)  # tests pin "now" so current-period defaults are stable


class StatsTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db)

    def tearDown(self):
        try:
            os.remove(self.db)
        except OSError:
            pass

    def seed(self, title, artist, played_at, ended_at=None, genre=None,
             release_year=None, art_path=None, deleted_at=None):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO plays (title, artist, played_at, ended_at, genre,"
            " release_year, art_path, deleted_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title, artist, played_at, ended_at, genre, release_year,
             art_path, deleted_at))
        conn.commit()
        conn.close()


class PeriodBoundsTest(unittest.TestCase):
    def test_month_bounds(self):
        start, end = stats._period_bounds("month", 2026, 7, now=NOW)
        self.assertEqual(start, ts(2026, 7, 1, 0))
        self.assertEqual(end, ts(2026, 8, 1, 0))

    def test_december_rolls_year(self):
        start, end = stats._period_bounds("month", 2025, 12, now=NOW)
        self.assertEqual(end, ts(2026, 1, 1, 0))

    def test_year_bounds(self):
        start, end = stats._period_bounds("year", 2025, None, now=NOW)
        self.assertEqual(start, ts(2025, 1, 1, 0))
        self.assertEqual(end, ts(2026, 1, 1, 0))

    def test_defaults_to_current(self):
        start, end = stats._period_bounds("month", None, None, now=NOW)
        self.assertEqual(start, ts(2026, 7, 1, 0))

    def test_all_starts_at_zero(self):
        start, end = stats._period_bounds("all", None, None, now=NOW)
        self.assertEqual(start, 0)
        self.assertGreater(end, NOW)

    def test_invalid_period_raises(self):
        with self.assertRaises(ValueError):
            stats._period_bounds("week", None, None, now=NOW)
        with self.assertRaises(ValueError):
            stats._period_bounds("month", 2026, 13, now=NOW)


class TotalsTest(StatsTestBase):
    def test_counts_and_uniques(self):
        self.seed("One", "A", ts(2026, 7, 1))
        self.seed("One", "A", ts(2026, 7, 2))
        self.seed("Two", "B", ts(2026, 7, 3))
        self.seed("Old", "C", ts(2025, 1, 1))          # outside period
        self.seed("Gone", "D", ts(2026, 7, 4), deleted_at=1)  # soft-deleted
        out = stats.compute_stats("month", 2026, 7, db_path=self.db, now=NOW)
        self.assertEqual(out["totals"]["plays"], 3)
        self.assertEqual(out["totals"]["unique_tracks"], 2)
        self.assertEqual(out["totals"]["unique_artists"], 2)

    def test_listening_time_real_rows_only_and_capped(self):
        t = ts(2026, 7, 1)
        self.seed("A", "A", t, ended_at=t + 200)        # 200s tracked
        self.seed("B", "B", t, ended_at=t + 9999)       # capped to 2400
        self.seed("C", "C", t, ended_at=t - 50)         # clock skew -> 0
        self.seed("D", "D", t)                          # NULL: excluded
        out = stats.compute_stats("month", 2026, 7, db_path=self.db, now=NOW)
        self.assertEqual(out["totals"]["listening_secs"], 200 + 2400 + 0)
        self.assertEqual(out["totals"]["listening_tracked_plays"], 3)
        self.assertEqual(out["totals"]["plays"], 4)


class TopListsTest(StatsTestBase):
    def test_top_artists_ranked_with_latest_art(self):
        t = ts(2026, 7, 1)
        self.seed("S1", "Beatles", t, art_path="art/1.jpg")
        self.seed("S2", "Beatles", t + 100, art_path="art/2.jpg")
        self.seed("S3", "Doors", t)
        out = stats.compute_stats("month", 2026, 7, db_path=self.db, now=NOW)
        top = out["top_artists"]
        self.assertEqual(top[0]["artist"], "Beatles")
        self.assertEqual(top[0]["plays"], 2)
        self.assertEqual(top[0]["art_path"], "art/2.jpg")  # most recent art
        self.assertEqual(top[1]["artist"], "Doors")

    def test_top_tracks_keyed_by_title_and_artist(self):
        t = ts(2026, 7, 1)
        self.seed("Same", "A", t)
        self.seed("Same", "A", t + 1)
        self.seed("Same", "B", t)   # same title, different artist: separate
        out = stats.compute_stats("month", 2026, 7, db_path=self.db, now=NOW)
        self.assertEqual(out["top_tracks"][0]["plays"], 2)
        self.assertEqual(len(out["top_tracks"]), 2)

    def test_top_lists_capped_at_five(self):
        t = ts(2026, 7, 1)
        for i in range(7):
            self.seed(f"T{i}", f"Artist{i}", t)
        out = stats.compute_stats("month", 2026, 7, db_path=self.db, now=NOW)
        self.assertEqual(len(out["top_artists"]), 5)


class BucketsTest(StatsTestBase):
    def test_month_period_day_buckets_zero_filled(self):
        self.seed("A", "A", ts(2026, 7, 1))
        self.seed("B", "B", ts(2026, 7, 1, 20))
        self.seed("C", "C", ts(2026, 7, 3))
        out = stats.compute_stats("month", 2026, 7, db_path=self.db, now=NOW)
        b = out["plays_over_time"]
        self.assertEqual(b["bucket"], "day")
        self.assertEqual(b["buckets"][0], {"key": "2026-07-01", "plays": 2})
        self.assertEqual(b["buckets"][1], {"key": "2026-07-02", "plays": 0})
        self.assertEqual(b["buckets"][2], {"key": "2026-07-03", "plays": 1})
        # current month clamps at "now" (July 15), not month end
        self.assertEqual(b["buckets"][-1]["key"], "2026-07-15")

    def test_year_period_month_buckets(self):
        self.seed("A", "A", ts(2025, 2, 10))
        out = stats.compute_stats("year", 2025, None, db_path=self.db, now=NOW)
        b = out["plays_over_time"]
        self.assertEqual(b["bucket"], "month")
        self.assertEqual(len(b["buckets"]), 12)  # past year: full 12 months
        self.assertEqual(b["buckets"][1], {"key": "2025-02", "plays": 1})

    def test_all_period_starts_at_first_play(self):
        self.seed("A", "A", ts(2026, 5, 10))
        out = stats.compute_stats("all", None, None, db_path=self.db, now=NOW)
        b = out["plays_over_time"]
        self.assertEqual(b["buckets"][0]["key"], "2026-05")
        self.assertEqual(b["buckets"][-1]["key"], "2026-07")

    def test_all_period_no_plays_empty(self):
        out = stats.compute_stats("all", None, None, db_path=self.db, now=NOW)
        self.assertEqual(out["plays_over_time"]["buckets"], [])


class GenresDecadesTest(StatsTestBase):
    def test_genre_coverage_and_top(self):
        t = ts(2026, 7, 1)
        self.seed("A", "A", t, genre="Rock")
        self.seed("B", "B", t, genre="Rock")
        self.seed("C", "C", t, genre="Jazz")
        self.seed("D", "D", t)  # no genre
        out = stats.compute_stats("month", 2026, 7, db_path=self.db, now=NOW)
        self.assertEqual(out["genres"]["covered"], 3)
        self.assertEqual(out["genres"]["total"], 4)
        self.assertEqual(out["genres"]["top"][0], {"genre": "Rock", "plays": 2})

    def test_decades_from_release_year(self):
        t = ts(2026, 7, 1)
        self.seed("A", "A", t, release_year=1973)
        self.seed("B", "B", t, release_year=1979)
        self.seed("C", "C", t, release_year=1981)
        out = stats.compute_stats("month", 2026, 7, db_path=self.db, now=NOW)
        self.assertEqual(out["decades"]["buckets"][0], {"decade": 1970, "plays": 2})
        self.assertEqual(out["decades"]["buckets"][1], {"decade": 1980, "plays": 1})
