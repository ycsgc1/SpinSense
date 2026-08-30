"""Shared shell markup, rendered through every page route.

The desktop sidebar logo shipped as a bare <div> while the mobile header's was
a link, so clicking it did nothing on a desktop browser — invisible to anyone
testing on a phone, and to a test suite that never rendered the layout.
"""
import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

from fastapi.testclient import TestClient  # noqa: E402

import backend_main  # noqa: E402
import config_manager  # noqa: E402
import stats  # noqa: E402

PAGES = ("/", "/history", "/settings", "/stats", "/setup")

# Both shells carry one: the desktop sidebar and the mobile top bar.
HOME_LINK = '<a href="/" aria-label="SpinSense home"'


class LayoutTest(unittest.TestCase):
    def setUp(self):
        # Without this the wizard gate 307s every page to /setup and the whole
        # suite silently asserts against the setup screen instead.
        self.cfg_fd, self.cfg_path = tempfile.mkstemp(suffix=".json")
        os.close(self.cfg_fd)
        with open(self.cfg_path, "w") as f:
            json.dump({"System": {"Setup_Wizard_State": "completed"}}, f)
        self._orig_cfg = config_manager.CONFIG_PATH
        config_manager.CONFIG_PATH = self.cfg_path
        self.client = TestClient(backend_main.app)

    def tearDown(self):
        config_manager.CONFIG_PATH = self._orig_cfg
        try:
            os.remove(self.cfg_path)
        except OSError:
            pass

    def test_the_dashboard_is_not_being_redirected_away(self):
        # Guards the fixture above: if the gate fires, every other assertion
        # here is about /setup and proves nothing.
        r = self.client.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 200)

    def test_every_page_renders(self):
        for path in PAGES:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_both_logos_link_home_on_every_page(self):
        for path in PAGES:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).text.count(HOME_LINK), 2)

    def test_every_page_carries_a_now_playing_caption(self):
        # Two shells, desktop and mobile.
        for path in PAGES:
            with self.subTest(path=path):
                self.assertEqual(
                    self.client.get(path).text.count('class="engine-now-playing'), 2)

    def test_the_dashboard_opts_out_of_the_caption(self):
        # It already renders the record and full metadata; the caption would
        # just say the same thing twice on the same screen.
        self.assertEqual(self.client.get("/").text.count('data-now-playing="off"'), 2)
        for path in ("/history", "/stats", "/settings"):
            with self.subTest(path=path):
                self.assertEqual(
                    self.client.get(path).text.count('data-now-playing="on"'), 2)

    def test_the_idle_pill_does_not_claim_to_be_listening(self):
        # It said "Listening" with nothing on the platter, while the dashboard
        # called the same state "Idle".
        body = self.client.get("/history").text
        self.assertNotIn(">Listening<", body)
        self.assertEqual(body.count("engine-pill-label"), 2)

    def test_static_urls_are_cache_stamped(self):
        # The stamp is what stops a rebuild serving stale JS against fresh
        # markup; an unstamped asset URL silently reopens that hole.
        body = self.client.get("/").text
        self.assertIn(f"?v={backend_main.ASSET_VERSION}", body)
        self.assertNotIn('src="/static/shell.js"', body)


class StatsExpanderMarkupTest(LayoutTest):
    """The "show all" controls are wired by id from stats.js.

    A renamed id or a missing button fails silently in the browser — the list
    renders, the button never appears, and the extra rows stay hidden forever.
    Both halves are asserted here because neither file can see the other.
    """

    LISTS = ("stats-top-artists", "stats-top-albums", "stats-top-tracks")

    def stats_html(self):
        return self.client.get("/stats").text

    def stats_js(self):
        return self.client.get("/static/stats.js").text

    def test_every_ranked_list_has_an_expander(self):
        html = self.stats_html()
        for list_id in self.LISTS:
            with self.subTest(list_id=list_id):
                self.assertIn(f'id="{list_id}"', html)
                self.assertIn(f'id="{list_id}-more"', html)

    def test_the_expander_starts_hidden_and_describes_its_list(self):
        # Hidden until the data says there is more to show, and pointed at the
        # list it controls so a screen reader can follow the relationship.
        html = self.stats_html()
        for list_id in self.LISTS:
            with self.subTest(list_id=list_id):
                button = html.split(f'id="{list_id}-more"', 1)[1].split(">", 1)[0]
                self.assertIn("hidden", button)
                self.assertIn('aria-expanded="false"', button)
                self.assertIn(f'aria-controls="{list_id}"', button)

    def test_the_script_targets_those_same_lists(self):
        js = self.stats_js()
        for list_id in self.LISTS:
            with self.subTest(list_id=list_id):
                self.assertIn(f'"{list_id}"', js)

    def test_the_collapsed_length_is_more_than_a_headline(self):
        # The server sends stats.TOP_N rows; this is how many are shown first.
        js = self.stats_js()
        match = re.search(r"COLLAPSED_ROWS\s*=\s*(\d+)", js)
        self.assertIsNotNone(match, "COLLAPSED_ROWS not found in stats.js")
        shown = int(match.group(1))
        self.assertGreaterEqual(shown, 10)
        self.assertLess(shown, stats.TOP_N)   # or there would be nothing to expand


if __name__ == "__main__":
    unittest.main()
