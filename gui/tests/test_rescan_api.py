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
from tests.test_calibrate_api import FakeEngine  # noqa: E402


class RescanApiTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.socket_path = os.path.join(self.tmpdir, "spinsense-cmd.sock")
        self._orig = backend_main.CMD_SOCKET_PATH
        backend_main.CMD_SOCKET_PATH = self.socket_path
        self.client = TestClient(backend_main.app)

    def tearDown(self):
        backend_main.CMD_SOCKET_PATH = self._orig
        self.client.close()
        try:
            os.rmdir(self.tmpdir)
        except OSError:
            pass

    def test_rescan_acks(self):
        fake = FakeEngine(self.socket_path)
        fake.queue({"ok": True})
        fake.start()
        try:
            res = self.client.post("/api/rescan")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json(), {"ok": True})
            self.assertEqual(fake.received[0]["cmd"], "rescan")
        finally:
            fake.stop()

    def test_rescan_503_when_engine_down(self):
        res = self.client.post("/api/rescan")
        self.assertEqual(res.status_code, 503)


if __name__ == "__main__":
    unittest.main()
