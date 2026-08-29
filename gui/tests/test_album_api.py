import os
import sys
import tempfile
import unittest

from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import backend_main  # noqa: E402
from spinsense import itunes  # noqa: E402
import play_history  # noqa: E402


class AlbumApiBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db_path)
        self._orig = play_history.DB_PATH
        play_history.DB_PATH = self.db_path
        self.client = TestClient(backend_main.app)  # no `with`: lifespan stays off

    def tearDown(self):
        play_history.DB_PATH = self._orig
        self.client.close()
        try:
            os.remove(self.db_path)
        except OSError:
            pass


class CandidatesTest(AlbumApiBase):
    def setUp(self):
        super().setUp()
        self._orig_fetch = backend_main._itunes_album_candidates

    def tearDown(self):
        backend_main._itunes_album_candidates = self._orig_fetch
        super().tearDown()

    def test_unknown_play_404(self):
        self.assertEqual(
            self.client.get("/api/plays/999/album-candidates").status_code, 404)

    def test_candidates_shape(self):
        pid = play_history.record_play("Come Together", "The Beatles",
                                       "Abbey Road", None, db_path=self.db_path)
        async def fake(artist, title):
            return [{"album": "Abbey Road (Super Deluxe Edition)",
                     "art_url": "http://a/1000x1000bb.jpg"}]
        backend_main._itunes_album_candidates = fake
        body = self.client.get(f"/api/plays/{pid}/album-candidates").json()
        self.assertEqual(body["current"], "Abbey Road")
        self.assertEqual(body["candidates"][0]["album"],
                         "Abbey Road (Super Deluxe Edition)")


class SetAlbumTest(AlbumApiBase):
    def test_empty_album_400(self):
        pid = play_history.record_play("T", "A", None, None, db_path=self.db_path)
        r = self.client.post(f"/api/plays/{pid}/album", json={"album": "  "})
        self.assertEqual(r.status_code, 400)

    def test_unknown_play_404(self):
        r = self.client.post("/api/plays/999/album", json={"album": "X"})
        self.assertEqual(r.status_code, 404)

    def test_single_play_update_locks(self):
        pid = play_history.record_play("T", "A", "Old", None, db_path=self.db_path)
        r = self.client.post(f"/api/plays/{pid}/album", json={"album": "New"})
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["updated"], 1)
        row = play_history.get_play(pid, db_path=self.db_path)
        self.assertEqual((row["album"], row["album_locked"]), ("New", 1))

    def test_response_carries_the_rows_it_changed(self):
        # The page redraws from these instead of reloading, so they have to be
        # the post-edit state, not what the client already had.
        pid = play_history.record_play("T", "A", "Old", None, db_path=self.db_path)
        body = self.client.post(f"/api/plays/{pid}/album",
                                json={"album": "New"}).json()
        self.assertEqual(body["rows"], [{"id": pid, "album": "New", "art_path": None}])

    def test_apply_to_run_updates_whole_run(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO plays (title, artist, album, played_at, album_locked)"
                     " VALUES ('t1', 'A', 'Wrong', 1000, 1)")
        cur = conn.execute("INSERT INTO plays (title, artist, album, played_at)"
                           " VALUES ('t2', 'A', 'Also Wrong', 1100)")
        pid = cur.lastrowid
        conn.commit()
        conn.close()
        r = self.client.post(f"/api/plays/{pid}/album",
                             json={"album": "Right", "apply_to_run": True})
        self.assertEqual(r.json()["updated"], 2)

    def _capture_unify(self):
        """Replace unify_art as resolved inside backend_main, recording its args."""
        calls = []

        async def fake(ids, source_play_id, art_url=None):
            calls.append((list(ids), source_play_id, art_url))
            return list(ids)

        self._orig_unify = backend_main.unify_art
        backend_main.unify_art = fake
        self.addCleanup(lambda: setattr(backend_main, "unify_art", self._orig_unify))
        return calls

    def test_art_is_unified_across_the_whole_run(self):
        # The reported bug: the album unified but every other row kept its old
        # cover, so a run edited to one album still looked like several.
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO plays (title, artist, album, played_at)"
                     " VALUES ('t1', 'A', 'Wrong', 1000)")
        cur = conn.execute("INSERT INTO plays (title, artist, album, played_at)"
                           " VALUES ('t2', 'A', 'Also Wrong', 1100)")
        pid = cur.lastrowid
        conn.commit()
        conn.close()
        calls = self._capture_unify()
        self.client.post(f"/api/plays/{pid}/album",
                         json={"album": "Right", "art_url": "http://a/x.jpg",
                               "apply_to_run": True})
        ids, source, url = calls[0]
        self.assertEqual(len(ids), 2)          # every play in the run, not just one
        self.assertEqual(source, pid)
        self.assertEqual(url, "http://a/x.jpg")

    def test_no_art_url_still_unifies_onto_the_edited_play(self):
        # Typing an album by hand, or picking a candidate with no cover, used to
        # skip artwork entirely and leave the run mismatched. It now falls back
        # to the artwork of the row being edited.
        pid = play_history.record_play("T", "A", "Old", None, db_path=self.db_path)
        calls = self._capture_unify()
        self.client.post(f"/api/plays/{pid}/album", json={"album": "New"})
        self.assertEqual(calls, [([pid], pid, None)])


class AlbumCandidatesTest(unittest.TestCase):
    """Now lives in spinsense.itunes, shared with the engine's enrichment —
    there used to be two iTunes parsers free to disagree."""

    def test_dedupes_and_upscales(self):
        got = itunes.album_candidates([
            {"collectionName": "A", "artworkUrl100": "http://x/100x100bb.jpg"},
            {"collectionName": "A", "artworkUrl100": "http://x/100x100bb.jpg"},
            {"collectionName": "B", "artworkUrl100": "http://y/100x100bb.jpg"},
        ])
        self.assertEqual([c["album"] for c in got], ["A", "B"])
        self.assertEqual(got[0]["art_url"], "http://x/1000x1000bb.jpg")

    def test_missing_art_is_none_and_empty_input_safe(self):
        self.assertEqual(itunes.album_candidates([]), [])
        self.assertEqual(itunes.album_candidates(None), [])
        got = itunes.album_candidates([{"collectionName": "A"}])
        self.assertIsNone(got[0]["art_url"])

    def test_rows_without_an_album_are_skipped(self):
        got = itunes.album_candidates([{"artworkUrl100": "x"}, {"collectionName": "A"}])
        self.assertEqual([c["album"] for c in got], ["A"])

    def test_the_list_is_capped(self):
        got = itunes.album_candidates(
            [{"collectionName": f"A{i}"} for i in range(50)])
        self.assertEqual(len(got), 10)


