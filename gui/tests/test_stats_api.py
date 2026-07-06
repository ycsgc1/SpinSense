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


class StatsApiTest(unittest.TestCase):
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

    def test_empty_db_shape(self):
        body = self.client.get("/api/stats?period=all").json()
        self.assertEqual(body["totals"]["plays"], 0)
        self.assertEqual(body["top_artists"], [])
        self.assertEqual(body["plays_over_time"]["buckets"], [])
        for key in ("period", "totals", "top_tracks", "genres", "decades"):
            self.assertIn(key, body)

    def test_counts_a_play(self):
        play_history.record_play("T", "A", None, None, db_path=self.db_path)
        body = self.client.get("/api/stats?period=all").json()
        self.assertEqual(body["totals"]["plays"], 1)
        self.assertEqual(body["top_artists"][0]["artist"], "A")

    def test_invalid_period_400(self):
        self.assertEqual(self.client.get("/api/stats?period=week").status_code, 400)

    def test_invalid_month_400(self):
        self.assertEqual(
            self.client.get("/api/stats?period=month&month=13").status_code, 400)
