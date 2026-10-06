"""POST /api/config, and the one part of the config a page may not write.

Settings and the wizard both save by posting back the whole config they
loaded. That is fine for everything a form edits and wrong for the Last.fm
connection, which is written by the auth flow while the page sits open: a page
is a snapshot, and saving a snapshot taken before the account was connected
saved the connection away again. The reverse held too — a page loaded before a
Disconnect signed the account back in the next time anything was saved.

Reachable without doing anything unusual: approve through the manual flow with
an unsaved edit on the page, or leave Settings open on a second device.
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
import lastfm  # noqa: E402


class _QuietAdvertiser:
    """Saving reconciles the mDNS advertisement; no test here wants a socket."""

    async def reconcile(self, config):
        self.last = config


class ConfigApiTest(unittest.TestCase):
    def setUp(self):
        fd, self.cfg_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._orig_cfg = config_manager.CONFIG_PATH
        config_manager.CONFIG_PATH = self.cfg_path
        config_manager.save_config(config_manager.get_default_config())
        self._orig_advertiser = backend_main.advertiser
        backend_main.advertiser = _QuietAdvertiser()
        self.client = TestClient(backend_main.app)

    def tearDown(self):
        backend_main.advertiser = self._orig_advertiser
        config_manager.CONFIG_PATH = self._orig_cfg
        try:
            os.remove(self.cfg_path)
        except OSError:
            pass

    # --- helpers ---

    def page_loads(self) -> dict:
        """The snapshot a Settings page holds from the moment it opens."""
        return self.client.get("/api/config").json()

    def connect(self):
        lastfm._store(Session_Key="REAL-SESSION", Username="greg",
                      Scrobble_Since=1700000000, Enabled=True)

    def saved(self) -> dict:
        with open(self.cfg_path) as f:
            return json.load(f)

    def save(self, config):
        return self.client.post("/api/config", json=config)

    # --- the connection is not the page's to write ---

    def test_a_page_opened_before_connecting_cannot_save_the_session_away(self):
        stale = self.page_loads()
        self.connect()
        stale["Audio"]["Song_Sample_Length"] = 6.0     # an unrelated edit
        self.assertEqual(self.save(stale).status_code, 200)
        section = self.saved()["LastFM"]
        self.assertEqual(section["Session_Key"], "REAL-SESSION")
        self.assertEqual(section["Username"], "greg")
        self.assertEqual(section["Scrobble_Since"], 1700000000)

    def test_a_page_opened_before_disconnecting_cannot_sign_back_in(self):
        self.connect()
        stale = self.page_loads()
        lastfm.disconnect()
        stale["Audio"]["Song_Sample_Length"] = 7.0
        self.save(stale)
        section = self.saved()["LastFM"]
        self.assertEqual(section["Session_Key"], "")
        self.assertEqual(section["Username"], "")
        self.assertFalse(lastfm.is_active())

    def test_an_override_key_survives_a_stale_save(self):
        stale = self.page_loads()
        lastfm._store(API_Key="OWN-KEY", API_Secret="OWN-SECRET")
        self.save(stale)
        self.assertEqual(lastfm.credentials(), ("OWN-KEY", "OWN-SECRET"))

    def test_a_posted_session_is_never_taken_at_its_word(self):
        # Nothing in the UI sends one that differs, so anything that does is
        # either stale or not ours.
        config = self.page_loads()
        config["LastFM"]["Session_Key"] = "SOMEONE-ELSES"
        config["LastFM"]["Username"] = "someone"
        self.save(config)
        self.assertEqual(self.saved()["LastFM"]["Session_Key"], "")

    # --- everything a form does edit still saves ---

    def test_the_scrobbling_settings_on_the_form_still_save(self):
        self.connect()
        config = self.page_loads()
        config["LastFM"].update({"Enabled": False, "Submit_Trigger": "track",
                                 "Submit_Delay_Mins": 5,
                                 "Scrobble_Now_Playing": False})
        self.assertEqual(self.save(config).status_code, 200)
        section = self.saved()["LastFM"]
        self.assertFalse(section["Enabled"])
        self.assertEqual(section["Submit_Trigger"], "track")
        self.assertEqual(section["Submit_Delay_Mins"], 5)
        self.assertFalse(section["Scrobble_Now_Playing"])
        self.assertEqual(section["Session_Key"], "REAL-SESSION")

    def test_other_sections_save_as_posted(self):
        config = self.page_loads()
        config["Audio"]["Volume_Threshold"] = 0.02
        config["Hardware"]["Mic_Device"] = "USB Audio CODEC"
        config["System"]["Setup_Wizard_State"] = "completed"
        self.save(config)
        saved = self.saved()
        self.assertAlmostEqual(saved["Audio"]["Volume_Threshold"], 0.02)
        self.assertEqual(saved["Hardware"]["Mic_Device"], "USB Audio CODEC")
        self.assertEqual(saved["System"]["Setup_Wizard_State"], "completed")

    def test_a_config_with_no_last_fm_section_keeps_the_connection(self):
        # The wizard builds its payload from whatever it managed to load, and
        # if that fetch failed it posts only the fields it sets itself.
        self.connect()
        self.save({"System": {"Setup_Wizard_State": "completed"}})
        self.assertEqual(self.saved()["LastFM"]["Session_Key"], "REAL-SESSION")

    def test_an_unreadable_file_is_not_a_reason_to_blank_the_connection(self):
        # Nothing saved can be trusted, so nothing is restored over the post.
        config = self.page_loads()
        config["LastFM"]["Session_Key"] = "FROM-THE-PAGE"
        with open(self.cfg_path, "w") as f:
            f.write('{"LastFM": {"Session_')
        self.save(config)
        self.assertEqual(self.saved()["LastFM"]["Session_Key"], "FROM-THE-PAGE")

    # --- validation ---

    def test_an_invalid_value_is_refused_and_named(self):
        config = self.page_loads()
        config["Audio"]["Fallback_Provider"] = "tape"
        r = self.save(config)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Audio.Fallback_Provider", r.json()["detail"])

    def test_a_body_that_is_not_an_object_is_refused(self):
        for body in ([], "config", 3):
            with self.subTest(body=body):
                self.assertEqual(self.save(body).status_code, 400)

    def test_a_refused_save_leaves_the_file_alone(self):
        before = self.saved()
        config = self.page_loads()
        config["Audio"]["Volume_Threshold"] = "loud"
        self.save(config)
        self.assertEqual(self.saved(), before)


if __name__ == "__main__":
    unittest.main()
