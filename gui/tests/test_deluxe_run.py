"""A side that turns out to be the deluxe, replayed end to end.

The reported session: eleven tracks of *Short n' Sweet*, then "15 Minutes"
(which iTunes' search cannot place at all), then "Please Please Please" with
Dolly Parton — a track that exists only on the deluxe. Nothing upgraded, and
every row kept the standard cover.

Three separate pieces have to work together for that to come out right: the
engine must let the bonus track reach search rather than answering it from the
standard album's tracklist, the reconciler must upgrade the run on the evidence,
and the artwork must follow the album it settled on.
"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import ipc_manager  # noqa: E402
import play_history  # noqa: E402

BASE = "Short n' Sweet"
DELUXE = "Short n' Sweet (Deluxe)"
ART_BASE = "http://cover/base.jpg"
ART_DELUXE = "http://cover/deluxe.jpg"

SIDE = ["Taste", "Please Please Please", "Good Graces", "Sharpest Tool",
        "Coincidence", "Bed Chem", "Espresso", "Dumb & Poetic"]


class DeluxeRunTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db)
        self._orig_db = play_history.DB_PATH
        play_history.DB_PATH = self.db

        self.root = tempfile.mkdtemp()
        self._orig_art_dir = ipc_manager.ART_DIR
        ipc_manager.ART_DIR = os.path.join(self.root, "art")

        ipc_manager._last_recorded_key = None
        ipc_manager._last_play_id = None
        ipc_manager._art_tasks.clear()

        self._orig = (ipc_manager._spawn_now_playing, ipc_manager._fetch_art,
                      ipc_manager._thumbnail)
        ipc_manager._spawn_now_playing = lambda track: None

        async def fake_fetch(url):
            # Distinct bytes per cover, so "which artwork is this" is decidable.
            return f"IMAGE:{url}".encode()

        ipc_manager._fetch_art = fake_fetch
        ipc_manager._thumbnail = lambda data: data

    def tearDown(self):
        (ipc_manager._spawn_now_playing, ipc_manager._fetch_art,
         ipc_manager._thumbnail) = self._orig
        ipc_manager.ART_DIR = self._orig_art_dir
        play_history.DB_PATH = self._orig_db
        ipc_manager._last_recorded_key = None
        ipc_manager._last_play_id = None
        shutil.rmtree(self.root, ignore_errors=True)
        try:
            os.remove(self.db)
        except OSError:
            pass

    # --- driving ---

    async def feed(self, title, album, art_url, artist="Sabrina Carpenter",
                   exclusive=False):
        await ipc_manager._record_if_new({
            "title": title, "artist": artist, "album": album,
            "art_url": art_url, "album_exclusive": exclusive,
        })
        ipc_manager._last_recorded_key = None

    async def settle(self):
        """Artwork is deliberately off the frame path, so wait for it here."""
        while ipc_manager._art_tasks:
            await asyncio.gather(*list(ipc_manager._art_tasks))

    async def play_the_side(self):
        for title in SIDE:
            await self.feed(title, BASE, ART_BASE)

    def rows(self):
        return play_history.recent_plays(limit=50, db_path=self.db)

    def albums(self):
        return {r["album"] for r in self.rows()}

    def art_bytes(self, row):
        rel = row.get("art_path")
        if not rel:
            return None
        with open(os.path.join(self.root, rel), "rb") as fh:
            return fh.read()

    def covers(self):
        return {self.art_bytes(r) for r in self.rows()}

    # --- the reported session ---

    def test_a_bonus_track_upgrades_the_whole_side(self):
        async def go():
            await self.play_the_side()
            self.assertEqual(self.albums(), {BASE})
            # The engine resolved this against the deluxe and said so.
            await self.feed("Please Please Please", DELUXE, ART_DELUXE,
                            artist="Sabrina Carpenter & Dolly Parton",
                            exclusive=True)
            await self.settle()
        asyncio.run(go())
        self.assertEqual(self.albums(), {DELUXE})

    def test_the_artwork_follows_the_album(self):
        # The visible half of the complaint: right title, wrong cover.
        async def go():
            await self.play_the_side()
            await self.settle()
            self.assertEqual(self.covers(), {b"IMAGE:" + ART_BASE.encode()})
            await self.feed("Please Please Please", DELUXE, ART_DELUXE,
                            artist="Sabrina Carpenter & Dolly Parton",
                            exclusive=True)
            await self.settle()
        asyncio.run(go())
        self.assertEqual(self.covers(), {b"IMAGE:" + ART_DELUXE.encode()})

    def test_a_track_search_could_not_place_is_swept_up_too(self):
        # "15 Minutes" resolves to nothing at all, so it is filed with no album
        # and whatever cover the recognizer offered. Once the run proves the
        # deluxe, it belongs to it like everything else.
        async def go():
            await self.play_the_side()
            await self.feed("15 Minutes", None, "http://cover/stray.jpg")
            await self.feed("Please Please Please", DELUXE, ART_DELUXE,
                            artist="Sabrina Carpenter & Dolly Parton",
                            exclusive=True)
            await self.settle()
        asyncio.run(go())
        stray = next(r for r in self.rows() if r["title"] == "15 Minutes")
        self.assertEqual(stray["album"], DELUXE)
        self.assertEqual(self.art_bytes(stray), b"IMAGE:" + ART_DELUXE.encode())

    def test_the_upgrade_survives_the_rest_of_the_record(self):
        # Tracks after the proof resolve from the deluxe and must not drag the
        # run back down to the base title.
        async def go():
            await self.play_the_side()
            await self.feed("Please Please Please", DELUXE, ART_DELUXE,
                            artist="Sabrina Carpenter & Dolly Parton",
                            exclusive=True)
            await self.feed("Busy Woman", DELUXE, ART_DELUXE)
            await self.settle()
        asyncio.run(go())
        self.assertEqual(self.albums(), {DELUXE})

    def test_overlapping_settles_do_not_race(self):
        # Every play spawns one of these and a side spawns a dozen, so they
        # overlap constantly. Left to interleave, an older one finishes last and
        # leaves the run showing a cover that was already superseded — which is
        # exactly the symptom this whole change is about. No settle() between
        # feeds here, so they are genuinely in flight together.
        async def go():
            await self.play_the_side()
            await self.feed("Please Please Please", DELUXE, ART_DELUXE,
                            artist="Sabrina Carpenter & Dolly Parton",
                            exclusive=True)
            await self.settle()
        asyncio.run(go())
        self.assertEqual(self.albums(), {DELUXE})
        self.assertEqual(self.covers(), {b"IMAGE:" + ART_DELUXE.encode()})

    # --- and where it must not reach ---

    def test_an_ordinary_side_is_left_on_the_standard_album(self):
        # No proof, no upgrade: a qualifier is usually an artifact of which
        # release iTunes matched, not a fact about the record on the platter.
        async def go():
            await self.play_the_side()
            await self.feed("Juno", DELUXE, ART_DELUXE)   # no exclusive flag
            await self.settle()
        asyncio.run(go())
        self.assertEqual(self.albums(), {BASE})

    def test_a_play_brought_into_line_adopts_the_runs_cover(self):
        # The reverse direction. This play was rewritten to the standard album,
        # so its own deluxe artwork is the one that is now wrong.
        async def go():
            await self.play_the_side()
            await self.settle()
            await self.feed("Juno", DELUXE, ART_DELUXE)
            await self.settle()
        asyncio.run(go())
        juno = next(r for r in self.rows() if r["title"] == "Juno")
        self.assertEqual(juno["album"], BASE)
        self.assertEqual(self.art_bytes(juno), b"IMAGE:" + ART_BASE.encode())

    def test_another_record_in_the_same_session_is_untouched(self):
        # A run is same-artist contiguous plays and can span two records;
        # unifying artwork across that boundary is the bleed reconciliation
        # exists to prevent.
        async def go():
            await self.feed("Nonsense", "emails i can't send",
                            "http://cover/emails.jpg")
            await self.settle()
            await self.play_the_side()
            await self.feed("Please Please Please", DELUXE, ART_DELUXE,
                            artist="Sabrina Carpenter & Dolly Parton",
                            exclusive=True)
            await self.settle()
        asyncio.run(go())
        other = next(r for r in self.rows() if r["title"] == "Nonsense")
        self.assertEqual(other["album"], "emails i can't send")
        self.assertEqual(self.art_bytes(other), b"IMAGE:http://cover/emails.jpg")


if __name__ == "__main__":
    unittest.main()
