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
        ipc_manager._last_recorded_title = ""

    def tearDown(self):
        play_history.DB_PATH = self._orig_db_path
        ipc_manager._last_recorded_title = ""
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


if __name__ == "__main__":
    unittest.main()
