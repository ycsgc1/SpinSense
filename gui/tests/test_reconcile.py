"""Tests for album/edition reconciliation. Pure-function table tests here;
run/rewrite DB tests are added by a later task in this same file."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import play_history  # noqa: E402  (used by DB tests in a later task)
import reconcile  # noqa: E402


class BaseTitleTest(unittest.TestCase):
    def test_equivalences(self):
        base = reconcile.base_title("Abbey Road")
        for variant in [
            "Abbey Road (Super Deluxe Edition)",
            "Abbey Road - 50th Anniversary",
            "Abbey Road (Deluxe Edition) [2019 Remaster]",
            "Abbey Road [2014]",
            "Abbey Road (Bonus Track Version)",
            "abbey  road",
        ]:
            self.assertEqual(reconcile.base_title(variant), base, variant)

    def test_non_merges(self):
        studio = reconcile.base_title("At Folsom Prison")
        self.assertNotEqual(reconcile.base_title("At Folsom Prison (Live)"), studio)
        self.assertNotEqual(reconcile.base_title("Nevermind (Acoustic)"),
                            reconcile.base_title("Nevermind"))

    def test_taylors_version_never_merges(self):
        base = reconcile.base_title("1989")
        self.assertNotEqual(reconcile.base_title("1989 (Taylor's Version)"), base)
        # curly apostrophe form (U+2019, as real music metadata emits)
        self.assertNotEqual(reconcile.base_title("1989 (Taylor’s Version)"), base)
        # but a deluxe qualifier ON TOP of Taylor's Version still strips
        self.assertEqual(
            reconcile.base_title("1989 (Taylor's Version) [Deluxe]"),
            reconcile.base_title("1989 (Taylor's Version)"),
        )

    def test_dash_only_strips_qualifiers(self):
        self.assertEqual(reconcile.base_title("Blood - Sugar - Deluxe Edition"),
                         reconcile.base_title("Blood - Sugar"))
        # a non-qualifier dash segment stays
        self.assertNotEqual(reconcile.base_title("First - Last"),
                            reconcile.base_title("First"))
        # hyphenated words (no spaces) never strip
        self.assertEqual(reconcile.base_title("X-Ray"), "x-ray")

    def test_empty_and_none_safe(self):
        self.assertEqual(reconcile.base_title(""), "")
        self.assertEqual(reconcile.base_title(None), "")


class PickWinnerTest(unittest.TestCase):
    def test_longest_wins(self):
        albums = [("Abbey Road", 100), ("Abbey Road (Super Deluxe Edition)", 50)]
        self.assertEqual(reconcile.pick_winner(albums),
                         "Abbey Road (Super Deluxe Edition)")

    def test_tie_breaks_most_recent(self):
        albums = [("Album (Deluxe A)", 100), ("Album (Deluxe B)", 200)]
        self.assertEqual(reconcile.pick_winner(albums), "Album (Deluxe B)")


import asyncio  # noqa: E402
import sqlite3  # noqa: E402

import ipc_manager  # noqa: E402


class ReconcileDbBase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db)

    def tearDown(self):
        try:
            os.remove(self.db)
        except OSError:
            pass

    def seed(self, artist, album, played_at, locked=None, deleted=None):
        conn = sqlite3.connect(self.db)
        cur = conn.execute(
            "INSERT INTO plays (title, artist, album, played_at, album_locked,"
            " deleted_at) VALUES (?, ?, ?, ?, ?, ?)",
            (f"t{played_at}", artist, album, played_at, locked, deleted))
        conn.commit()
        pid = cur.lastrowid
        conn.close()
        return pid

    def albums(self):
        conn = sqlite3.connect(self.db)
        rows = conn.execute("SELECT album FROM plays ORDER BY id").fetchall()
        conn.close()
        return [r[0] for r in rows]


class FindRunTest(ReconcileDbBase):
    def test_run_bounded_by_gap_and_artist(self):
        self.seed("A", "X", 1000)
        p2 = self.seed("A", "X", 2000)
        self.seed("A", "X", 2000 + reconcile.SESSION_GAP_SECS + 1)  # gap: new run
        self.seed("B", "Y", 2500)  # other artist: not in run
        run = reconcile.find_run(p2, db_path=self.db)
        self.assertEqual([r["played_at"] for r in run], [1000, 2000])

    def test_missing_play_empty(self):
        self.assertEqual(reconcile.find_run(999, db_path=self.db), [])


class ReconcileAlbumTest(ReconcileDbBase):
    def test_rewrites_backwards(self):
        self.seed("A", "Abbey Road", 1000)
        self.seed("A", "Abbey Road", 1100)
        p3 = self.seed("A", "Abbey Road (Super Deluxe Edition)", 1200)
        changed = reconcile.reconcile_album(p3, db_path=self.db)
        self.assertEqual(changed, 2)
        self.assertEqual(set(self.albums()), {"Abbey Road (Super Deluxe Edition)"})

    def test_rewrites_forwards(self):
        self.seed("A", "Abbey Road (Super Deluxe Edition)", 1000)
        p2 = self.seed("A", "Abbey Road", 1100)
        reconcile.reconcile_album(p2, db_path=self.db)
        self.assertEqual(set(self.albums()), {"Abbey Road (Super Deluxe Edition)"})

    def test_locked_rows_neither_vote_nor_rewrite(self):
        p1 = self.seed("A", "Abbey Road (My Special Long Locked Edition)", 1000,
                       locked=1)
        p2 = self.seed("A", "Abbey Road", 1100)
        reconcile.reconcile_album(p2, db_path=self.db)
        albums = self.albums()
        self.assertEqual(albums[0], "Abbey Road (My Special Long Locked Edition)")
        self.assertEqual(albums[1], "Abbey Road")  # locked row didn't drag it

    def test_locked_trigger_is_noop(self):
        self.seed("A", "Abbey Road", 1000)
        p2 = self.seed("A", "Abbey Road (Deluxe Edition)", 1100, locked=1)
        self.assertEqual(reconcile.reconcile_album(p2, db_path=self.db), 0)

    def test_different_base_titles_untouched(self):
        self.seed("A", "Revolver", 1000)
        p2 = self.seed("A", "Abbey Road (Deluxe Edition)", 1100)
        reconcile.reconcile_album(p2, db_path=self.db)
        self.assertIn("Revolver", self.albums())

    def test_gap_splits_runs(self):
        self.seed("A", "Abbey Road", 1000)
        p2 = self.seed("A", "Abbey Road (Deluxe Edition)",
                       1000 + reconcile.SESSION_GAP_SECS + 100)
        reconcile.reconcile_album(p2, db_path=self.db)
        self.assertIn("Abbey Road", self.albums())  # earlier run untouched

    def test_soft_deleted_ignored(self):
        self.seed("A", "Abbey Road", 1000, deleted=1)
        p2 = self.seed("A", "Abbey Road (Deluxe Edition)", 1100)
        reconcile.reconcile_album(p2, db_path=self.db)
        self.assertIn("Abbey Road", self.albums())

    def test_null_album_noop(self):
        p1 = self.seed("A", None, 1000)
        self.assertEqual(reconcile.reconcile_album(p1, db_path=self.db), 0)


class ApplyToRunTest(ReconcileDbBase):
    def test_overrides_locks_and_locks_all(self):
        p1 = self.seed("A", "Wrong", 1000, locked=1)
        p2 = self.seed("A", "Also Wrong", 1100)
        ids = reconcile.apply_album_to_run(p2, "Right (Deluxe)", db_path=self.db)
        self.assertEqual(sorted(ids), [p1, p2])
        conn = sqlite3.connect(self.db)
        rows = conn.execute("SELECT album, album_locked FROM plays").fetchall()
        conn.close()
        self.assertEqual(set(rows), {("Right (Deluxe)", 1)})

    def test_missing_play_empty(self):
        self.assertEqual(reconcile.apply_album_to_run(999, "X", db_path=self.db), [])


class IpcReconcileTriggerTest(ReconcileDbBase):
    def setUp(self):
        super().setUp()
        self._orig_db = play_history.DB_PATH
        play_history.DB_PATH = self.db
        ipc_manager._last_recorded_key = None
        ipc_manager._last_play_id = None

    def tearDown(self):
        play_history.DB_PATH = self._orig_db
        ipc_manager._last_recorded_key = None
        ipc_manager._last_play_id = None
        super().tearDown()

    def test_new_play_triggers_reconcile(self):
        asyncio.run(ipc_manager._record_if_new(
            {"title": "One", "artist": "A", "album": "Abbey Road"}))
        asyncio.run(ipc_manager._record_if_new(
            {"title": "Two", "artist": "A", "album": "Abbey Road (Deluxe Edition)"}))
        self.assertEqual(set(self.albums()), {"Abbey Road (Deluxe Edition)"})

    def test_reconcile_failure_does_not_block_recording(self):
        orig = reconcile.reconcile_album
        reconcile.reconcile_album = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            asyncio.run(ipc_manager._record_if_new(
                {"title": "One", "artist": "A", "album": "X"}))
        finally:
            reconcile.reconcile_album = orig
        self.assertEqual(self.albums(), ["X"])
