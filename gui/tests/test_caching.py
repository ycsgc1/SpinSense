import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import unittest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


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

    def test_album_art_must_revalidate(self):
        # /art/{id}.jpg is rewritten in place when a play's album is corrected,
        # so a cached copy at the same URL is stale by definition. Leaving it
        # cacheable made run-wide art changes revert on the next load — and
        # behind a caching proxy, revert in a way clearing the browser cache
        # could not fix.
        import io as _io
        import os as _os

        from PIL import Image

        import ipc_manager
        _os.makedirs(ipc_manager.ART_DIR, exist_ok=True)
        probe = _os.path.join(ipc_manager.ART_DIR, "cache-probe.jpg")
        buf = _io.BytesIO()
        Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, "JPEG")
        with open(probe, "wb") as f:
            f.write(buf.getvalue())
        try:
            r = self.client.get("/art/cache-probe.jpg")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers.get("cache-control"), "no-cache")
        finally:
            _os.remove(probe)

    def test_api_response_not_forced_no_cache(self):
        # API JSON isn't an asset; we don't slap no-cache on it.
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(r.headers.get("cache-control"), "no-cache")
