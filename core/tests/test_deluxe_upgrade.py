"""The bonus track that has to reach the reconciler.

Reported from a full side of *Short n' Sweet*: the last two tracks played are
deluxe-only, and both were filed under the standard album — so nothing ever
upgraded the session, and the covers stayed the standard pressing's.

The cause was a shortcut. Once a record is playing, tracks resolve from its
tracklist instead of from search, and that lookup matched on title alone. A
deluxe edition routinely carries two recordings of one song — *Short n' Sweet
(Deluxe)* has "Please Please Please" at track 2 solo and again at track 14 with
Dolly Parton — so the duet resolved against the standard album's track 2. Wrong
duration, wrong artwork, and, worst of all, the evidence destroyed: "this track
is not on the record we thought was playing" is precisely what upgrades a run.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

import core_engine  # noqa: E402
from spinsense import itunes  # noqa: E402

ART_BASE = "http://cover/base/100x100bb.jpg"
ART_DELUXE = "http://cover/deluxe/100x100bb.jpg"

BASE_ID, DELUXE_ID = 1752214909, 1795512297

# Twelve tracks, and one "Please Please Please" — the solo recording.
BASE_TRACKS = [
    {"trackName": "Taste", "artistName": "Sabrina Carpenter",
     "trackTimeMillis": 157000, "artworkUrl100": ART_BASE},
    {"trackName": "Please Please Please", "artistName": "Sabrina Carpenter",
     "trackTimeMillis": 186000, "artworkUrl100": ART_BASE},
    {"trackName": "Espresso", "artistName": "Sabrina Carpenter",
     "trackTimeMillis": 175000, "artworkUrl100": ART_BASE},
]
# Seventeen, and two "Please Please Please" — solo at 2, the duet at 14.
DELUXE_TRACKS = BASE_TRACKS + [
    {"trackName": "15 Minutes", "artistName": "Sabrina Carpenter",
     "trackTimeMillis": 148000, "artworkUrl100": ART_DELUXE},
    {"trackName": "Please Please Please",
     "artistName": "Sabrina Carpenter & Dolly Parton",
     "trackTimeMillis": 194000, "artworkUrl100": ART_DELUXE},
]


class DeluxeBonusTrackTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig = (itunes.search_songs, itunes.album_tracks)
        core_engine.album_context = None
        core_engine._tracklist_cache.clear()
        self.searches = []

        async def fake_search(artist, title, limit=10, timeout_secs=5.0):
            self.searches.append((artist, title))
            return self.results

        async def fake_tracks(collection_id, timeout_secs=8.0):
            return DELUXE_TRACKS if collection_id == DELUXE_ID else BASE_TRACKS

        itunes.search_songs = fake_search
        itunes.album_tracks = fake_tracks
        self.results = []

    def tearDown(self):
        itunes.search_songs, itunes.album_tracks = self._orig
        core_engine.album_context = None
        core_engine._tracklist_cache.clear()

    def solo_result(self):
        return [{"trackName": "Taste", "artistName": "Sabrina Carpenter",
                 "collectionName": "Short n' Sweet", "collectionId": BASE_ID,
                 "trackTimeMillis": 157000, "artworkUrl100": ART_BASE}]

    def duet_result(self):
        # What iTunes really answers: the duet exists only on the deluxe.
        return [{"trackName": "Please Please Please",
                 "artistName": "Sabrina Carpenter & Dolly Parton",
                 "collectionName": "Short n' Sweet (Deluxe)",
                 "collectionId": DELUXE_ID,
                 "trackTimeMillis": 194000, "artworkUrl100": ART_DELUXE}]

    async def establish_the_standard_album(self):
        self.results = self.solo_result()
        await core_engine.fetch_itunes_metadata("Sabrina Carpenter", "Taste")
        self.assertEqual(core_engine.album_context["name"], "Short n' Sweet")
        self.searches.clear()

    async def test_the_duet_is_not_answered_from_the_standard_album(self):
        await self.establish_the_standard_album()
        self.results = self.duet_result()
        album, art, duration, exclusive = await core_engine.fetch_itunes_metadata(
            "Sabrina Carpenter & Dolly Parton", "Please Please Please")
        self.assertEqual(album, "Short n' Sweet (Deluxe)")
        self.assertEqual(duration, 194)
        self.assertIn(ART_DELUXE.replace("100x100bb", "1000x1000bb"), art)

    async def test_the_duet_proves_the_deluxe(self):
        # The flag the reconciler acts on. Without it the run never upgrades.
        await self.establish_the_standard_album()
        self.results = self.duet_result()
        *_, exclusive = await core_engine.fetch_itunes_metadata(
            "Sabrina Carpenter & Dolly Parton", "Please Please Please")
        self.assertTrue(exclusive)

    async def test_a_track_not_on_the_record_is_looked_up(self):
        # The shortcut has to stand aside, or search never runs and the album
        # is silently wrong rather than merely unknown.
        await self.establish_the_standard_album()
        self.results = self.duet_result()
        await core_engine.fetch_itunes_metadata(
            "Sabrina Carpenter & Dolly Parton", "Please Please Please")
        self.assertEqual(self.searches,
                         [("Sabrina Carpenter & Dolly Parton", "Please Please Please")])

    async def test_the_record_becomes_the_deluxe(self):
        # So the tracks after it resolve from the 17-track list, not the 12.
        await self.establish_the_standard_album()
        self.results = self.duet_result()
        await core_engine.fetch_itunes_metadata(
            "Sabrina Carpenter & Dolly Parton", "Please Please Please")
        self.assertEqual(core_engine.album_context["id"], DELUXE_ID)

    async def test_a_deluxe_only_track_then_resolves_without_a_search(self):
        await self.establish_the_standard_album()
        self.results = self.duet_result()
        await core_engine.fetch_itunes_metadata(
            "Sabrina Carpenter & Dolly Parton", "Please Please Please")
        self.searches.clear()

        self.results = []          # search would find nothing for this one
        album, _art, duration, _ = await core_engine.fetch_itunes_metadata(
            "Sabrina Carpenter", "15 Minutes")
        self.assertEqual(album, "Short n' Sweet (Deluxe)")
        self.assertEqual(duration, 148)
        self.assertEqual(self.searches, [])

    async def test_the_solo_recording_still_resolves_from_the_record(self):
        # The artist check must not cost the shortcut its ordinary job.
        await self.establish_the_standard_album()
        self.results = []          # any search here would be a failure
        album, _art, duration, exclusive = await core_engine.fetch_itunes_metadata(
            "Sabrina Carpenter", "Please Please Please")
        self.assertEqual(album, "Short n' Sweet")
        self.assertEqual(duration, 186)
        self.assertFalse(exclusive)
        self.assertEqual(self.searches, [])

    async def test_the_duet_is_not_confused_for_the_solo_on_the_deluxe(self):
        # Both recordings live on the deluxe, so this is not only a
        # cross-edition problem: within one tracklist, title alone is ambiguous.
        self.results = self.duet_result()
        await core_engine.fetch_itunes_metadata(
            "Sabrina Carpenter & Dolly Parton", "Please Please Please")
        self.searches.clear()
        self.results = []

        solo = await core_engine.fetch_itunes_metadata(
            "Sabrina Carpenter", "Please Please Please")
        duet = await core_engine.fetch_itunes_metadata(
            "Sabrina Carpenter & Dolly Parton", "Please Please Please")
        self.assertEqual(solo[2], 186)
        self.assertEqual(duet[2], 194)
        self.assertEqual(self.searches, [])


if __name__ == "__main__":
    unittest.main()
