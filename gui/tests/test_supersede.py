"""Replacing a play a rescan corrects, and refusing to when it shouldn't.

The rescan button exists because an identification can be wrong, and the way it
is usually wrong is the first one of a side: a needle drop starts a scan of the
lead-in groove and the recognizer answers from whatever fragment reached it.
Filing the correction as a *second* play would leave the wrong one in history
and in the scrobble queue, which is the opposite of what pressing the button
meant.

The guard against overreach is time. A rescan minutes into a play means
something else entirely — the engine sat through a transition it never heard —
and the play being corrected is one that really did play.
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


class SupersedeTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db)
        self._orig_db = play_history.DB_PATH
        play_history.DB_PATH = self.db
        ipc_manager._last_recorded_key = None
        ipc_manager._last_play_id = None
        ipc_manager.events.clear()

        # Recording spawns background work that wants a network and a Last.fm
        # session; none of it is what these tests are about.
        self._orig = (ipc_manager._spawn_now_playing, ipc_manager.spawn_run_art)
        ipc_manager._spawn_now_playing = lambda track: None
        ipc_manager.spawn_run_art = lambda pid, before, url, rec: None

    def tearDown(self):
        (ipc_manager._spawn_now_playing, ipc_manager.spawn_run_art) = self._orig
        play_history.DB_PATH = self._orig_db
        ipc_manager._last_recorded_key = None
        ipc_manager._last_play_id = None
        try:
            os.remove(self.db)
        except OSError:
            pass

    # --- helpers ---

    def feed(self, title, supersede=False, artist="AJR"):
        asyncio.run(ipc_manager._record_if_new(
            {"title": title, "artist": artist, "album": "Some Album"},
            None, supersede=supersede))
        # One call stands for one *identification*. The engine repeats the
        # current track on every frame and the dedupe swallows those, so
        # clearing the key here is what makes each feed() a distinct event.
        ipc_manager._last_recorded_key = None

    def live(self):
        return [(r["id"], r["title"]) for r in
                play_history.recent_plays(limit=50, db_path=self.db)]

    def titles(self):
        return [t for _id, t in self.live()]

    def backdate(self, play_id, secs):
        """Make a play look `secs` old, the only thing here real time governs."""
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE plays SET played_at = ? WHERE id = ?",
                     (int(time.time()) - secs, play_id))
        conn.commit()
        conn.close()

    def row(self, play_id):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM plays WHERE id = ?", (play_id,)).fetchone()
        conn.close()
        return dict(r)

    # --- the correction itself ---

    def test_a_rescan_replaces_the_play_it_just_filed(self):
        self.feed("Misfire")
        first = ipc_manager._last_play_id
        self.feed("The Real Track", supersede=True)
        self.assertEqual(self.titles(), ["The Real Track"])
        self.assertIsNotNone(self.row(first)["deleted_at"])

    def test_the_replaced_play_is_recoverable_not_destroyed(self):
        # Soft-deleted, so it is undoable for the same grace period as a delete
        # from the history page. This is a judgement call about intent, and
        # judgement calls should not be irreversible.
        self.feed("Misfire")
        first = ipc_manager._last_play_id
        self.feed("The Real Track", supersede=True)
        self.assertTrue(play_history.restore_play(first, db_path=self.db))
        self.assertIn("Misfire", self.titles())

    def test_the_replaced_play_never_reaches_last_fm(self):
        self.feed("Misfire")
        self.feed("The Real Track", supersede=True)
        asyncio.run(ipc_manager._record_if_new({"title": "", "artist": ""}))
        queued = [c["title"] for c in
                  play_history.scrobble_candidates(db_path=self.db)]
        self.assertEqual(queued, ["The Real Track"])

    def test_it_says_so_in_the_diagnostics_log(self):
        # Deleting a row on inference should be visible, not silent.
        self.feed("Misfire")
        self.feed("The Real Track", supersede=True)
        self.assertTrue(any("Misfire" in e["message"] for e in ipc_manager.events))

    # --- and where it must not reach ---

    def test_an_ordinary_transition_files_a_second_play(self):
        self.feed("Track One")
        self.feed("Track Two")
        self.assertEqual(self.titles(), ["Track Two", "Track One"])

    def test_a_rescan_past_the_window_files_a_second_play(self):
        # The listener is correcting a missed transition, not a misfire: the
        # play being replaced really did play for that long.
        self.feed("Track One")
        first = ipc_manager._last_play_id
        self.backdate(first, ipc_manager.SUPERSEDE_WINDOW_SECS + 1)
        self.feed("Track Two", supersede=True)
        self.assertEqual(self.titles(), ["Track Two", "Track One"])

    def test_a_rescan_ten_minutes_into_a_play_never_replaces_it(self):
        # Pinned in real seconds rather than against the constant, so this also
        # says the window is a sane size: ten minutes in, whatever is showing
        # has been showing long enough to have been the truth.
        self.feed("Track One")
        self.backdate(ipc_manager._last_play_id, 600)
        self.feed("Track Two", supersede=True)
        self.assertEqual(self.titles(), ["Track Two", "Track One"])

    def test_a_rescan_seconds_into_a_play_does_replace_it(self):
        # The other end of the same statement: the window is not so tight that
        # the case it exists for — noticing a wrong title and reaching for the
        # button — falls outside it.
        self.feed("Misfire")
        self.backdate(ipc_manager._last_play_id, 20)
        self.feed("The Real Track", supersede=True)
        self.assertEqual(self.titles(), ["The Real Track"])

    def test_a_play_kept_past_the_window_is_still_closed_properly(self):
        # The supersede path replaces _stamp_last_play_ended(); declining to
        # supersede must fall through to it, or the play never ends and is
        # never scrobbled.
        self.feed("Track One")
        first = ipc_manager._last_play_id
        self.backdate(first, ipc_manager.SUPERSEDE_WINDOW_SECS + 1)
        self.feed("Track Two", supersede=True)
        self.assertIsNotNone(self.row(first)["ended_at"])

    def test_a_scrobbled_play_is_never_dropped(self):
        # Last.fm has no API to take a scrobble back, so a submitted play is
        # history whether or not it was right. Deleting our copy would only
        # make the two disagree.
        self.feed("Already Sent")
        first = ipc_manager._last_play_id
        play_history.mark_scrobbled([first], int(time.time()), db_path=self.db)
        self.feed("The Real Track", supersede=True)
        self.assertEqual(self.titles(), ["The Real Track", "Already Sent"])

    def test_a_rescan_with_no_open_play_records_normally(self):
        self.feed("First Thing Today", supersede=True)
        self.assertEqual(self.titles(), ["First Thing Today"])

    def test_a_rescan_after_silence_has_nothing_to_replace(self):
        # Silence closed the play and cleared the pointer; the record was
        # lifted and put back, which is two plays however fast it happened.
        self.feed("Track One")
        asyncio.run(ipc_manager._record_if_new({"title": "", "artist": ""}))
        self.feed("Track Two", supersede=True)
        self.assertEqual(self.titles(), ["Track Two", "Track One"])

    def test_a_rescan_confirming_the_same_track_leaves_the_row_alone(self):
        # Frames arrive as they really do — dedupe state intact — because the
        # answer here comes from the dedupe, not the supersede logic: an
        # identical identification is not a new play, so there is nothing to
        # replace and nothing to file.
        track = {"title": "Track One", "artist": "AJR", "album": "Some Album"}
        asyncio.run(ipc_manager._record_if_new(track))
        first = ipc_manager._last_play_id
        asyncio.run(ipc_manager._record_if_new(track, None, supersede=True))
        self.assertEqual(self.live(), [(first, "Track One")])


class FrameCarriesTheFlagTest(unittest.TestCase):
    """The flag has to survive the socket, not just exist in the engine."""

    def setUp(self):
        self.calls = []

        async def fake_record(track, play_clock=None, supersede=False):
            self.calls.append(supersede)

        self._orig = ipc_manager._record_if_new
        ipc_manager._record_if_new = fake_record

    def tearDown(self):
        ipc_manager._record_if_new = self._orig

    def deliver(self, payload):
        import json

        class Reader:
            def __init__(self, lines):
                self.lines = list(lines)

            async def readline(self):
                return self.lines.pop(0) if self.lines else b""

        asyncio.run(ipc_manager.handle_uds_client(
            Reader([json.dumps(payload).encode() + b"\n"]), None))

    def frame(self, **extra):
        return {"type": "live_status",
                "payload": {"track": {"title": "T", "artist": "A"}, **extra}}

    def test_a_flagged_frame_supersedes(self):
        self.deliver(self.frame(supersedes_previous=True))
        self.assertEqual(self.calls, [True])

    def test_an_unflagged_frame_does_not(self):
        self.deliver(self.frame(supersedes_previous=False))
        self.assertEqual(self.calls, [False])

    def test_a_frame_from_an_older_engine_does_not(self):
        # The field is additive; a build that predates it must not be read as
        # asking for a deletion.
        self.deliver(self.frame())
        self.assertEqual(self.calls, [False])


if __name__ == "__main__":
    unittest.main()
