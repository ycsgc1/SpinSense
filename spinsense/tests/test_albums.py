"""Edition evidence: deciding which album a track belongs to, and when a track
proves the record on the platter is a particular edition.

The rule, restated: assume the base album, because a qualifier is usually an
artifact of which release iTunes happened to match. But a track that exists
*only* on the deluxe is not an artifact — it is proof, and the whole listening
session can be upgraded on it.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from spinsense import albums, itunes  # noqa: E402


class BaseFormTest(unittest.TestCase):
    def test_a_plain_title_is_a_base_form(self):
        self.assertTrue(albums.is_base_form("SOUR"))

    def test_an_edition_qualifier_disqualifies_it(self):
        self.assertFalse(albums.is_base_form("SOUR (Deluxe)"))
        self.assertFalse(albums.is_base_form("Rumours (Super Deluxe Edition)"))
        self.assertFalse(albums.is_base_form("Abbey Road (Remastered)"))

    def test_a_rendition_is_its_own_base_form(self):
        # A live album is a record in its own right; its plain name is base.
        self.assertTrue(albums.is_base_form("Woodstock (Live)"))
        self.assertTrue(albums.is_base_form("1989 (Taylor's Version)"))

    def test_a_re_recordings_deluxe_is_not_a_base_form(self):
        self.assertFalse(albums.is_base_form("1989 (Taylor's Version) [Deluxe]"))


class ChooseEditionTest(unittest.TestCase):
    def test_the_base_album_wins_when_it_exists(self):
        album, exclusive = albums.choose_edition(
            ["SOUR (Deluxe)", "SOUR", "SOUR (Super Deluxe Edition)"])
        self.assertEqual(album, "SOUR")
        self.assertFalse(exclusive)

    def test_order_does_not_matter_when_a_base_exists(self):
        # iTunes ranks by relevance, which may put the deluxe first.
        album, exclusive = albums.choose_edition(["SOUR (Deluxe)", "SOUR"])
        self.assertEqual(album, "SOUR")
        self.assertFalse(exclusive)

    def test_a_deluxe_only_track_is_evidence(self):
        # The bonus track: it cannot be on the standard pressing, so the record
        # being played must be the deluxe.
        album, exclusive = albums.choose_edition(
            ["SOUR (Super Deluxe Edition)", "SOUR (Deluxe)"])
        self.assertTrue(exclusive)
        self.assertEqual(album, "SOUR (Deluxe)")   # least-qualified that fits

    def test_unrelated_albums_are_ignored(self):
        # The same song on a greatest-hits says nothing about which edition of
        # this album is spinning.
        album, exclusive = albums.choose_edition(
            ["SOUR (Deluxe)", "Now That's What I Call Music 108", "SOUR"])
        self.assertEqual(album, "SOUR")
        self.assertFalse(exclusive)

    def test_a_single_plain_result_proves_nothing(self):
        self.assertEqual(albums.choose_edition(["SOUR"]), ("SOUR", False))

    def test_renditions_do_not_count_as_evidence(self):
        # "SOUR (Video Version)" is a different recording, and it is the top
        # result here, so it defines its own family — and is its own base form.
        album, exclusive = albums.choose_edition(["SOUR (Video Version)"])
        self.assertEqual(album, "SOUR (Video Version)")
        self.assertFalse(exclusive)

    def test_empty_input_is_safe(self):
        self.assertEqual(albums.choose_edition([]), (None, False))
        self.assertEqual(albums.choose_edition(None), (None, False))
        self.assertEqual(albums.choose_edition(["", None]), (None, False))


class PickWinnerEvidenceTest(unittest.TestCase):
    def test_without_evidence_the_plainest_title_wins(self):
        self.assertEqual(
            albums.pick_winner([("SOUR", 100, False), ("SOUR (Deluxe)", 200, False)]),
            "SOUR")

    def test_one_proven_play_upgrades_the_whole_run(self):
        # This is the behaviour the original design asked for: default to the
        # base, and upgrade the session the moment a track proves the deluxe.
        self.assertEqual(
            albums.pick_winner([("SOUR", 100, False), ("SOUR", 150, False),
                                ("SOUR (Deluxe)", 200, True)]),
            "SOUR (Deluxe)")

    def test_evidence_on_a_base_title_is_ignored(self):
        # A base form can't prove an edition, whatever the flag says.
        self.assertEqual(
            albums.pick_winner([("SOUR", 100, True), ("SOUR (Deluxe)", 200, False)]),
            "SOUR")

    def test_the_most_qualified_proven_edition_wins(self):
        self.assertEqual(
            albums.pick_winner([("SOUR (Deluxe)", 100, True),
                                ("SOUR (Super Deluxe Edition)", 200, True)]),
            "SOUR (Super Deluxe Edition)")

    def test_pairs_without_a_flag_still_work(self):
        # Rows written before the column existed carry no evidence.
        self.assertEqual(
            albums.pick_winner([("SOUR", 100), ("SOUR (Deluxe)", 200)]), "SOUR")

    def test_ties_break_to_the_most_recent(self):
        self.assertEqual(
            albums.pick_winner([("Album (Deluxe A)", 100, False),
                                ("Album (Deluxe B)", 200, False)]),
            "Album (Deluxe B)")



class TrackMatchingTest(unittest.TestCase):
    """iTunes' search is fuzzy and answers with *something* rather than nothing.
    A query for AJR's "3 O'Clock Things" comes back with "Yes I'm A Mess" and
    "3AM" — from two albums the track is not on — so a play was labelled with
    the wrong record. Verified against the live API before writing these."""

    def key(self, title):
        return itunes.track_key(title)

    def test_punctuation_and_case_do_not_matter(self):
        # Shazam and iTunes disagree constantly on apostrophes, curly or not.
        self.assertEqual(self.key("3 O'Clock Things"), self.key("3 O’Clock Things"))
        self.assertEqual(self.key("Bang!"), self.key("bang"))
        self.assertEqual(self.key("Sing, Sing, Sing"), self.key("Sing Sing Sing"))

    def test_one_trailing_qualifier_comes_off(self):
        self.assertEqual(self.key("Weak (feat. Someone)"), self.key("Weak"))
        self.assertEqual(self.key("Weak [Remastered]"), self.key("Weak"))
        self.assertEqual(self.key("Weak - 2019 Remaster"), self.key("Weak"))

    def test_different_songs_do_not_collide(self):
        self.assertNotEqual(self.key("3 O'Clock Things"), self.key("3AM"))
        self.assertNotEqual(self.key("Bummerland"), self.key("Yes I'm A Mess"))

    def test_empty_titles_are_never_a_match(self):
        for junk in (None, "", "   ", "!!!"):
            self.assertEqual(itunes.results_for_track([{"trackName": "x"}], junk), [])

    def test_only_the_real_track_survives(self):
        results = [
            {"trackName": "Yes I'm A Mess", "collectionName": "The Maybe Man"},
            {"trackName": "3AM", "collectionName": "Infinity - EP"},
        ]
        self.assertEqual(itunes.results_for_track(results, "3 O'Clock Things"), [])

    def test_a_genuine_match_is_kept(self):
        results = [
            {"trackName": "Bummerland", "collectionName": "OK ORCHESTRA"},
            {"trackName": "Not It", "collectionName": "Something Else"},
        ]
        got = itunes.results_for_track(results, "Bummerland")
        self.assertEqual([r["collectionName"] for r in got], ["OK ORCHESTRA"])

    def test_cover_and_karaoke_rows_are_excluded(self):
        # Their titles carry the performer, so they never key as the track —
        # which also keeps them out of the edition analysis.
        results = [
            {"trackName": "Bummerland", "collectionName": "OK ORCHESTRA"},
            {"trackName": "Bummerland (Originally Performed by AJR) [Instrumental Version]",
             "collectionName": "Pristine Karaoke, Vol. 21"},
        ]
        got = itunes.results_for_track(results, "Bummerland")
        self.assertEqual(len(got), 1)

    def test_malformed_rows_are_skipped(self):
        results = [None, {}, {"trackName": None}, {"trackName": "Weak"}]
        self.assertEqual(len(itunes.results_for_track(results, "Weak")), 1)

    def test_no_results_is_safe(self):
        self.assertEqual(itunes.results_for_track([], "Weak"), [])
        self.assertEqual(itunes.results_for_track(None, "Weak"), [])

if __name__ == "__main__":
    unittest.main()
