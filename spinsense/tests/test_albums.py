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


class SingleAndEpTest(unittest.TestCase):
    """iTunes ranks by relevance, so a hit song's top result is the single, not
    the record it is on: "Espresso" led with "Espresso EP" and "Please Please
    Please" with "Please Please Please - Single". Someone with a turntable is
    playing an album."""

    def test_singles_and_eps_are_recognised(self):
        for name in ("Espresso - Single", "Espresso EP", "Bang! - single",
                     "Infinity - EP"):
            with self.subTest(name=name):
                self.assertTrue(albums.is_single_or_ep(name))

    def test_albums_are_not(self):
        for name in ("Short n' Sweet", "OK ORCHESTRA", "SOUR (Deluxe)",
                     "Hurry Up, We're Dreaming"):
            with self.subTest(name=name):
                self.assertFalse(albums.is_single_or_ep(name))

    def test_an_album_outranks_a_higher_ranked_single(self):
        album, exclusive = albums.choose_edition(
            ["Espresso EP", "Short n' Sweet", "Espresso - Single"])
        self.assertEqual(album, "Short n' Sweet")
        self.assertFalse(exclusive)

    def test_a_lone_single_still_resolves_to_itself(self):
        # 7-inches exist; this only reorders preference, it doesn't discard.
        self.assertEqual(
            albums.choose_edition(["Espresso - Single"]),
            ("Espresso - Single", False))

    def test_the_edition_rule_still_applies_among_albums(self):
        album, exclusive = albums.choose_edition(
            ["Espresso - Single", "Short n' Sweet (Deluxe)", "Short n' Sweet"])
        self.assertEqual(album, "Short n' Sweet")
        self.assertFalse(exclusive)

    def test_a_deluxe_only_track_is_still_proof(self):
        album, exclusive = albums.choose_edition(
            ["Bonus - Single", "Short n' Sweet (Deluxe)"])
        self.assertEqual(album, "Short n' Sweet (Deluxe)")
        self.assertTrue(exclusive)


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


class ArtistMatchingTest(unittest.TestCase):
    """Cover and lullaby records title their tracks identically, so a title
    check alone waves them through: "My Play" came back from "Lullaby Versions
    of AJR", performed by The Cat and Owl, and was recorded as the album."""

    def test_the_performer_is_what_distinguishes_a_cover(self):
        results = [
            {"trackName": "My Play", "artistName": "The Cat and Owl",
             "collectionName": "Lullaby Versions of AJR - EP"},
        ]
        self.assertEqual(itunes.results_for_track(results, "My Play", "AJR"), [])

    def test_the_album_name_would_not_have_helped(self):
        # "Lullaby Versions of AJR" contains the real artist; artistName does
        # not. That is why the check is on the performer, not the album.
        self.assertIn("AJR", "Lullaby Versions of AJR - EP")
        self.assertNotEqual(itunes.artist_key("The Cat and Owl"),
                            itunes.artist_key("AJR"))

    def test_the_real_artist_is_kept(self):
        results = [{"trackName": "Joe", "artistName": "AJR",
                    "collectionName": "OK ORCHESTRA"}]
        self.assertEqual(len(itunes.results_for_track(results, "Joe", "AJR")), 1)

    def test_featured_credits_do_not_break_a_match(self):
        # The two catalogues attach featured artists inconsistently.
        for name in ("AJR feat. Someone", "AJR ft. Someone",
                     "AJR featuring Someone", "AJR (feat. Someone)"):
            with self.subTest(name=name):
                self.assertEqual(itunes.artist_key(name), itunes.artist_key("AJR"))

    def test_punctuation_and_case_are_ignored(self):
        self.assertEqual(itunes.artist_key("Tyler, The Creator"),
                         itunes.artist_key("tyler the creator"))

    def test_different_artists_never_collide(self):
        self.assertNotEqual(itunes.artist_key("M83"), itunes.artist_key("M"))
        self.assertNotEqual(itunes.artist_key("AJR"), itunes.artist_key("AJR Project"))

    def test_omitting_the_artist_keeps_the_old_behaviour(self):
        # The filter is opt-in per caller; title-only still works.
        results = [{"trackName": "My Play", "artistName": "The Cat and Owl"}]
        self.assertEqual(len(itunes.results_for_track(results, "My Play")), 1)

