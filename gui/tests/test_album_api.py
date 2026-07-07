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
        self.assertEqual(r.json(), {"status": "ok", "updated": 1})
        row = play_history.get_play(pid, db_path=self.db_path)
        self.assertEqual((row["album"], row["album_locked"]), ("New", 1))

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

    def test_art_url_fires_download(self):
        # Patch the SYNCHRONOUS spawn helper as resolved inside backend_main —
        # patching the async downloader would race the response (create_task
        # may not have run by assertion time).
        pid = play_history.record_play("T", "A", "Old", None, db_path=self.db_path)
        calls = []
        orig = backend_main.spawn_art_download
        backend_main.spawn_art_download = lambda p, u: calls.append((p, u))
        try:
            self.client.post(f"/api/plays/{pid}/album",
                             json={"album": "New", "art_url": "http://a/x.jpg"})
        finally:
            backend_main.spawn_art_download = orig
        self.assertEqual(calls, [(pid, "http://a/x.jpg")])

    def test_no_art_url_no_download(self):
        pid = play_history.record_play("T", "A", "Old", None, db_path=self.db_path)
        calls = []
        orig = backend_main.spawn_art_download
        backend_main.spawn_art_download = lambda p, u: calls.append((p, u))
        try:
            self.client.post(f"/api/plays/{pid}/album", json={"album": "New"})
        finally:
            backend_main.spawn_art_download = orig
        self.assertEqual(calls, [])
