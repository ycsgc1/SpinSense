"""Resolving a track against the record we already believe is playing.

A side is one album, so once any track resolves, the rest are answerable from
that album's own tracklist. That matters because iTunes' *search* is
relevance-ranked and was wrong for five plays of one OK ORCHESTRA side: nothing
at all for "OK Overture", two unrelated songs for "3 O'Clock Things", a lullaby
cover for "My Play", and only a live album for "World's Smallest Violin".
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

# Shaped like a real `lookup?entity=song` response, artistName included — every
# entry iTunes returns carries one, and a tracklist that resolves by title alone
# cannot tell two recordings of the same song apart.
OK_ORCHESTRA = [
    {"trackName": "OK Overture", "artistName": "AJR", "trackTimeMillis": 271000,
     "artworkUrl100": "http://a/100x100bb.jpg"},
    {"trackName": "World's Smallest Violin", "artistName": "AJR", "trackTimeMillis": 180000,
     "artworkUrl100": "http://a/100x100bb.jpg"},
    {"trackName": "Christmas in June", "artistName": "AJR", "trackTimeMillis": 279000,
     "artworkUrl100": "http://a/100x100bb.jpg"},
]


class AlbumContextTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig = (itunes.search_songs, itunes.album_tracks)
        core_engine.album_context = None
        core_engine._tracklist_cache.clear()

        self.searches = []
        self.lookups = []
        self.search_result = []

        async def fake_search(artist, title, limit=10, timeout_secs=5.0):
            self.searches.append(title)
            return self.search_result

        async def fake_tracks(collection_id, timeout_secs=8.0):
            self.lookups.append(collection_id)
            return OK_ORCHESTRA

        itunes.search_songs = fake_search
        itunes.album_tracks = fake_tracks

    def tearDown(self):
        itunes.search_songs, itunes.album_tracks = self._orig
        core_engine.album_context = None
        core_engine._tracklist_cache.clear()

    def searched_result(self, album="OK ORCHESTRA", track="Joe", cid=1713833569):
        return [{"trackName": track, "artistName": "AJR", "collectionName": album,
                 "collectionId": cid, "trackTimeMillis": 212000,
                 "artworkUrl100": "http://a/100x100bb.jpg"}]

    async def test_a_resolved_track_establishes_the_record(self):
        self.search_result = self.searched_result()
        album, _, duration, _ = await core_engine.fetch_itunes_metadata("AJR", "Joe")
        self.assertEqual(album, "OK ORCHESTRA")
        self.assertEqual(duration, 212)
        self.assertEqual(core_engine.album_context["id"], 1713833569)

    async def test_later_tracks_come_from_the_tracklist_not_search(self):
        self.search_result = self.searched_result()
        await core_engine.fetch_itunes_metadata("AJR", "Joe")
        self.searches.clear()

        # Search finds nothing for this one; the album knows it as track 1.
        self.search_result = []
        album, _, duration, _ = await core_engine.fetch_itunes_metadata("AJR", "OK Overture")
        self.assertEqual(album, "OK ORCHESTRA")
        self.assertEqual(duration, 271)
        self.assertEqual(self.searches, [])       # never asked search at all

    async def test_the_tracklist_overrides_a_wrong_search_result(self):
        # The reported case: search returns only the live album for a track
        # that is on the studio record we are demonstrably playing.
        self.search_result = self.searched_result()
        await core_engine.fetch_itunes_metadata("AJR", "Joe")
        self.search_result = self.searched_result(
            album="Live from the Hollywood Bowl", track="World's Smallest Violin", cid=999)
        album, _, duration, _ = await core_engine.fetch_itunes_metadata(
            "AJR", "World's Smallest Violin")
        self.assertEqual(album, "OK ORCHESTRA")
        self.assertEqual(duration, 180)           # not the live version's length

    async def test_the_tracklist_is_fetched_once_per_album(self):
        self.search_result = self.searched_result()
        await core_engine.fetch_itunes_metadata("AJR", "Joe")
        for title in ("OK Overture", "Christmas in June", "World's Smallest Violin"):
            await core_engine.fetch_itunes_metadata("AJR", title)
        self.assertEqual(self.lookups, [1713833569])

    async def test_a_track_not_on_the_record_falls_back_to_search(self):
        # This is what lets a different record take over.
        self.search_result = self.searched_result()
        await core_engine.fetch_itunes_metadata("AJR", "Joe")
        self.searches.clear()
        self.search_result = self.searched_result(album="The Maybe Man", track="Yes I'm A Mess",
                                                  cid=555)
        album, _, _, _ = await core_engine.fetch_itunes_metadata("AJR", "Yes I'm A Mess")
        self.assertEqual(album, "The Maybe Man")
        self.assertEqual(self.searches, ["Yes I'm A Mess"])
        self.assertEqual(core_engine.album_context["id"], 555)   # context moved

    async def test_a_stale_context_is_not_trusted(self):
        # Yesterday's record must not answer for today's.
        self.search_result = self.searched_result()
        await core_engine.fetch_itunes_metadata("AJR", "Joe")
        core_engine.album_context["at"] -= core_engine.ALBUM_CONTEXT_TTL_SECS + 1
        self.searches.clear()
        self.search_result = []
        album, _, _, _ = await core_engine.fetch_itunes_metadata("AJR", "OK Overture")
        self.assertIsNone(album)
        self.assertEqual(self.searches, ["OK Overture"])

    async def test_playing_the_side_keeps_the_context_alive(self):
        self.search_result = self.searched_result()
        await core_engine.fetch_itunes_metadata("AJR", "Joe")
        core_engine.album_context["at"] -= core_engine.ALBUM_CONTEXT_TTL_SECS - 5
        await core_engine.fetch_itunes_metadata("AJR", "OK Overture")
        # That confirmation refreshed it, so the next track is still covered.
        self.searches.clear()
        self.search_result = []
        album, _, _, _ = await core_engine.fetch_itunes_metadata("AJR", "Christmas in June")
        self.assertEqual(album, "OK ORCHESTRA")
        self.assertEqual(self.searches, [])

    async def test_an_unavailable_tracklist_is_only_asked_for_once(self):
        # Otherwise an album iTunes cannot expand would be re-requested for
        # every track for the next half hour.
        async def no_tracks(collection_id, timeout_secs=8.0):
            self.lookups.append(collection_id)
            return []

        itunes.album_tracks = no_tracks
        self.search_result = self.searched_result()
        await core_engine.fetch_itunes_metadata("AJR", "Joe")
        for title in ("Bang!", "The Trick", "Humpty Dumpty"):
            self.search_result = self.searched_result(track=title)
            await core_engine.fetch_itunes_metadata("AJR", title)
        self.assertEqual(self.lookups, [1713833569])

    async def test_an_empty_tracklist_does_not_wedge_the_context(self):
        async def no_tracks(collection_id, timeout_secs=8.0):
            self.lookups.append(collection_id)
            return []

        itunes.album_tracks = no_tracks
        self.search_result = self.searched_result()
        await core_engine.fetch_itunes_metadata("AJR", "Joe")
        self.search_result = self.searched_result(track="Bang!")
        album, _, _, _ = await core_engine.fetch_itunes_metadata("AJR", "Bang!")
        self.assertEqual(album, "OK ORCHESTRA")   # search still answers


if __name__ == "__main__":
    unittest.main()
