import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import unittest
from fastapi.testclient import TestClient


class TestStatusApi(unittest.TestCase):
    def test_status_default_is_stopped(self):
        import importlib, ipc_manager
        importlib.reload(ipc_manager)
        import backend_main
        importlib.reload(backend_main)
        client = TestClient(backend_main.app)
        r = client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status_msg"], "stopped")
        self.assertEqual(body["engine_active"], False)
        self.assertIn("track", body)

    def test_status_reflects_last_broadcast(self):
        import backend_main
        backend_main.manager.last_status = {
            "engine_active": True, "status_msg": "Playing", "rms_level": 0.2,
            "track": {"title": "X", "artist": "Y", "album": "", "art_url": "http://a"},
        }
        client = TestClient(backend_main.app)
        body = client.get("/api/status").json()
        self.assertEqual(body["status_msg"], "Playing")
        self.assertEqual(body["track"]["title"], "X")
