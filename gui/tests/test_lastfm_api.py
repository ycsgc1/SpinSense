"""The /api/lastfm/* surface. The module's own behaviour is covered in
test_lastfm.py; these pin the HTTP contract the Settings page depends on —
especially the two-step handshake's in-memory token."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

from fastapi.testclient import TestClient  # noqa: E402

import backend_main  # noqa: E402
import lastfm  # noqa: E402


class LastFmApiTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(backend_main.app)
        self.stored = {}
        self._orig = (lastfm.request_token, lastfm.complete_auth,
                      lastfm.disconnect, lastfm.flush, lastfm.status,
                      lastfm.settings, lastfm._store)
        backend_main._pending_auth_token = None

        self.token_result = ("TOK", "")
        self.auth_result = ("gregory", "")

        async def fake_request_token(key, secret):
            self.stored["requested"] = (key, secret)
            return self.token_result

        async def fake_complete(key, secret, token):
            self.stored["completed"] = (key, secret, token)
            return self.auth_result

        lastfm.request_token = fake_request_token
        lastfm.complete_auth = fake_complete
        lastfm.disconnect = lambda: True
        lastfm.status = lambda db_path=None: {"connected": True, "username": "gregory"}
        lastfm.settings = lambda: {"API_Key": "K", "API_Secret": "S"}
        lastfm._store = lambda **fields: self.stored.setdefault("saved", {}).update(fields) or True

    def tearDown(self):
        (lastfm.request_token, lastfm.complete_auth, lastfm.disconnect,
         lastfm.flush, lastfm.status, lastfm.settings, lastfm._store) = self._orig
        backend_main._pending_auth_token = None

    def test_status_is_proxied(self):
        r = self.client.get("/api/lastfm/status")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["username"], "gregory")

    def test_auth_start_returns_a_redirect_url_built_from_the_origin(self):
        r = self.client.post("/api/lastfm/auth/start",
                             json={"api_key": "K", "api_secret": "S",
                                   "origin": "http://truenas.local:3313"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("cb=http%3A%2F%2Ftruenas.local%3A3313%2Fapi%2Flastfm%2Fcallback",
                      body["auth_url"])
        self.assertNotIn("token=", body["auth_url"])   # Last.fm mints its own

    def test_auth_start_also_returns_the_manual_fallback(self):
        r = self.client.post("/api/lastfm/auth/start",
                             json={"api_key": "K", "api_secret": "S",
                                   "origin": "http://h:3313"})
        self.assertIn("token=TOK", r.json()["manual_url"])
        self.assertEqual(backend_main._pending_auth_token, "TOK")

    def test_an_unusable_origin_still_leaves_the_manual_route(self):
        # No redirect possible, but the user isn't stuck.
        r = self.client.post("/api/lastfm/auth/start",
                             json={"api_key": "K", "api_secret": "S",
                                   "origin": "javascript:alert(1)"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["auth_url"])
        self.assertIn("token=TOK", r.json()["manual_url"])

    def test_auth_start_saves_credentials_only_after_lastfm_accepts_them(self):
        # A typo'd secret must not overwrite a working one.
        self.token_result = (None, "Invalid API key")
        r = self.client.post("/api/lastfm/auth/start",
                             json={"api_key": "bad", "api_secret": "bad"})
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("saved", self.stored)

    def test_auth_start_requires_both_fields(self):
        r = self.client.post("/api/lastfm/auth/start", json={"api_key": "K"})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("requested", self.stored)

    def test_auth_complete_consumes_the_pending_token(self):
        self.client.post("/api/lastfm/auth/start",
                         json={"api_key": "K", "api_secret": "S",
                               "origin": "http://h:3313"})
        r = self.client.post("/api/lastfm/auth/complete")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["username"], "gregory")
        self.assertEqual(self.stored["completed"][2], "TOK")
        # Single-use: the token is spent, so a replay can't re-authorise.
        self.assertIsNone(backend_main._pending_auth_token)

    def test_auth_complete_without_a_start_is_a_400(self):
        self.auth_result = ("", "No pending authorisation — start again")
        r = self.client.post("/api/lastfm/auth/complete")
        self.assertEqual(r.status_code, 400)

    def test_failed_complete_keeps_the_token_for_a_retry(self):
        # The user may simply not have approved it on last.fm yet.
        self.client.post("/api/lastfm/auth/start",
                         json={"api_key": "K", "api_secret": "S", "origin": "http://h:3313"})
        self.auth_result = ("", "Unauthorized Token")
        self.client.post("/api/lastfm/auth/complete")
        self.assertEqual(backend_main._pending_auth_token, "TOK")

    def test_disconnect_clears_any_pending_token(self):
        self.client.post("/api/lastfm/auth/start",
                         json={"api_key": "K", "api_secret": "S", "origin": "http://h:3313"})
        r = self.client.post("/api/lastfm/disconnect")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(backend_main._pending_auth_token)

    def test_flush_is_proxied(self):
        async def fake_flush(db_path=None):
            return {"ok": True, "submitted": 3, "detail": "scrobbled 3"}
        lastfm.flush = fake_flush
        r = self.client.post("/api/lastfm/flush")
        self.assertEqual(r.json()["submitted"], 3)


class LastFmCallbackTest(unittest.TestCase):
    """Where Last.fm drops the user after the redirect flow. It is a page they
    land on, so every outcome must redirect to Settings, never return JSON."""

    def setUp(self):
        # follow_redirects=False so we can assert on the redirect itself.
        self.client = TestClient(backend_main.app, follow_redirects=False)
        self._orig = (lastfm.complete_auth, lastfm.settings)
        self.result = ("gregory", "")
        self.seen = {}

        async def fake_complete(key, secret, token):
            self.seen["token"] = token
            return self.result

        lastfm.complete_auth = fake_complete
        lastfm.settings = lambda: {"API_Key": "K", "API_Secret": "S"}

    def tearDown(self):
        lastfm.complete_auth, lastfm.settings = self._orig

    def test_success_redirects_to_settings_with_the_username(self):
        r = self.client.get("/api/lastfm/callback?token=FROM_LASTFM")
        self.assertEqual(r.status_code, 303)
        self.assertIn("lastfm=connected", r.headers["location"])
        self.assertIn("user=gregory", r.headers["location"])
        self.assertEqual(self.seen["token"], "FROM_LASTFM")

    def test_missing_token_means_the_user_declined(self):
        r = self.client.get("/api/lastfm/callback")
        self.assertEqual(r.status_code, 303)
        self.assertIn("lastfm=denied", r.headers["location"])

    def test_failure_passes_the_reason_through(self):
        self.result = ("", "Unauthorized Token")
        r = self.client.get("/api/lastfm/callback?token=STALE")
        self.assertIn("lastfm=error", r.headers["location"])
        self.assertIn("Unauthorized", r.headers["location"])

    def test_the_reason_is_url_encoded(self):
        self.result = ("", "Invalid method signature & stuff")
        r = self.client.get("/api/lastfm/callback?token=X")
        self.assertNotIn(" ", r.headers["location"])
        self.assertIn("%26", r.headers["location"])


if __name__ == "__main__":
    unittest.main()
