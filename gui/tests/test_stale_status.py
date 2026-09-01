"""A status frame stops being true when the engine stops sending them.

The last frame used to stand forever. When the engine died mid-session,
`/api/status` and the Home Assistant integration kept reporting whatever was on
the platter at the moment it stopped, `engine_active` still true — so the
dashboard looked healthy and the only symptom was a meter that never moved.
"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import ipc_manager  # noqa: E402


def frame(title="Taste"):
    return {
        "type": "live_status",
        "payload": {
            "engine_active": True, "status_msg": "Playing", "phase": "playing",
            "rms_level": 0.04,
            "track": {"title": title, "artist": "A", "album": "B", "art_url": ""},
            "play_clock": None,
        },
    }


class StaleStatusTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = ipc_manager.ConnectionManager()

    def age(self, secs):
        self.manager.last_status_at = time.time() - secs

    async def test_nothing_reported_yet_reads_as_stopped(self):
        status = self.manager.current_status()
        self.assertFalse(status["engine_active"])
        self.assertEqual(status["status_msg"], "stopped")

    async def test_a_fresh_frame_is_reported_as_is(self):
        await self.manager.broadcast(frame())
        status = self.manager.current_status()
        self.assertTrue(status["engine_active"])
        self.assertEqual(status["track"]["title"], "Taste")

    async def test_a_frame_still_inside_the_window_survives(self):
        # Recognition stops the capture stream, and the retry ladder can run
        # ~25 s, so the window has to outlast a scan or the dashboard would
        # blink to "stopped" every time a track was identified.
        await self.manager.broadcast(frame())
        self.age(ipc_manager.STATUS_STALE_SECS - 5)
        self.assertTrue(self.manager.current_status()["engine_active"])

    async def test_a_stale_frame_reads_as_stopped(self):
        await self.manager.broadcast(frame())
        self.age(ipc_manager.STATUS_STALE_SECS + 5)
        status = self.manager.current_status()
        self.assertFalse(status["engine_active"])
        self.assertEqual(status["track"]["title"], "")

    async def test_the_window_outlasts_a_full_retry_ladder(self):
        # Pinned in absolute seconds, not against the constant, so shrinking it
        # below what a recognition takes is a test failure.
        self.assertGreaterEqual(ipc_manager.STATUS_STALE_SECS, 30)

    async def test_a_new_frame_makes_it_current_again(self):
        await self.manager.broadcast(frame("Old"))
        self.age(ipc_manager.STATUS_STALE_SECS + 5)
        self.assertFalse(self.manager.current_status()["engine_active"])
        await self.manager.broadcast(frame("New"))
        status = self.manager.current_status()
        self.assertTrue(status["engine_active"])
        self.assertEqual(status["track"]["title"], "New")

    async def test_the_stale_reply_is_a_copy_not_the_shared_default(self):
        # A caller mutating the response must not corrupt DEFAULT_STATUS for
        # every future request.
        await self.manager.broadcast(frame())
        self.age(ipc_manager.STATUS_STALE_SECS + 5)
        self.manager.current_status()["engine_active"] = True
        self.assertFalse(ipc_manager.DEFAULT_STATUS["engine_active"])
        self.assertFalse(self.manager.current_status()["engine_active"])


if __name__ == "__main__":
    unittest.main()
