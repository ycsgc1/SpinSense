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

ARTIST_ID = 390647681

ART_BASE = "http://cover/base/100x100bb.jpg"
ART_DELUXE = "http://cover/deluxe/100x100bb.jpg"

BASE_ID, DELUXE_ID = 1752214909, 1795512297
OTHER_ID = 1631578046      # a different record entirely
SINGLE_ID = 1752767050

# Twelve tracks, and one "Please Please Please" — the solo recording.
BASE_TRACKS = [
    {"trackName": "Taste", "artistName": "Sabrina Carpenter",
     "artistId": ARTIST_ID, "trackTimeMillis": 157000, "artworkUrl100": ART_BASE},
    {"trackName": "Please Please Please", "artistName": "Sabrina Carpenter",
     "artistId": ARTIST_ID, "trackTimeMillis": 186000, "artworkUrl100": ART_BASE},
    {"trackName": "Espresso", "artistName": "Sabrina Carpenter",
     "artistId": ARTIST_ID, "trackTimeMillis": 175000, "artworkUrl100": ART_BASE},
]
# Seventeen, and two "Please Please Please" — solo at 2, the duet at 14.
DELUXE_TRACKS = BASE_TRACKS + [
    {"trackName": "15 Minutes", "artistName": "Sabrina Carpenter",
     "artistId": ARTIST_ID, "trackTimeMillis": 148000, "artworkUrl100": ART_DELUXE},
    {"trackName": "Please Please Please",
     "artistName": "Sabrina Carpenter & Dolly Parton",
     "artistId": ARTIST_ID, "trackTimeMillis": 194000, "artworkUrl100": ART_DELUXE},
]

# A different record that happens to carry a track of the same name. Artists
# re-record and reuse titles, so "this artist has a song called that" is not
# evidence about which pressing is on the platter.
OTHER_ALBUM_TRACKS = [
    {"trackName": "15 Minutes", "artistName": "Sabrina Carpenter",
     "artistId": ARTIST_ID, "trackTimeMillis": 99000, "artworkUrl100": ART_BASE},
]

# What `lookup?id=<artist>&entity=album` returns: a career's worth of releases,
# only one of which is another edition of the record on the platter.
ARTIST_RELEASES = [
    {"collectionId": BASE_ID, "collectionName": "Short n' Sweet"},
    {"collectionId": DELUXE_ID, "collectionName": "Short n' Sweet (Deluxe)"},
    {"collectionId": OTHER_ID, "collectionName": "emails i can't send"},
    {"collectionId": 1746800651, "collectionName": "Espresso EP"},
    {"collectionId": SINGLE_ID, "collectionName": "Please Please Please - Single"},
]


class DeluxeBonusTrackTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig = (itunes.search_songs, itunes.album_tracks, itunes.artist_albums)
        core_engine.album_context = None
        core_engine._tracklist_cache.clear()
        core_engine._artist_albums_cache.clear()
        self.searches = []

        async def fake_search(artist, title, limit=10, timeout_secs=5.0):
            self.searches.append((artist, title))
            return self.results

        async def fake_tracks(collection_id, timeout_secs=8.0):
            self.tracklist_lookups.append(collection_id)
            return self.tracklists.get(collection_id, [])

        async def fake_artist_albums(artist_id, timeout_secs=8.0):
            self.artist_lookups.append(artist_id)
            return self.releases

        itunes.search_songs = fake_search
        itunes.album_tracks = fake_tracks
        itunes.artist_albums = fake_artist_albums
        self.results = []
        self.releases = ARTIST_RELEASES
        self.artist_lookups = []
        self.tracklist_lookups = []
        # Addressable per collection, so a test can put a same-named track on a
        # record that is *not* an edition of the one playing and prove it is
        # still refused. A shared fallback list would make that unprovable.
        self.tracklists = {
            BASE_ID: BASE_TRACKS,
            DELUXE_ID: DELUXE_TRACKS,
            OTHER_ID: OTHER_ALBUM_TRACKS,
            SINGLE_ID: OTHER_ALBUM_TRACKS,
        }

    def tearDown(self):
        (itunes.search_songs, itunes.album_tracks,
         itunes.artist_albums) = self._orig
        core_engine.album_context = None
        core_engine._tracklist_cache.clear()
        core_engine._artist_albums_cache.clear()

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


