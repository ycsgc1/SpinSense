"""Tests for album/edition reconciliation. Pure-function table tests here;
run/rewrite DB tests are added by a later task in this same file."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import play_history  # noqa: E402  (used by DB tests in a later task)
import reconcile  # noqa: E402


class BaseTitleTest(unittest.TestCase):
    def test_equivalences(self):
        base = reconcile.base_title("Abbey Road")
        for variant in [
            "Abbey Road (Super Deluxe Edition)",
            "Abbey Road - 50th Anniversary",
            "Abbey Road (Deluxe Edition) [2019 Remaster]",
            "Abbey Road [2014]",
            "Abbey Road (Bonus Track Version)",
            "abbey  road",
        ]:
            self.assertEqual(reconcile.base_title(variant), base, variant)

    def test_non_merges(self):
        studio = reconcile.base_title("At Folsom Prison")
        self.assertNotEqual(reconcile.base_title("At Folsom Prison (Live)"), studio)
        self.assertNotEqual(reconcile.base_title("Nevermind (Acoustic)"),
                            reconcile.base_title("Nevermind"))

    def test_taylors_version_never_merges(self):
        base = reconcile.base_title("1989")
        self.assertNotEqual(reconcile.base_title("1989 (Taylor's Version)"), base)
        # curly apostrophe form
        self.assertNotEqual(reconcile.base_title("1989 (Taylor's Version)"), base)
        # but a deluxe qualifier ON TOP of Taylor's Version still strips
        self.assertEqual(
            reconcile.base_title("1989 (Taylor's Version) [Deluxe]"),
            reconcile.base_title("1989 (Taylor's Version)"),
        )

    def test_dash_only_strips_qualifiers(self):
        self.assertEqual(reconcile.base_title("Blood - Sugar - Deluxe Edition"),
                         reconcile.base_title("Blood - Sugar"))
        # a non-qualifier dash segment stays
        self.assertNotEqual(reconcile.base_title("First - Last"),
                            reconcile.base_title("First"))
        # hyphenated words (no spaces) never strip
        self.assertEqual(reconcile.base_title("X-Ray"), "x-ray")

    def test_empty_and_none_safe(self):
        self.assertEqual(reconcile.base_title(""), "")
        self.assertEqual(reconcile.base_title(None), "")


class PickWinnerTest(unittest.TestCase):
    def test_longest_wins(self):
        albums = [("Abbey Road", 100), ("Abbey Road (Super Deluxe Edition)", 50)]
        self.assertEqual(reconcile.pick_winner(albums),
                         "Abbey Road (Super Deluxe Edition)")

    def test_tie_breaks_most_recent(self):
        albums = [("Album (Deluxe A)", 100), ("Album (Deluxe B)", 200)]
        self.assertEqual(reconcile.pick_winner(albums), "Album (Deluxe B)")
