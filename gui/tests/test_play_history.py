"""Lightweight checks for the play_history module and the ipc_manager dedupe
hook. No network, no real audio — just temp SQLite files."""
import asyncio
import os
import sys
import tempfile
import time
import unittest

# Tests live under gui/tests/ but the production code imports as `play_history`,
# `ipc_manager`, etc. (no `gui.` prefix). Make sure gui/ is on sys.path.
HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import play_history  # noqa: E402
import ipc_manager  # noqa: E402


class PlayHistoryRoundTripTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db_path)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_record_and_read_back(self):
        play_history.record_play("Midnight City", "M83", "Hurry Up", "http://a", db_path=self.db_path)
        rows = play_history.recent_plays(limit=10, db_path=self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Midnight City")
        self.assertEqual(rows[0]["artist"], "M83")
        self.assertEqual(rows[0]["album"], "Hurry Up")
        self.assertIsNone(rows[0]["art_path"])
        self.assertIsInstance(rows[0]["played_at"], int)

    def test_recent_plays_orders_newest_first(self):
        play_history.record_play("First", "A", None, None, db_path=self.db_path)
        # Force a 1-second gap so played_at values differ even on fast hardware.
        time.sleep(1.1)
        play_history.record_play("Second", "B", None, None, db_path=self.db_path)
        rows = play_history.recent_plays(db_path=self.db_path)
        self.assertEqual([r["title"] for r in rows], ["Second", "First"])

    def test_set_art_path_updates_row(self):
        play_id = play_history.record_play("X", "Y", None, None, db_path=self.db_path)
        play_history.set_art_path(play_id, "art/X.jpg", db_path=self.db_path)
        rows = play_history.recent_plays(db_path=self.db_path)
        self.assertEqual(rows[0]["art_path"], "art/X.jpg")

    def test_recent_plays_limit_clamps(self):
        for i in range(3):
            play_history.record_play(f"t{i}", "a", None, None, db_path=self.db_path)
        self.assertEqual(len(play_history.recent_plays(limit=2, db_path=self.db_path)), 2)
        # negative/zero limits get clamped up to 1
        self.assertEqual(len(play_history.recent_plays(limit=0, db_path=self.db_path)), 1)

    def test_offset_paginates(self):
        # Use distinct played_at values so order is deterministic.
        for i, title in enumerate(["A", "B", "C", "D"]):
            play_history.record_play(title, "x", None, None, db_path=self.db_path)
            time.sleep(1.05)
        # Newest first: D, C, B, A.
        first_page = play_history.recent_plays(limit=2, offset=0, db_path=self.db_path)
        self.assertEqual([r["title"] for r in first_page], ["D", "C"])
        second_page = play_history.recent_plays(limit=2, offset=2, db_path=self.db_path)
        self.assertEqual([r["title"] for r in second_page], ["B", "A"])

    def test_offset_past_end_returns_empty(self):
        play_history.record_play("only", "x", None, None, db_path=self.db_path)
        self.assertEqual(play_history.recent_plays(limit=10, offset=5, db_path=self.db_path), [])

    def test_count_plays(self):
        self.assertEqual(play_history.count_plays(db_path=self.db_path), 0)
        for i in range(3):
            play_history.record_play(f"t{i}", "a", None, None, db_path=self.db_path)
        self.assertEqual(play_history.count_plays(db_path=self.db_path), 3)


class IPCDedupeTest(unittest.TestCase):
    """The dedupe state lives in ipc_manager (a module-level variable).
    Stand up a temp DB, point play_history at it, reset the dedupe state, then
    feed a sequence of frames through _record_if_new and assert the resulting
    rows."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db_path)
        # Redirect production DB_PATH at the play_history default so
        # _record_if_new (which doesn't pass db_path) writes into our temp DB.
        self._orig_db_path = play_history.DB_PATH
        play_history.DB_PATH = self.db_path
        ipc_manager._last_recorded_key = None

    def tearDown(self):
        play_history.DB_PATH = self._orig_db_path
        ipc_manager._last_recorded_key = None
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def _feed(self, *titles):
        async def run():
            for t in titles:
                await ipc_manager._record_if_new({
                    "title": t, "artist": "A", "album": None, "art_url": "",
                })
        asyncio.run(run())

    def test_identical_titles_record_once(self):
        self._feed("Same Track", "Same Track", "Same Track")
        rows = play_history.recent_plays(db_path=self.db_path)
        self.assertEqual(len(rows), 1)

    def test_different_titles_record_each(self):
        self._feed("Track A", "Track B")
        titles = [r["title"] for r in play_history.recent_plays(db_path=self.db_path)]
        self.assertEqual(titles, ["Track B", "Track A"])

    def test_empty_title_resets_dedupe(self):
        # Play A → silence → A again: should be two rows.
        self._feed("Track A", "", "Track A")
        titles = [r["title"] for r in play_history.recent_plays(db_path=self.db_path)]
        self.assertEqual(titles, ["Track A", "Track A"])

    def test_empty_title_alone_records_nothing(self):
        self._feed("", "", "")
        self.assertEqual(play_history.recent_plays(db_path=self.db_path), [])

    def test_same_title_different_artist_records_each(self):
        # Two distinct songs that happen to share a title must not collapse into
        # one row — dedupe is keyed on (artist, title), not title alone.
        async def run():
            await ipc_manager._record_if_new(
                {"title": "Intro", "artist": "Band One", "album": None, "art_url": ""}
            )
            await ipc_manager._record_if_new(
                {"title": "Intro", "artist": "Band Two", "album": None, "art_url": ""}
            )
        asyncio.run(run())
        rows = play_history.recent_plays(db_path=self.db_path)
        self.assertEqual(len(rows), 2)


class TestEnrichmentColumns(unittest.TestCase):
    def setUp(self):
        import tempfile, os
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        import play_history
        play_history.init_db(self.db)

    def test_init_db_is_idempotent_with_new_columns(self):
        import play_history
        play_history.init_db(self.db)  # run twice, must not error
        import sqlite3
        cols = {r[1] for r in sqlite3.connect(self.db).execute("PRAGMA table_info(plays)")}
        self.assertTrue({"isrc", "genre", "release_year"} <= cols)

    def test_record_play_stores_enrichment(self):
        import play_history
        pid = play_history.record_play(
            "Title", "Artist", "Album", "http://art",
            isrc="USRC12345678", genre="Rock", release_year=1977,
            db_path=self.db,
        )
        rows = play_history.recent_plays(10, 0, db_path=self.db)
        self.assertEqual(rows[0]["id"], pid)
        self.assertEqual(rows[0]["isrc"], "USRC12345678")
        self.assertEqual(rows[0]["genre"], "Rock")
        self.assertEqual(rows[0]["release_year"], 1977)

    def test_record_play_enrichment_optional(self):
        import play_history
        pid = play_history.record_play("T", "A", None, None, db_path=self.db)
        rows = play_history.recent_plays(10, 0, db_path=self.db)
        self.assertIsNone(rows[0]["isrc"])
        self.assertIsNone(rows[0]["genre"])
        self.assertIsNone(rows[0]["release_year"])


class SoftDeleteTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db_path)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_delete_hides_from_reads(self):
        pid = play_history.record_play("Gone", "A", None, None, db_path=self.db_path)
        self.assertTrue(play_history.delete_play(pid, db_path=self.db_path))
        self.assertEqual(play_history.recent_plays(db_path=self.db_path), [])
        self.assertEqual(play_history.count_plays(db_path=self.db_path), 0)

    def test_delete_unknown_returns_false(self):
        self.assertFalse(play_history.delete_play(999, db_path=self.db_path))

    def test_delete_is_idempotent(self):
        pid = play_history.record_play("Once", "A", None, None, db_path=self.db_path)
        self.assertTrue(play_history.delete_play(pid, db_path=self.db_path))
        self.assertFalse(play_history.delete_play(pid, db_path=self.db_path))

    def test_restore_brings_it_back(self):
        pid = play_history.record_play("Back", "A", None, None, db_path=self.db_path)
        play_history.delete_play(pid, db_path=self.db_path)
        self.assertTrue(play_history.restore_play(pid, db_path=self.db_path))
        rows = play_history.recent_plays(db_path=self.db_path)
        self.assertEqual([r["title"] for r in rows], ["Back"])

    def test_restore_unknown_or_live_returns_false(self):
        pid = play_history.record_play("Live", "A", None, None, db_path=self.db_path)
        self.assertFalse(play_history.restore_play(pid, db_path=self.db_path))  # not deleted
        self.assertFalse(play_history.restore_play(999, db_path=self.db_path))

    def test_migration_adds_column_on_existing_db(self):
        play_history.init_db(db_path=self.db_path)  # run twice, must not error
        import sqlite3
        cols = {r[1] for r in sqlite3.connect(self.db_path).execute("PRAGMA table_info(plays)")}
        self.assertIn("deleted_at", cols)


class PurgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "t.db")
        os.makedirs(os.path.join(self.tmp, "art"), exist_ok=True)
        play_history.init_db(db_path=self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _age_deleted(self, pid, seconds_ago):
        import sqlite3
        with sqlite3.connect(self.db_path) as c:
            c.execute("UPDATE plays SET deleted_at = ? WHERE id = ?",
                      (int(time.time()) - seconds_ago, pid))

    def _art(self, name):
        p = os.path.join(self.tmp, "art", name)
        with open(p, "wb") as f:
            f.write(b"x")
        return p, f"art/{name}"

    def test_purges_past_grace_and_unlinks_art(self):
        path, rel = self._art("1.jpg")
        pid = play_history.record_play("Old", "A", None, None, db_path=self.db_path)
        play_history.set_art_path(pid, rel, db_path=self.db_path)
        play_history.delete_play(pid, db_path=self.db_path)
        self._age_deleted(pid, 300)
        n = play_history.purge_deleted(grace_seconds=120, data_dir=self.tmp, db_path=self.db_path)
        self.assertEqual(n, 1)
        self.assertFalse(os.path.exists(path))

    def test_keeps_rows_within_grace(self):
        pid = play_history.record_play("Recent", "A", None, None, db_path=self.db_path)
        play_history.delete_play(pid, db_path=self.db_path)  # deleted_at = now
        n = play_history.purge_deleted(grace_seconds=120, data_dir=self.tmp, db_path=self.db_path)
        self.assertEqual(n, 0)

    def test_keeps_art_still_referenced_by_live_row(self):
        path, rel = self._art("shared.jpg")
        live = play_history.record_play("Live", "A", None, None, db_path=self.db_path)
        dead = play_history.record_play("Dead", "A", None, None, db_path=self.db_path)
        play_history.set_art_path(live, rel, db_path=self.db_path)
        play_history.set_art_path(dead, rel, db_path=self.db_path)
        play_history.delete_play(dead, db_path=self.db_path)
        self._age_deleted(dead, 300)
        play_history.purge_deleted(grace_seconds=120, data_dir=self.tmp, db_path=self.db_path)
        self.assertTrue(os.path.exists(path))  # still used by the live row

    def test_tolerates_missing_art_file(self):
        pid = play_history.record_play("NoFile", "A", None, None, db_path=self.db_path)
        play_history.set_art_path(pid, "art/missing.jpg", db_path=self.db_path)
        play_history.delete_play(pid, db_path=self.db_path)
        self._age_deleted(pid, 300)
        n = play_history.purge_deleted(grace_seconds=120, data_dir=self.tmp, db_path=self.db_path)
        self.assertEqual(n, 1)  # no exception


class EndedAtAndDurationTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db_path)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def _row(self, pid):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM plays WHERE id = ?", (pid,)).fetchone()
        conn.close()
        return row

    def test_new_columns_default_null(self):
        pid = play_history.record_play("T", "A", None, None, db_path=self.db_path)
        row = self._row(pid)
        self.assertIsNone(row["ended_at"])
        self.assertIsNone(row["duration_secs"])

    def test_record_play_stores_duration(self):
        pid = play_history.record_play("T", "A", None, None,
                                       db_path=self.db_path, duration_secs=245)
        self.assertEqual(self._row(pid)["duration_secs"], 245)

    def test_set_ended_at_first_write_wins(self):
        pid = play_history.record_play("T", "A", None, None, db_path=self.db_path)
        play_history.set_ended_at(pid, 1000, db_path=self.db_path)
        self.assertEqual(self._row(pid)["ended_at"], 1000)
        play_history.set_ended_at(pid, 2000, db_path=self.db_path)  # idempotent: no overwrite
        self.assertEqual(self._row(pid)["ended_at"], 1000)

    def test_migration_adds_columns_to_existing_db(self):
        # init_db twice must not fail, and columns exist exactly once.
        play_history.init_db(db_path=self.db_path)
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(plays)")]
        conn.close()
        self.assertEqual(cols.count("ended_at"), 1)
        self.assertEqual(cols.count("duration_secs"), 1)


if __name__ == "__main__":
    unittest.main()