class BonusTrackSearchCannotPlaceTest(DeluxeBonusTrackTest):
    """The bonus tracks iTunes' song search does not know exist.

    Searching for "15 Minutes", "Busy Woman" or "Couldn't Make It Any Harder"
    returns nothing usable, and an album search for the record's own name does
    not list the deluxe either — so three tracks of a seventeen-track record
    were unresolvable, and the reported session upgraded only when it happened
    to reach "Bad Reviews", the one bonus track search does know. Listed under
    the *artist*, the deluxe is right there.

    This is also the original rule stated outright: a song that is not on the
    standard pressing means the pressing is not the standard one.
    """

    async def test_a_track_search_cannot_place_is_found_on_the_deluxe(self):
        await self.establish_the_standard_album()
        self.results = []
        album, art, duration, _ = await core_engine.fetch_itunes_metadata(
            "Sabrina Carpenter", "15 Minutes")
        self.assertEqual(album, "Short n' Sweet (Deluxe)")
        self.assertEqual(duration, 148)
        self.assertIn(ART_DELUXE.replace("100x100bb", "1000x1000bb"), art)

    async def test_it_proves_the_edition(self):
        # The whole point: the first deluxe-only track upgrades the session,
        # rather than the session waiting for one search happens to know.
        await self.establish_the_standard_album()
        self.results = []
        *_, exclusive = await core_engine.fetch_itunes_metadata(
            "Sabrina Carpenter", "15 Minutes")
        self.assertTrue(exclusive)

    async def test_the_record_becomes_the_deluxe_from_then_on(self):
        await self.establish_the_standard_album()
        self.results = []
        await core_engine.fetch_itunes_metadata("Sabrina Carpenter", "15 Minutes")
        self.assertEqual(core_engine.album_context["id"], DELUXE_ID)

    async def test_the_artist_is_asked_only_after_search_fails(self):
        # It returns a whole career, so it must never be on the ordinary path.
        await self.establish_the_standard_album()
        self.results = self.solo_result()
        await core_engine.fetch_itunes_metadata("Sabrina Carpenter", "Espresso")
        self.assertEqual(self.artist_lookups, [])

    async def test_the_artist_is_asked_once_however_many_tracks_miss(self):
        await self.establish_the_standard_album()
        self.results = []
        await core_engine.fetch_itunes_metadata("Sabrina Carpenter", "15 Minutes")
        await core_engine.fetch_itunes_metadata("Sabrina Carpenter", "Nothing At All")
        self.assertEqual(self.artist_lookups, [ARTIST_ID])

    async def test_a_track_on_no_edition_stays_unknown(self):
        # Honest ignorance beats attributing a stray play to this record.
        await self.establish_the_standard_album()
        self.results = []
        album, _art, duration, exclusive = await core_engine.fetch_itunes_metadata(
            "Sabrina Carpenter", "Some Other Song Entirely")
        self.assertIsNone(album)
        self.assertIsNone(duration)
        self.assertFalse(exclusive)

    async def test_other_records_by_the_artist_are_not_considered(self):
        # A career of 77 releases; only editions of *this* record may answer.
        await self.establish_the_standard_album()
        self.results = []
        self.releases = [
            {"collectionId": OTHER_ID, "collectionName": "emails i can't send"},
        ]
        album, *_ = await core_engine.fetch_itunes_metadata(
            "Sabrina Carpenter", "15 Minutes")
        self.assertIsNone(album)

    async def test_a_single_named_for_the_record_is_not_an_edition(self):
        # A single carrying the album's own name, and a track of the right
        # title on it. "- Single" is not an edition qualifier, so it is not a
        # pressing of the album and cannot say which one is playing.
        await self.establish_the_standard_album()
        self.results = []
        self.releases = [
            {"collectionId": SINGLE_ID, "collectionName": "Short n' Sweet - Single"},
        ]
        album, *_ = await core_engine.fetch_itunes_metadata(
            "Sabrina Carpenter", "15 Minutes")
        self.assertIsNone(album)

    async def test_the_plainest_edition_that_has_the_track_wins(self):
        # Never claim a super deluxe when an ordinary deluxe accounts for it.
        await self.establish_the_standard_album()
        self.results = []
        self.releases = [
            {"collectionId": 4242, "collectionName": "Short n' Sweet (Super Deluxe)"},
            {"collectionId": DELUXE_ID, "collectionName": "Short n' Sweet (Deluxe)"},
        ]
        self.tracklists[4242] = DELUXE_TRACKS
        album, *_ = await core_engine.fetch_itunes_metadata(
            "Sabrina Carpenter", "15 Minutes")
        self.assertEqual(album, "Short n' Sweet (Deluxe)")

    async def test_nothing_is_asked_when_no_record_is_playing(self):
        # With no context there is no record whose editions could be checked.
        core_engine.album_context = None
        self.results = []
        album, *_ = await core_engine.fetch_itunes_metadata(
            "Sabrina Carpenter", "15 Minutes")
        self.assertIsNone(album)
        self.assertEqual(self.artist_lookups, [])


if __name__ == "__main__":
    unittest.main()
