import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import unittest
from fastapi.testclient import TestClient


class TestNoCacheHeaders(unittest.TestCase):
    """The app's own HTML pages and JS/CSS must always revalidate, so a rebuild
    can't leave the browser running a stale asset against fresh markup (the
    class of 'works in incognito but not my normal browser' bugs)."""

    def setUp(self):
        import backend_main
        self.client = TestClient(backend_main.app)

    def test_static_asset_is_no_cache(self):
        r = self.client.get("/static/setup.js")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("cache-control"), "no-cache")

    def test_html_page_is_no_cache(self):
        r = self.client.get("/setup")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers.get("content-type", "").startswith("text/html"))
        self.assertEqual(r.headers.get("cache-control"), "no-cache")

    def test_api_response_not_forced_no_cache(self):
        # API JSON isn't an asset; we don't slap no-cache on it.
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(r.headers.get("cache-control"), "no-cache")
