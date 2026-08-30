"""Two ways a play gets an album without asking iTunes again.

Both come from one observation: a vinyl collection is small and repetitive. Most
people own one or two pressings of any given record, and a side is one record —
so the listener's own history and the rest of the current run are better oracles
than a relevance-ranked search.

The case that prompted this: a full side of OK ORCHESTRA came out right except
its *first* track, "OK Overture", which iTunes' search returns nothing at all
for. The album was known by track two, but nothing looked back.
"""
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
import reconcile  # noqa: E402


class _Db(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db)
        self._orig_db = play_history.DB_PATH
        play_history.DB_PATH = self.db

    def tearDown(self):
        play_history.DB_PATH = self._orig_db
        try:
            os.remove(self.db)
        except OSError:
            pass

    def seed(self, title, album, played_at, artist="AJR", locked=None):
        conn = sqlite3.connect(self.db)
        cur = conn.execute(
            "INSERT INTO plays (title, artist, album, played_at, album_locked)"
            " VALUES (?, ?, ?, ?, ?)",
            (title, artist, album, played_at, locked))
        conn.commit()
        pid = cur.lastrowid
        conn.close()
        return pid

    def albums(self):
        conn = sqlite3.connect(self.db)
        rows = [r[0] for r in conn.execute(
            "SELECT album FROM plays ORDER BY played_at, id")]
        conn.close()
        return rows


class AlbumFromHistoryTest(_Db):
    def test_a_track_we_have_filed_before_is_recalled(self):
        self.seed("OK Overture", "OK ORCHESTRA", 1000)
        self.assertEqual(
            play_history.album_for_track("AJR", "OK Overture", db_path=self.db),
            "OK ORCHESTRA")

    def test_the_most_recent_filing_wins(self):
        self.seed("Bang!", "Old Guess", 1000)
        self.seed("Bang!", "OK ORCHESTRA", 2000)
        self.assertEqual(
            play_history.album_for_track("AJR", "Bang!", db_path=self.db),
            "OK ORCHESTRA")

    def test_a_hand_set_album_outranks_a_newer_guess(self):
        # album_locked is the listener telling us; that beats inference,
        # however recent the inference.
        self.seed("Bang!", "OK ORCHESTRA", 1000, locked=1)
        self.seed("Bang!", "Some Compilation", 2000)
        self.assertEqual(
            play_history.album_for_track("AJR", "Bang!", db_path=self.db),
            "OK ORCHESTRA")

    def test_the_collection_speaks_for_itself(self):
        # Only ever played the deluxe? Then the deluxe is what they own.
        self.seed("Espresso", "Short n' Sweet (Deluxe)", 1000,
                  artist="Sabrina Carpenter")
        self.assertEqual(
            play_history.album_for_track("Sabrina Carpenter", "Espresso",
                                         db_path=self.db),
            "Short n' Sweet (Deluxe)")

    def test_unknown_and_missing_albums_are_not_recalled(self):
        self.seed("Ghost", "Unknown Album", 1000)
        self.seed("Ghost", None, 1100)
        self.assertIsNone(
            play_history.album_for_track("AJR", "Ghost", db_path=self.db))

    def test_a_deleted_play_does_not_speak(self):
        pid = self.seed("Bang!", "Wrong Album", 1000)
        play_history.delete_play(pid, db_path=self.db)
        self.assertIsNone(
            play_history.album_for_track("AJR", "Bang!", db_path=self.db))

    def test_another_artists_song_of_the_same_name_is_not_confused(self):
        self.seed("Joe", "OK ORCHESTRA", 1000, artist="AJR")
        self.assertIsNone(
            play_history.album_for_track("Someone Else", "Joe", db_path=self.db))

    def test_missing_inputs_are_safe(self):
        for artist, title in (("", "x"), ("x", ""), (None, None)):
            self.assertIsNone(
                play_history.album_for_track(artist, title, db_path=self.db))


