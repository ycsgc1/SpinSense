"""Guards on the qualifier lists themselves.

`EDITION_MARKERS` and `RENDITION_MARKERS` exist to be edited — that is the whole
point of them being plain lists rather than hand-written regexes. Which means
the safety has to live here rather than in whoever edits them next.

Which list a term goes in decides opposite behaviour: an edition is stripped and
its plays merged into the plain title, a rendition is kept apart forever. These
tests check that every declared term actually does what its list promises, and
that nothing has drifted into both.
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from spinsense import albums  # noqa: E402

ALL_TERMS = albums.EDITION_MARKERS + albums.RENDITION_MARKERS


class TermHygieneTest(unittest.TestCase):
    def test_every_term_is_lowercase_and_trimmed(self):
        # Matching folds case, but a stray capital or space in the list is a
        # sign the entry was pasted rather than written, and worth catching.
        for term in ALL_TERMS:
            with self.subTest(term=term):
                self.assertEqual(term, term.strip())
                self.assertEqual(term, term.lower())

    def test_no_term_is_empty(self):
        # An empty string would match everywhere and strip every album title.
        for term in ALL_TERMS:
            self.assertTrue(term)

    def test_no_term_is_listed_twice_in_one_list(self):
        for terms in (albums.EDITION_MARKERS, albums.RENDITION_MARKERS):
            self.assertEqual(len(terms), len(set(terms)))

    def test_no_term_is_in_both_lists(self):
        # A rendition marker wins over an edition marker, so a term in both
        # would silently behave as a rendition and the edition entry would
        # look present while doing nothing.
        overlap = set(albums.EDITION_MARKERS) & set(albums.RENDITION_MARKERS)
        self.assertEqual(overlap, set())

    def test_a_longer_term_wins_over_one_nested_inside_it(self):
        # Word boundaries already separate the pairs currently listed
        # ("remix"/"remixes"), so this is about terms added later: a new
        # "deluxe edition" alongside "deluxe" must report the longer one, or
        # the marker read off a title would be the wrong half of the phrase.
        pattern = albums._compile_markers(("deluxe", "deluxe edition"))
        self.assertEqual(pattern.search("deluxe edition").group(1), "deluxe edition")

    def test_a_term_with_punctuation_is_matched_literally(self):
        # Terms are escaped, so a phrase can contain regex characters without
        # anyone having to know that.
        pattern = albums._compile_markers(("rock + roll",))
        self.assertTrue(pattern.search("rock + roll"))
        self.assertFalse(pattern.search("rock roll"))

    def test_every_pattern_compiles(self):
        for pattern in albums.EDITION_PATTERNS + albums.RENDITION_PATTERNS:
            with self.subTest(pattern=pattern):
                re.compile(pattern)


class TermsDoWhatTheirListPromisesTest(unittest.TestCase):
    """Each term, placed in a real trailing qualifier, on a real title."""

    def test_every_edition_term_is_stripped(self):
        for term in albums.EDITION_MARKERS:
            with self.subTest(term=term):
                title = f"Some Record ({term})"
                self.assertEqual(albums.base_title(title), "some record")
                self.assertFalse(albums.is_base_form(title))

    def test_every_edition_term_works_in_square_brackets_too(self):
        # Both are common; iTunes uses either depending on the release.
        for term in albums.EDITION_MARKERS:
            with self.subTest(term=term):
                self.assertEqual(albums.base_title(f"Some Record [{term}]"),
                                 "some record")

    def test_every_edition_term_works_after_a_dash(self):
        for term in albums.EDITION_MARKERS:
            with self.subTest(term=term):
                self.assertEqual(albums.base_title(f"Some Record - {term}"),
                                 "some record")

    def test_no_rendition_term_is_ever_stripped(self):
        # A live album is not a pressing of the studio album and must never be
        # merged into it.
        for term in albums.RENDITION_MARKERS:
            with self.subTest(term=term):
                title = f"Some Record ({term})"
                self.assertEqual(albums.base_title(title), albums.normalized(title))
                self.assertTrue(albums.is_base_form(title))

    def test_every_rendition_term_marks_a_different_recording(self):
        # This is what keeps a re-recording from being answered by the original.
        for term in albums.RENDITION_MARKERS:
            with self.subTest(term=term):
                self.assertTrue(albums.rendition_markers(f"A Song ({term})"))
                self.assertFalse(
                    albums.same_recording(f"A Song ({term})", "A Song"))

    def test_every_term_reduces_to_the_same_record(self):
        # recording_base is the looser reading, and both kinds come off it.
        for term in ALL_TERMS:
            with self.subTest(term=term):
                self.assertEqual(albums.recording_base(f"Some Record ({term})"),
                                 "some record")


class DeclaredPatternsTest(unittest.TestCase):
    """The raw-regex entries, with the examples their comments claim."""

    def test_a_year_plus_remaster_is_an_edition(self):
        for title in ("Some Record (2011 Remaster)", "Some Record (1987 Mix)"):
            with self.subTest(title=title):
                self.assertEqual(albums.base_title(title), "some record")

    def test_a_possessive_version_is_a_rendition(self):
        for title in ("1989 (Taylor's Version)", "1989 (Taylor’s Version)"):
            with self.subTest(title=title):
                self.assertTrue(albums.is_base_form(title))
                self.assertTrue(albums.rendition_markers(title))

    def test_a_possessive_versions_own_edition_still_strips(self):
        self.assertEqual(albums.base_title("1989 (Taylor's Version) [Deluxe]"),
                         "1989 (taylor's version)")


class ApostropheFoldingTest(unittest.TestCase):
    """iTunes and Shazam disagree about apostrophes constantly."""

    def test_a_curly_apostrophe_reads_as_a_straight_one(self):
        self.assertEqual(albums.base_title("Some Record (Collector’s Edition)"),
                         albums.base_title("Some Record (Collector's Edition)"))

    def test_a_curly_possessive_version_is_still_a_rendition(self):
        self.assertEqual(albums.rendition_markers("A Song (Taylor’s Version)"),
                         albums.rendition_markers("A Song (Taylor's Version)"))


if __name__ == "__main__":
    unittest.main()
