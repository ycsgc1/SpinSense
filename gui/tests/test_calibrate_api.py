"""Integration tests for /api/calibrate/{start,status,clear} against a
controllable fake UDS listener that stands in for core_engine."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import backend_main  # noqa: E402


class FakeEngine:
    """Tiny UDS listener that responds to one command at a time per a
    user-controlled scripted-responses queue. Runs in a background thread
    on its own event loop so we don't deadlock the TestClient's loop."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.responses: list[dict] = []
        self.received: list[dict] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.AbstractServer | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def queue(self, response: dict) -> None:
        self.responses.append(response)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3)

    def stop(self) -> None:
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._thread:
            self._thread.join(timeout=2)
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)

    def _run(self) -> None:
        async def main():
            self._server = await asyncio.start_unix_server(
                self._handle, path=self.socket_path,
            )
            self._ready.set()
            async with self._server:
                await self._server.serve_forever()

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(main())
        except asyncio.CancelledError:
            pass

    async def _handle(self, reader, writer):
        line = await reader.readline()
        try:
            payload = json.loads(line.decode())
        except Exception:
            payload = {"_parse_error": True}
        self.received.append(payload)
        response = self.responses.pop(0) if self.responses else {"ok": False, "detail": "no response queued"}
        writer.write((json.dumps(response) + "\n").encode())
        await writer.drain()
        writer.close()


class CalibrateApiTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.socket_path = os.path.join(self.tmpdir, "spinsense-cmd.sock")
        self._orig_path = backend_main.CMD_SOCKET_PATH
        backend_main.CMD_SOCKET_PATH = self.socket_path
        self.client = TestClient(backend_main.app)

    def tearDown(self):
        backend_main.CMD_SOCKET_PATH = self._orig_path
        self.client.close()
        if os.path.exists(self.tmpdir):
            try:
                os.rmdir(self.tmpdir)
            except OSError:
                pass

    def test_start_returns_engine_ack(self):
        fake = FakeEngine(self.socket_path)
        fake.queue({"ok": True, "duration_s": 5.0})
        fake.start()
        try:
            res = self.client.post("/api/calibrate/start", json={"phase": "noise_floor"})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json(), {"ok": True, "duration_s": 5.0})
            self.assertEqual(fake.received[0]["cmd"], "start_calibration")
            self.assertEqual(fake.received[0]["phase"], "noise_floor")
        finally:
            fake.stop()

    def test_start_rejects_invalid_phase(self):
        # No fake listener needed — validation happens before dispatch.
        res = self.client.post("/api/calibrate/start", json={"phase": "garbage"})
        self.assertEqual(res.status_code, 400)

    def test_start_503_when_engine_unreachable(self):
        # No fake listener running; socket doesn't exist.
        res = self.client.post("/api/calibrate/start", json={"phase": "noise_floor"})
        self.assertEqual(res.status_code, 503)
        self.assertIn("detail", res.json())

    def test_status_returns_engine_response(self):
        fake = FakeEngine(self.socket_path)
        fake.queue({"status": "done", "samples_count": 100, "stats": {"p10": 0.001}})
        fake.start()
        try:
            res = self.client.get("/api/calibrate/status")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertEqual(body["status"], "done")
            self.assertEqual(body["samples_count"], 100)
            self.assertEqual(body["stats"]["p10"], 0.001)
            self.assertEqual(fake.received[0]["cmd"], "get_calibration")
        finally:
            fake.stop()

    def test_clear_returns_engine_ack(self):
        fake = FakeEngine(self.socket_path)
        fake.queue({"ok": True})
        fake.start()
        try:
            res = self.client.post("/api/calibrate/clear")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json(), {"ok": True})
            self.assertEqual(fake.received[0]["cmd"], "clear_calibration")
        finally:
            fake.stop()


if __name__ == "__main__":
    unittest.main()
