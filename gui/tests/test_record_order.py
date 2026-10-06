"""One play is one row, however slowly the database answers.

The engine repeats the current track on every frame, once a second, and each
frame arrives on its own connection — so each is handled by its own task.
Recording is a check ("is this the track I filed last?"), then several awaited
database calls, and only then the note that it has been filed. While the
database is quick that window is a few milliseconds and nothing fits in it.
When a write takes longer than a second — a NAS spinning its disks up, a long
read holding the file — the next frame for the same track arrives inside it,
passes the same check, and files the play a second time.

That was a duplicate row in History for as long as it was only a database. With
scrobbling it is two scrobbles, and Last.fm has no way to take one back.

Frames are therefore recorded one at a time, in the order they arrived, which
also keeps a silence frame from being handled before the play it ends.
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import ipc_manager  # noqa: E402
import play_history  # noqa: E402

SLOW_WRITE_SECS = 0.05


def track(title, artist="Sabrina Carpenter"):
    return {"title": title, "artist": artist, "album": "Short n' Sweet"}


class RecordingIsSerializedTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db)
        self._orig_db = play_history.DB_PATH
        play_history.DB_PATH = self.db
        ipc_manager._last_recorded_key = None
        ipc_manager._last_play_id = None

        # Recording spawns background work that wants a network and a Last.fm
        # session; none of it is what these tests are about.
        self._orig = (ipc_manager._spawn_now_playing, ipc_manager.spawn_run_art,
                      play_history.record_play)
        ipc_manager._spawn_now_playing = lambda track: None
        ipc_manager.spawn_run_art = lambda pid, before, url, rec: None

        real_record = play_history.record_play

        def slow_record(*args, **kwargs):
            time.sleep(SLOW_WRITE_SECS)        # runs in a worker thread
            return real_record(*args, **kwargs)

        play_history.record_play = slow_record

    def tearDown(self):
        (ipc_manager._spawn_now_playing, ipc_manager.spawn_run_art,
         play_history.record_play) = self._orig
        play_history.DB_PATH = self._orig_db
        ipc_manager._last_recorded_key = None
        ipc_manager._last_play_id = None
        try:
            os.remove(self.db)
        except OSError:
            pass

    # --- helpers ---

    def arrive_together(self, *frames):
        """Hand over frames the way overlapping connections do: each in its own
        task, all started before the first has finished writing."""
        async def burst():
            await asyncio.gather(*(ipc_manager._record_if_new(f) for f in frames))
        asyncio.run(burst())

    def rows(self):
        with sqlite3.connect(self.db) as conn:
            return conn.execute(
                "SELECT title, ended_at FROM plays ORDER BY id").fetchall()

    # --- tests ---

    def test_repeated_frames_for_one_track_make_one_row(self):
        self.arrive_together(track("Espresso"), track("Espresso"), track("Espresso"))
        self.assertEqual([title for title, _ in self.rows()], ["Espresso"])

    def test_a_different_track_is_still_its_own_play(self):
        self.arrive_together(track("Taste"), track("Espresso"))
        rows = self.rows()
        self.assertEqual([title for title, _ in rows], ["Taste", "Espresso"])
        self.assertIsNotNone(rows[0][1])       # the first was closed by the second
        self.assertIsNone(rows[1][1])          # which is still playing

    def test_silence_closes_the_play_it_arrived_after(self):
        # Handled early, the silence frame finds no open play to close — and
        # the one being written is then left open with nothing coming to end it.
        self.arrive_together(track("Espresso"), {"title": ""})
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0][1])
        self.assertIsNone(ipc_manager._last_recorded_key)

    def test_the_same_track_again_after_silence_is_a_new_play(self):
        # The dedupe must not become a reason to lose a second spin.
        self.arrive_together(track("Espresso"), {"title": ""}, track("Espresso"))
        self.assertEqual([title for title, _ in self.rows()], ["Espresso", "Espresso"])

    def test_each_event_loop_gets_a_working_lock(self):
        # The suite drives a fresh loop per call; a lock left over from a
        # finished one would raise rather than serialize.
        for title in ("Taste", "Espresso"):
            asyncio.run(ipc_manager._record_if_new(track(title)))
        self.assertEqual([title for title, _ in self.rows()], ["Taste", "Espresso"])


if __name__ == "__main__":
    unittest.main()
