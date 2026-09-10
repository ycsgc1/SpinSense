"""A re-recording is not the record it re-records.

From a listening session: a full side of *1989 (Taylor's Version)* came out
with its first thirteen plays filed under *1989* and the last eight under
*1989 (Taylor's Version)* — two albums, two covers, and that is what went to
Last.fm.

`track_key` strips one trailing qualifier so the two catalogues can agree
through "(feat. X)" and "- 2019 Remaster". That tolerance is right for those
and wrong for "(Taylor's Version)", which does not decorate a title — it names
a different performance. Stripping it let the original album's tracklist answer
for the re-recording, and the album context then claimed the whole side.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from spinsense import albums, itunes  # noqa: E402

TS = "Taylor Swift"


def entry(name, artist=TS):
    return {"trackName": name, "artistName": artist}


class RenditionMarkerTest(unittest.TestCase):
    def test_a_plain_title_claims_nothing(self):
        self.assertEqual(albums.rendition_markers("Blank Space"), frozenset())

    def test_a_re_recording_says_so(self):
        self.assertIn("taylorsversion",
                      albums.rendition_markers("Blank Space (Taylor's Version)"))

    def test_a_curly_apostrophe_reads_the_same(self):
        # The two catalogues disagree about apostrophes constantly.
        self.assertEqual(albums.rendition_markers("This Love (Taylor's Version)"),
                         albums.rendition_markers("This Love (Taylor’s Version)"))

    def test_stacked_qualifiers_are_all_read(self):
        self.assertIn("taylorsversion", albums.rendition_markers(
            "Is It Over Now? (Taylor's Version) [From The Vault]"))

    def test_a_live_recording_says_so(self):
        self.assertIn("live", albums.rendition_markers(
            "A Bunch of Songs (Live from the Hollywood Bowl)"))

    def test_a_song_merely_named_live_claims_nothing(self):
        # Read from trailing qualifiers only, so the title itself is safe.
        self.assertEqual(albums.rendition_markers("Live and Let Die"), frozenset())
        self.assertEqual(albums.rendition_markers("Alive"), frozenset())

    def test_an_edition_qualifier_is_not_a_rendition(self):
        self.assertEqual(albums.rendition_markers("Bang! (Deluxe Edition)"), frozenset())


class SameRecordingTest(unittest.TestCase):
    def test_a_re_recording_is_not_the_original(self):
        self.assertFalse(albums.same_recording(
            "Blank Space (Taylor's Version)", "Blank Space"))

    def test_a_re_recording_is_itself(self):
        self.assertTrue(albums.same_recording(
            "Blank Space (Taylor's Version)", "Blank Space (Taylor's Version)"))

    def test_a_plain_title_may_still_be_the_re_recording(self):
        # Deliberately one-directional: recognisers frequently report the plain
        # title for a re-recorded track, and refusing there would put the right
        # album out of reach for the rest of the side.
        self.assertTrue(albums.same_recording(
            "Welcome To New York", "Welcome To New York (Taylor's Version)"))

    def test_two_different_renditions_never_match(self):
        self.assertFalse(albums.same_recording("Karma (Live)", "Karma (Acoustic)"))


class TracklistRespectsRenditionTest(unittest.TestCase):
    """The shortcut that caused it: once a record is playing, tracks resolve
    from its tracklist instead of from search."""

    ORIGINAL = [entry("Blank Space"), entry("Style"), entry("Shake It Off")]
    TAYLORS = [entry("Blank Space (Taylor's Version)"),
               entry("Style (Taylor's Version)"),
               entry("Shake It Off (Taylor's Version)")]

    def test_the_original_cannot_answer_for_the_re_recording(self):
        self.assertIsNone(itunes.find_track(
            self.ORIGINAL, "Blank Space (Taylor's Version)", TS))

    def test_the_re_recording_answers_for_itself(self):
        self.assertIsNotNone(itunes.find_track(
            self.TAYLORS, "Blank Space (Taylor's Version)", TS))

    def test_the_re_recording_answers_a_plain_title(self):
        # "Welcome To New York" arrived with no qualifier; once the record is
        # known to be the re-recording, it belongs to it.
        self.assertIsNotNone(itunes.find_track(self.TAYLORS, "Blank Space", TS))

    def test_the_original_still_answers_a_plain_title(self):
        self.assertIsNotNone(itunes.find_track(self.ORIGINAL, "Blank Space", TS))


class SearchResultsRespectRenditionTest(unittest.TestCase):
    def results(self):
        return [
            {"trackName": "Blank Space", "artistName": TS, "collectionName": "1989"},
            {"trackName": "Blank Space (Taylor's Version)", "artistName": TS,
             "collectionName": "1989 (Taylor's Version)"},
        ]

    def test_asking_about_the_re_recording_excludes_the_original(self):
        hits = itunes.results_for_track(
            self.results(), "Blank Space (Taylor's Version)", TS)
        self.assertEqual([h["collectionName"] for h in hits],
                         ["1989 (Taylor's Version)"])

    def test_asking_plainly_still_sees_both(self):
        hits = itunes.results_for_track(self.results(), "Blank Space", TS)
        self.assertEqual(len(hits), 2)

    def test_the_album_chosen_follows(self):
        hits = itunes.results_for_track(
            self.results(), "Blank Space (Taylor's Version)", TS)
        chosen, _exclusive = albums.choose_edition(itunes.album_names(hits))
        self.assertEqual(chosen, "1989 (Taylor's Version)")


class RecordingBaseTest(unittest.TestCase):
    def test_a_re_recording_reduces_to_the_record_it_re_records(self):
        self.assertEqual(albums.recording_base("1989 (Taylor's Version)"), "1989")

    def test_stacked_qualifiers_all_come_off(self):
        self.assertEqual(
            albums.recording_base("1989 (Taylor's Version) [Deluxe]"), "1989")

    def test_the_plain_record_is_its_own_base(self):
        self.assertEqual(albums.recording_base("1989"), "1989")
        self.assertTrue(albums.is_recording_base_form("1989"))

    def test_a_rendition_is_not_a_base_form(self):
        self.assertFalse(albums.is_recording_base_form("1989 (Taylor's Version)"))
        self.assertFalse(albums.is_recording_base_form("Abbey Road (Live)"))

    def test_base_title_still_keeps_them_apart(self):
        # recording_base is the looser reading, used only to ask whether two
        # titles name the same underlying record. The stricter one still
        # refuses to merge a re-recording into the original.
        self.assertNotEqual(albums.base_title("1989 (Taylor's Version)"),
                            albums.base_title("1989"))


if __name__ == "__main__":
    unittest.main()