if __name__ == "__main__":
    unittest.main()


class SharedCreditTest(unittest.TestCase):
    """Which track credits belong to the same artist's record.

    From a real session: a full side of Short n' Sweet, ending with "Please
    Please Please" credited to "Sabrina Carpenter & Dolly Parton". That play is
    the only one carrying proof the record is the deluxe, and an exact-string
    match put it in a session by itself where it could upgrade nothing.
    """

    def same(self, a, b):
        return albums.shares_credit(a, b)

    def test_a_credit_matches_itself(self):
        self.assertTrue(self.same("AJR", "AJR"))

    def test_case_and_spacing_do_not_matter(self):
        self.assertTrue(self.same("Sabrina Carpenter", "  sabrina   carpenter "))

    def test_a_guest_joins_the_records_artist(self):
        self.assertTrue(self.same("Sabrina Carpenter",
                                  "Sabrina Carpenter & Dolly Parton"))

    def test_it_holds_in_both_directions(self):
        # Reconciliation can be triggered from either play.
        self.assertTrue(self.same("Sabrina Carpenter & Dolly Parton",
                                  "Sabrina Carpenter"))

    def test_every_way_a_guest_is_joined(self):
        for joined in ("Artist & Guest", "Artist and Guest", "Artist feat. Guest",
                       "Artist ft. Guest", "Artist featuring Guest",
                       "Artist with Guest", "Artist, Guest", "Artist + Guest",
                       "Artist x Guest", "Artist vs. Guest"):
            with self.subTest(joined=joined):
                self.assertTrue(self.same("Artist", joined))

    def test_a_guest_does_not_capture_someone_elses_record(self):
        # "Rowan Blanchard & Sabrina Carpenter" is Rowan Blanchard's record.
        self.assertFalse(self.same("Sabrina Carpenter",
                                   "Rowan Blanchard & Sabrina Carpenter"))

    def test_two_unrelated_artists_do_not_match(self):
        self.assertFalse(self.same("AJR", "Sabrina Carpenter"))

    def test_bands_whose_names_contain_a_join_are_left_alone(self):
        # The reason this is a prefix test and not a "take the first name" one:
        # reducing each credit to its leading word would file all of these under
        # somebody else's.
        for band, impostor in (("Simon & Garfunkel", "Simon"),
                               ("Florence + the Machine", "Florence"),
                               ("Earth, Wind & Fire", "Earth"),
                               ("Nick Cave & The Bad Seeds", "Nick Cave")):
            with self.subTest(band=band):
                self.assertTrue(self.same(band, band))
                # The band is that artist's record *extended*, which is the one
                # ambiguity no rule can resolve — but they must not collapse
                # into a third, unrelated artist.
                self.assertFalse(self.same(band, "Bruce Springsteen"))
                self.assertNotEqual(band, impostor)

    def test_a_shared_first_word_is_not_enough(self):
        self.assertFalse(self.same("The Beatles", "The Beach Boys"))
        self.assertFalse(self.same("Sabrina Carpenter", "Sabrina Claudio"))

    def test_a_prefix_that_is_not_a_credit_boundary_does_not_match(self):
        # "Kid Cudi" starts with "Kid" but is not "Kid" plus a guest.
        self.assertFalse(self.same("Kid", "Kid Cudi"))

    def test_missing_credits_never_match(self):
        for a, b in ((None, None), ("", ""), ("AJR", None), ("", "AJR")):
            with self.subTest(a=a, b=b):
                self.assertFalse(self.same(a, b))
