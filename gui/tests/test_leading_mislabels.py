"""The opening of a side, before the record is known.

A side of *1989 (Taylor's Version)* opens with "Welcome To New York" — reported
with no qualifier, because that is how the recogniser had it. A track by that
name genuinely lives on *1989*, so that is where it resolved, and nothing later
could correct it: `base_title` deliberately keeps a re-recording separate from
what it re-records, so the two never met in the same reconciliation group.

That separation is right. Two records are two records. What it misses is the
one case where it is not two records — a side whose first play or two landed on
the original because nothing yet said otherwise.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import play_history  # noqa: E402
import reconcile  # noqa: E402

ORIGINAL = "1989"
TV = "1989 (Taylor's Version)"


class _SideBase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db)
        self._orig = play_history.DB_PATH
        play_history.DB_PATH = self.db
        self.t = 1_780_000_000

    def tearDown(self):
        play_history.DB_PATH = self._orig
        try:
            os.remove(self.db)
        except OSError:
            pass

    def seed(self, title, album, artist="Taylor Swift", locked=None):
        self.t += 200
        conn = sqlite3.connect(self.db)
        cur = conn.execute(
            "INSERT INTO plays (title, artist, album, played_at, album_locked)"
            " VALUES (?, ?, ?, ?, ?)", (title, artist, album, self.t, locked))
        conn.commit()
        pid = cur.lastrowid
        conn.close()
        return pid

    def albums(self):
        conn = sqlite3.connect(self.db)
        rows = [r[0] for r in conn.execute(
            "SELECT album FROM plays WHERE deleted_at IS NULL ORDER BY played_at, id")]
        conn.close()
        return rows

    def side(self, leading, rendition):
        """`leading` plays on the original, then `rendition` on the re-recording."""
        for i in range(leading):
            self.seed(f"Opening {i}", ORIGINAL)
        last = None
        for i in range(rendition):
            last = self.seed(f"Track {i} (Taylor's Version)", TV)
        return last


class LeadingMislabelTest(_SideBase):
    # --- the reported case ---

    def test_one_opening_play_joins_the_re_recording(self):
        last = self.side(leading=1, rendition=8)
        reconcile.reconcile_album(last, db_path=self.db)
        self.assertEqual(set(self.albums()), {TV})

    def test_two_opening_plays_join_it_too(self):
        last = self.side(leading=2, rendition=8)
        reconcile.reconcile_album(last, db_path=self.db)
        self.assertEqual(set(self.albums()), {TV})

    def test_the_side_ends_up_on_one_record(self):
        last = self.side(leading=1, rendition=20)
        reconcile.reconcile_album(last, db_path=self.db)
        self.assertEqual(len(set(self.albums())), 1)

    # --- and where it must not reach ---

    def test_a_whole_album_played_first_is_left_alone(self):
        # Playing 1989 and then 1989 (Taylor's Version) is two records, and
        # merging them is exactly what base_title exists to refuse.
        last = self.side(leading=13, rendition=21)
        reconcile.reconcile_album(last, db_path=self.db)
        self.assertEqual(set(self.albums()), {ORIGINAL, TV})

    def test_the_opening_must_be_outnumbered(self):
        last = self.side(leading=3, rendition=2)
        reconcile.reconcile_album(last, db_path=self.db)
        self.assertEqual(set(self.albums()), {ORIGINAL, TV})

    def test_it_only_ever_moves_toward_a_rendition(self):
        # The reverse — a side of the original opening on the re-recording —
        # is not a thing this may fix, because the plain title is where an
        # unqualified track legitimately belongs.
        self.seed("Opening (Taylor's Version)", TV)
        last = self.seed("Track", ORIGINAL)
        for _ in range(5):
            last = self.seed(f"More {_}", ORIGINAL)
        reconcile.reconcile_album(last, db_path=self.db)
        self.assertIn(TV, set(self.albums()))

    def test_a_different_rendition_is_never_absorbed(self):
        # A live album is not a pressing of the studio one and never becomes it.
        self.seed("Opening (Live)", "1989 (Live)")
        last = self.side(leading=0, rendition=6)
        reconcile.reconcile_album(last, db_path=self.db)
        self.assertIn("1989 (Live)", set(self.albums()))

    def test_an_unrelated_record_in_the_opening_is_not_touched(self):
        self.seed("Something Else", "Red")
        last = self.side(leading=0, rendition=6)
        reconcile.reconcile_album(last, db_path=self.db)
        self.assertIn("Red", set(self.albums()))

    def test_a_hand_set_album_is_never_overwritten(self):
        self.seed("Deliberate", ORIGINAL, locked=1)
        last = self.side(leading=0, rendition=6)
        reconcile.reconcile_album(last, db_path=self.db)
        self.assertIn(ORIGINAL, set(self.albums()))

    def test_a_run_already_on_one_record_changes_nothing(self):
        last = self.side(leading=0, rendition=6)
        self.assertEqual(reconcile.reconcile_album(last, db_path=self.db), 0)
        self.assertEqual(set(self.albums()), {TV})

    def test_another_artists_plays_are_not_in_the_run(self):
        self.seed("Their Song", "Their Album", artist="Someone Else")
        last = self.side(leading=1, rendition=6)
        reconcile.reconcile_album(last, db_path=self.db)
        self.assertIn("Their Album", set(self.albums()))


class StillMergesEditionsTest(_SideBase):
    """The ordinary edition rule has to keep working alongside this."""

    def test_a_deluxe_still_reduces_to_the_plain_title(self):
        self.seed("One", "OK ORCHESTRA", artist="AJR")
        last = self.seed("Two", "OK ORCHESTRA (Deluxe)", artist="AJR")
        reconcile.reconcile_album(last, db_path=self.db)
        self.assertEqual(set(self.albums()), {"OK ORCHESTRA"})

    def test_a_taylors_version_deluxe_reduces_to_taylors_version(self):
        # Its own edition strips normally; it never falls back to the original.
        self.seed("One (Taylor's Version)", TV)
        last = self.seed("Two (Taylor's Version)", "1989 (Taylor's Version) [Deluxe]")
        reconcile.reconcile_album(last, db_path=self.db)
        self.assertEqual(set(self.albums()), {TV})


if __name__ == "__main__":
    unittest.main()
