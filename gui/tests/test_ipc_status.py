import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import asyncio
import unittest


class TestLastStatusCache(unittest.TestCase):
    def test_broadcast_caches_live_status_payload(self):
        from ipc_manager import ConnectionManager
        mgr = ConnectionManager()
        frame = {"type": "live_status", "payload": {"engine_active": True, "status_msg": "Playing"}}
        asyncio.run(mgr.broadcast(frame))
        self.assertEqual(mgr.last_status["status_msg"], "Playing")

    def test_default_last_status_is_stopped(self):
        from ipc_manager import ConnectionManager
        mgr = ConnectionManager()
        self.assertEqual(mgr.last_status["status_msg"], "stopped")
        self.assertEqual(mgr.last_status["engine_active"], False)
        self.assertIn("track", mgr.last_status)
