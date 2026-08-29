"""The engine diagnostics buffer and /api/events.

The engine's print() output only reaches `docker logs`, which needs shell
access on the host — so a stalled input or a run of failed identifications was
invisible from the web UI. Events cross the same socket as status frames, so
everything here is untrusted input from another process.
"""
import asyncio
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

from fastapi.testclient import TestClient  # noqa: E402

import backend_main  # noqa: E402
import ipc_manager  # noqa: E402


class RecordEventTest(unittest.TestCase):
    def setUp(self):
        ipc_manager.events.clear()

    def test_a_well_formed_event_is_kept(self):
        ipc_manager.record_event({"ts": 1000, "level": "warning", "message": "stalled"})
        self.assertEqual(list(ipc_manager.events),
                         [{"ts": 1000, "level": "warning", "message": "stalled"}])

    def test_newest_last(self):
        for i in range(3):
            ipc_manager.record_event({"ts": i, "level": "info", "message": f"e{i}"})
        self.assertEqual([e["message"] for e in ipc_manager.events], ["e0", "e1", "e2"])

    def test_the_buffer_is_bounded(self):
        # In memory and unbounded would be a slow leak on a box that runs for
        # months; this is "what just happened", not an audit trail.
        for i in range(ipc_manager.EVENT_LIMIT + 50):
            ipc_manager.record_event({"ts": i, "level": "info", "message": str(i)})
        self.assertEqual(len(ipc_manager.events), ipc_manager.EVENT_LIMIT)
        self.assertEqual(ipc_manager.events[-1]["message"],
                         str(ipc_manager.EVENT_LIMIT + 49))

    def test_junk_from_the_socket_is_dropped_not_raised(self):
        for junk in (None, "nope", 5, {}, {"message": "   "}, {"level": "warning"}):
            with self.subTest(junk=junk):
                ipc_manager.record_event(junk)
        self.assertEqual(len(ipc_manager.events), 0)

    def test_an_unknown_level_falls_back_to_info(self):
        # The UI colours by level; an unexpected one must not break rendering.
        ipc_manager.record_event({"message": "x", "level": "catastrophe"})
        self.assertEqual(ipc_manager.events[0]["level"], "info")

    def test_a_missing_timestamp_is_filled_in(self):
        ipc_manager.record_event({"message": "x"})
        self.assertGreater(ipc_manager.events[0]["ts"], 0)

    def test_absurdly_long_messages_are_truncated(self):
        ipc_manager.record_event({"message": "x" * 5000})
        self.assertLessEqual(len(ipc_manager.events[0]["message"]), 500)


class EventFrameRoutingTest(unittest.TestCase):
    """Events share the status socket, so the reader has to tell them apart —
    and an event must never be mistaken for a track and recorded as a play."""

    def setUp(self):
        ipc_manager.events.clear()

    def feed(self, *frames):
        class Reader:
            def __init__(self, lines):
                self.lines = list(lines)

            async def readline(self):
                return self.lines.pop(0) if self.lines else b""

        class Writer:
            def close(self):
                pass

        lines = [(json.dumps(f) + "\n").encode() for f in frames]
        asyncio.run(ipc_manager.handle_uds_client(Reader(lines), Writer()))

    def test_an_event_frame_lands_in_the_buffer(self):
        self.feed({"type": "event",
                   "payload": {"ts": 1, "level": "warning", "message": "stalled"}})
        self.assertEqual(len(ipc_manager.events), 1)

    def test_an_event_frame_does_not_record_a_play(self):
        recorded = []

        async def fake_record(track, play_clock=None):
            recorded.append(track)

        orig = ipc_manager._record_if_new
        ipc_manager._record_if_new = fake_record
        try:
            self.feed({"type": "event", "payload": {"message": "x"}})
        finally:
            ipc_manager._record_if_new = orig
        self.assertEqual(recorded, [])


class EventsApiTest(unittest.TestCase):
    def setUp(self):
        ipc_manager.events.clear()
        self.client = TestClient(backend_main.app)

    def test_newest_first_for_reading(self):
        # The buffer stores oldest-first; a log people read wants the opposite.
        for i in range(3):
            ipc_manager.record_event({"ts": i, "level": "info", "message": f"e{i}"})
        got = self.client.get("/api/events").json()["events"]
        self.assertEqual([e["message"] for e in got], ["e2", "e1", "e0"])

    def test_limit_is_respected_and_capped(self):
        for i in range(150):
            ipc_manager.record_event({"ts": i, "level": "info", "message": str(i)})
        self.assertEqual(len(self.client.get("/api/events?limit=10").json()["events"]), 10)
        self.assertLessEqual(
            len(self.client.get("/api/events?limit=9999").json()["events"]), 200)

    def test_empty_is_an_empty_list_not_an_error(self):
        r = self.client.get("/api/events")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["events"], [])


if __name__ == "__main__":
    unittest.main()
