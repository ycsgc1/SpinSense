import os
import sys
import tempfile
import unittest

from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import play_history  # noqa: E402
import backend_main  # noqa: E402


class DeleteApiTest(unittest.TestCase):
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

    def test_delete_then_gone_from_list(self):
        pid = play_history.record_play("X", "A", None, None, db_path=self.db_path)
        r = self.client.delete(f"/api/plays/{pid}")
        self.assertEqual(r.status_code, 200)
        body = self.client.get("/api/plays").json()
        self.assertEqual(body["plays"], [])
        self.assertEqual(body["total"], 0)

    def test_delete_unknown_404(self):
        self.assertEqual(self.client.delete("/api/plays/999").status_code, 404)

    def test_restore_brings_back(self):
        pid = play_history.record_play("Y", "A", None, None, db_path=self.db_path)
        self.client.delete(f"/api/plays/{pid}")
        r = self.client.post(f"/api/plays/{pid}/restore")
        self.assertEqual(r.status_code, 200)
        titles = [p["title"] for p in self.client.get("/api/plays").json()["plays"]]
        self.assertEqual(titles, ["Y"])

    def test_restore_unknown_404(self):
        self.assertEqual(self.client.post("/api/plays/999/restore").status_code, 404)


if __name__ == "__main__":
    unittest.main()