class RecordUsesHistoryTest(_Db):
    def setUp(self):
        super().setUp()
        ipc_manager._last_recorded_key = None
        ipc_manager._last_play_id = None

    def tearDown(self):
        ipc_manager._last_recorded_key = None
        ipc_manager._last_play_id = None
        super().tearDown()

    def feed(self, title, album):
        asyncio.run(ipc_manager._record_if_new(
            {"title": title, "artist": "AJR", "album": album}))

    def test_an_unresolved_album_is_filled_from_history(self):
        self.seed("OK Overture", "OK ORCHESTRA", 1000)
        self.feed("OK Overture", None)
        self.assertEqual(self.albums()[-1], "OK ORCHESTRA")

    def test_unknown_album_is_treated_as_unresolved(self):
        self.seed("OK Overture", "OK ORCHESTRA", 1000)
        self.feed("OK Overture", "Unknown Album")
        self.assertEqual(self.albums()[-1], "OK ORCHESTRA")

    def test_a_resolved_album_is_left_alone(self):
        # Today's lookup outranks memory: they may be playing the other pressing.
        self.seed("Bang!", "Old Guess", 1000)
        self.feed("Bang!", "OK ORCHESTRA")
        self.assertEqual(self.albums()[-1], "OK ORCHESTRA")

    def test_nothing_remembered_stays_unresolved(self):
        self.feed("Brand New Song", None)
        self.assertIsNone(self.albums()[-1])


class RunBackfillTest(_Db):
    def test_the_first_track_of_a_side_adopts_the_record(self):
        # Exactly the reported case, in order.
        first = self.seed("OK Overture", None, 1000)
        self.seed("Bummerland", "OK ORCHESTRA", 1200)
        last = self.seed("Joe", "OK ORCHESTRA", 1400)
        reconcile.reconcile_album(last, db_path=self.db)
        self.assertEqual(
            play_history.get_play(first, db_path=self.db)["album"], "OK ORCHESTRA")

    def test_unknown_album_strings_are_adopted_too(self):
        first = self.seed("OK Overture", "Unknown Album", 1000)
        last = self.seed("Bummerland", "OK ORCHESTRA", 1200)
        reconcile.reconcile_album(last, db_path=self.db)
        self.assertEqual(
            play_history.get_play(first, db_path=self.db)["album"], "OK ORCHESTRA")

    def test_a_run_spanning_two_records_adopts_nothing(self):
        # Not unanimous, so there is no single record to attribute it to —
        # guessing here would let one album bleed into another.
        first = self.seed("Mystery", None, 1000)
        self.seed("A Track", "Album One", 1200)
        last = self.seed("Another", "Album Two", 1400)
        reconcile.reconcile_album(last, db_path=self.db)
        self.assertIsNone(play_history.get_play(first, db_path=self.db)["album"])

    def test_editions_still_unify_after_a_backfill(self):
        # The adopted row must then take part in normal reconciliation.
        first = self.seed("OK Overture", None, 1000)
        self.seed("Bummerland", "OK ORCHESTRA", 1200)
        last = self.seed("Joe", "OK ORCHESTRA (Deluxe)", 1400)
        reconcile.reconcile_album(last, db_path=self.db)
        self.assertEqual(set(self.albums()), {"OK ORCHESTRA"})
        self.assertEqual(
            play_history.get_play(first, db_path=self.db)["album"], "OK ORCHESTRA")

    def test_a_locked_row_is_never_overwritten(self):
        first = self.seed("Deliberate", None, 1000, locked=1)
        last = self.seed("Bummerland", "OK ORCHESTRA", 1200)
        reconcile.reconcile_album(last, db_path=self.db)
        self.assertIsNone(play_history.get_play(first, db_path=self.db)["album"])

    def test_a_run_with_no_known_album_changes_nothing(self):
        first = self.seed("One", None, 1000)
        last = self.seed("Two", None, 1200)
        self.assertEqual(reconcile.reconcile_album(last, db_path=self.db), 0)
        self.assertIsNone(play_history.get_play(first, db_path=self.db)["album"])


if __name__ == "__main__":
    unittest.main()
