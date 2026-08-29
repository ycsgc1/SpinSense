"""Shared shell markup, rendered through every page route.

The desktop sidebar logo shipped as a bare <div> while the mobile header's was
a link, so clicking it did nothing on a desktop browser — invisible to anyone
testing on a phone, and to a test suite that never rendered the layout.
"""
import json
import os
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


if __name__ == "__main__":
    unittest.main()
