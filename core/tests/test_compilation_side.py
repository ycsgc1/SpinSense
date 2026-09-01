"""A side whose tracks each belong to a different record.

From a stress test: AJR's *Live from the Hollywood Bowl*. Every track's
catalogue home is a different studio album, so the ordinary lookup answered
OK ORCHESTRA, then Neotheater, then The Click, then nothing at all — four
albums for one side, with studio durations driving the play clock and one song
("Yes I'm A Mess") never identified because Shazam couldn't hear it through the
applause.

The ordinary question — "which album is this track from?" — has no useful
answer on a live album or a compilation. The useful question is "which single
release holds *all* the tracks I have heard?", which iTunes answers: the live
album held 4 of 4 where every studio album held 1.
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

ARTIST_ID = 359553651
LIVE_ID, OKO_ID, NEO_ID, CLICK_ID, SINGLE_ID = 1868793244, 1713833569, 101, 102, 103
ART = "http://cover/x/100x100bb.jpg"


def t(name, secs, artist="AJR"):
    return {"trackName": name, "artistName": artist, "artistId": ARTIST_ID,
            "trackTimeMillis": secs * 1000, "artworkUrl100": ART}


LIVE_TRACKS = [
    t("Way Less Sad (Live from the Hollywood Bowl)", 209),
    t("Karma (Live from the Hollywood Bowl)", 293),
    t("Yes I'm A Mess (Live from the Hollywood Bowl)", 164),
    t("The Good Part (Live from the Hollywood Bowl)", 195),
    t("The Big Goodbye (Live from the Hollywood Bowl)", 342),
]
TRACKLISTS = {
    LIVE_ID: LIVE_TRACKS,
    OKO_ID: [t("Way Less Sad", 206), t("Bummerland", 200)],
    NEO_ID: [t("Karma", 245), t("100 Bad Days", 199)],
    CLICK_ID: [t("The Good Part", 227), t("Weak", 201)],
    SINGLE_ID: [t("Way Less Sad", 206), t("Way Less Sad (Cash Cash Remix)", 164)],
}
RELEASES = [
    {"collectionId": OKO_ID, "collectionName": "OK ORCHESTRA", "trackCount": 2},
    {"collectionId": NEO_ID, "collectionName": "Neotheater", "trackCount": 2},
    {"collectionId": CLICK_ID, "collectionName": "The Click", "trackCount": 2},
    {"collectionId": LIVE_ID, "collectionName": "Live from the Hollywood Bowl",
     "trackCount": 6},
    {"collectionId": SINGLE_ID, "collectionName": "Way Less Sad (Cash Cash Remix) - Single",
     "trackCount": 2},
]
# What a search returns for each title: (collectionName, collectionId, secs).
SEARCH = {
    "Way Less Sad": [("OK ORCHESTRA", OKO_ID, 206),
                     ("Way Less Sad (Cash Cash Remix) - Single", SINGLE_ID, 206)],
    "Karma": [("Neotheater", NEO_ID, 245),
              ("Live from the Hollywood Bowl", LIVE_ID, 293)],
    "The Good Part": [("The Click", CLICK_ID, 227),
                      ("Live from the Hollywood Bowl", LIVE_ID, 195)],
    "The Big Goodbye": [],          # search cannot place it at all
    "Yes I'm A Mess": [],
}


class CompilationSideTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig = (itunes.search_songs, itunes.album_tracks, itunes.artist_albums)
        core_engine.album_context = None
        core_engine._tracklist_cache.clear()
        core_engine._artist_albums_cache.clear()
        core_engine._artist_ids.clear()
        core_engine.side_tracks.clear()
        self.probed = []

        async def fake_search(artist, title, limit=10, timeout_secs=5.0):
            # artistId included because real search results always carry one,
            # and the release hunt needs it to reach the artist's catalogue.
            return [{"trackName": title, "artistName": "AJR", "collectionName": c,
                     "collectionId": cid, "artistId": ARTIST_ID,
                     "trackTimeMillis": secs * 1000, "artworkUrl100": ART}
                    for c, cid, secs in SEARCH.get(title, [])]

        async def fake_tracks(collection_id, timeout_secs=8.0):
            self.probed.append(collection_id)
            return TRACKLISTS.get(collection_id, [])

        async def fake_artist_albums(artist_id, timeout_secs=8.0):
            return self.releases

        itunes.search_songs = fake_search
        itunes.album_tracks = fake_tracks
        itunes.artist_albums = fake_artist_albums
        self.releases = RELEASES

    def tearDown(self):
        (itunes.search_songs, itunes.album_tracks, itunes.artist_albums) = self._orig
        core_engine.album_context = None
        core_engine._tracklist_cache.clear()
        core_engine._artist_albums_cache.clear()
        core_engine._artist_ids.clear()
        core_engine.side_tracks.clear()

    async def play(self, *titles):
        out = []
        for title in titles:
            out.append(await core_engine.fetch_itunes_metadata("AJR", title))
        return out

    # --- finding the record ---

    async def test_the_first_track_still_looks_like_its_studio_album(self):
        # Nothing else has been heard yet, so there is no evidence to weigh.
        (album, _art, dur, _x), = await self.play("Way Less Sad")
        self.assertEqual(album, "OK ORCHESTRA")
        self.assertEqual(dur, 206)

    async def test_the_second_track_settles_the_record(self):
        _first, second = await self.play("Way Less Sad", "Karma")
        self.assertEqual(second[0], "Live from the Hollywood Bowl")

    async def test_it_uses_the_live_duration_not_the_studio_one(self):
        # The whole point: 293s, not 245s. The studio length fired the end-check
        # 23 seconds before the track had finished, three times over.
        _first, second = await self.play("Way Less Sad", "Karma")
        self.assertEqual(second[1] and second[2], 293)

    async def test_the_record_sticks_for_the_rest_of_the_side(self):
        results = await self.play("Way Less Sad", "Karma", "The Good Part")
        self.assertEqual(results[2][0], "Live from the Hollywood Bowl")
        self.assertEqual(results[2][2], 195)

    async def test_a_track_search_cannot_place_resolves_from_the_record(self):
        results = await self.play("Way Less Sad", "Karma", "The Big Goodbye")
        self.assertEqual(results[2][0], "Live from the Hollywood Bowl")
        self.assertEqual(results[2][2], 342)

    async def test_the_side_reads_as_one_record(self):
        results = await self.play("Way Less Sad", "Karma", "The Good Part",
                                  "The Big Goodbye")
        self.assertEqual({r[0] for r in results[1:]}, {"Live from the Hollywood Bowl"})

    async def test_a_track_that_never_resolved_keeps_the_question_open(self):
        # From the field: "A Bunch of Songs We Haven't Played In a Long Time"
        # resolved to nothing, then the next track resolved cleanly to the album
        # we already believed in — so there was no *disagreement* to notice, and
        # the record went unfound. An unresolved track is a standing question.
        results = await self.play("The Big Goodbye", "Karma")
        self.assertEqual(results[1][0], "Live from the Hollywood Bowl")

    async def test_the_unresolved_track_is_not_left_behind(self):
        # It was filed as unknown at the time; once the record is found, the
        # album it belongs to is knowable and the run reconciler backfills it.
        await self.play("The Big Goodbye", "Karma")
        again = await self.play("The Big Goodbye")
        self.assertEqual(again[0][0], "Live from the Hollywood Bowl")
        self.assertEqual(again[0][2], 342)

    # --- and where it must not reach ---

    async def test_one_track_is_never_enough(self):
        await self.play("Karma")
        self.assertEqual(core_engine.album_context["name"], "Neotheater")

    async def test_the_first_track_of_a_new_side_costs_no_release_hunt(self):
        # The case the two-track minimum actually guards. After a side ends the
        # record we believed in is still remembered, so the next side's first
        # track disagrees with it — and one track is never evidence about which
        # release is playing, only an invitation to scan a whole discography.
        await self.play("Way Less Sad")
        core_engine._end_of_side()
        self.asked = []
        orig = itunes.artist_albums

        async def counting(artist_id, timeout_secs=8.0):
            self.asked.append(artist_id)
            return self.releases

        itunes.artist_albums = counting
        try:
            results = await self.play("Karma")
            self.assertEqual(self.asked, [])
            self.assertEqual(results[0][0], "Neotheater")
        finally:
            itunes.artist_albums = orig

    async def test_a_stop_between_records_prevents_a_false_match(self):
        # Two studio albums played back to back, where a live album happens to
        # hold one song from each. Pooling them would "find" the live album and
        # relabel both. Changing a record costs enough silence to stop the
        # engine, and that stop ends the side — a live side never produces any.
        await self.play("Way Less Sad")
        core_engine._end_of_side()               # what the silence-stop calls
        core_engine.album_context = None
        results = await self.play("The Good Part")
        self.assertEqual(results[0][0], "The Click")

    async def test_the_end_of_a_side_forgets_what_was_on_it(self):
        await self.play("Way Less Sad")
        self.assertTrue(core_engine.side_tracks)
        core_engine._end_of_side()
        self.assertEqual(core_engine.side_tracks, [])

    async def test_without_a_stop_a_shared_release_is_taken_as_the_record(self):
        # The other side of that trade, stated plainly: two tracks heard with no
        # silence between them, both on one release, is the live-album signature.
        results = await self.play("Way Less Sad", "The Good Part")
        self.assertEqual(results[1][0], "Live from the Hollywood Bowl")

    async def test_an_ambiguous_answer_is_refused(self):
        # Two unrelated releases both holding everything is not evidence.
        self.releases = RELEASES + [
            {"collectionId": 999, "collectionName": "Greatest Hits", "trackCount": 5},
        ]
        TRACKLISTS[999] = LIVE_TRACKS
        try:
            results = await self.play("Way Less Sad", "Karma")
            self.assertEqual(results[1][0], "Neotheater")
        finally:
            TRACKLISTS.pop(999, None)

    async def test_two_editions_of_one_record_are_not_ambiguous(self):
        # "The Click" and "The Click (Deluxe Edition)" are the same answer, and
        # the plainer title wins rather than the pair cancelling out.
        self.releases = RELEASES + [
            {"collectionId": 998, "collectionName": "Live from the Hollywood Bowl (Deluxe)",
             "trackCount": 5},
        ]
        TRACKLISTS[998] = LIVE_TRACKS
        try:
            results = await self.play("Way Less Sad", "Karma")
            self.assertEqual(results[1][0], "Live from the Hollywood Bowl")
        finally:
            TRACKLISTS.pop(998, None)

    async def test_a_release_too_short_to_hold_them_is_never_fetched(self):
        # The track-count bound prunes without a request. It is also what keeps
        # 7" singles workable: the bound is however many tracks have been heard,
        # so a two-track single stays a candidate until a third track rules it
        # out, rather than being filtered out for being a single.
        self.releases = [
            {"collectionId": 500, "collectionName": "Some Single", "trackCount": 1},
        ] + RELEASES
        await self.play("Way Less Sad", "Karma")
        self.assertNotIn(500, self.probed)

    async def test_a_two_track_single_is_still_a_candidate_for_a_seven_inch(self):
        # A 7" is two tracks, and both are on the single. Nothing about this
        # path excludes it for being a single.
        self.releases = [
            {"collectionId": 700, "collectionName": "A Side / B Side - Single",
             "trackCount": 2},
        ]
        TRACKLISTS[700] = [t("A Side", 180), t("B Side", 175)]
        SEARCH["A Side"] = [("Some Album", 800, 180)]
        SEARCH["B Side"] = [("Other Album", 801, 175)]
        TRACKLISTS[800] = [t("A Side", 180)]
        TRACKLISTS[801] = [t("B Side", 175)]
        try:
            results = await self.play("A Side", "B Side")
            self.assertEqual(results[1][0], "A Side / B Side - Single")
        finally:
            for k in (700, 800, 801):
                TRACKLISTS.pop(k, None)
            SEARCH.pop("A Side", None)
            SEARCH.pop("B Side", None)


if __name__ == "__main__":
    unittest.main()
