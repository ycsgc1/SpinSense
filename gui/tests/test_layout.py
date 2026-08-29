"""Shared shell markup, rendered through every page route.

The desktop sidebar logo shipped as a bare <div> while the mobile header's was
a link, so clicking it did nothing on a desktop browser — invisible to anyone
testing on a phone, and to a test suite that never rendered the layout.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

from fastapi.testclient import TestClient  # noqa: E402

import backend_main  # noqa: E402

PAGES = ("/", "/history", "/settings", "/stats", "/setup")

# Both shells carry one: the desktop sidebar and the mobile top bar.
HOME_LINK = '<a href="/" aria-label="SpinSense home"'


class LayoutTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(backend_main.app)

    def test_every_page_renders(self):
        for path in PAGES:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_both_logos_link_home_on_every_page(self):
        for path in PAGES:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).text.count(HOME_LINK), 2)

    def test_static_urls_are_cache_stamped(self):
        # The stamp is what stops a rebuild serving stale JS against fresh
        # markup; an unstamped asset URL silently reopens that hole.
        body = self.client.get("/").text
        self.assertIn(f"?v={backend_main.ASSET_VERSION}", body)
        self.assertNotIn('src="/static/shell.js"', body)


if __name__ == "__main__":
    unittest.main()
