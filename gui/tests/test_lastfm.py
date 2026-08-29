"""Last.fm scrobbling: the protocol maths, the queue's exactly-once behaviour,
and the auth handshake. `lastfm._api_post` is the only network seam, so every
test here stubs that one function."""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import config_manager  # noqa: E402
import lastfm  # noqa: E402
import play_history  # noqa: E402


class SignatureTest(unittest.TestCase):
    """Last.fm's api_sig: parameters sorted by name, concatenated as name+value
    with no separators, secret appended, MD5. Getting any part of this wrong
    produces a valid-looking request that is always rejected."""

    def test_known_vector(self):
        # Worked by hand: "api_keyKEYmethodauth.getTokenSECRET"
        import hashlib
        expected = hashlib.md5(
            b"api_keyKEYmethodauth.getTokenSECRET", usedforsecurity=False).hexdigest()
        self.assertEqual(
            lastfm.sign({"method": "auth.getToken", "api_key": "KEY"}, "SECRET"),
            expected)

    def test_parameter_order_does_not_matter(self):
        a = lastfm.sign({"b": "2", "a": "1", "c": "3"}, "S")
        b = lastfm.sign({"c": "3", "a": "1", "b": "2"}, "S")
        self.assertEqual(a, b)

    def test_format_and_api_sig_are_excluded(self):
        base = lastfm.sign({"a": "1"}, "S")
        self.assertEqual(lastfm.sign({"a": "1", "format": "json"}, "S"), base)
        self.assertEqual(lastfm.sign({"a": "1", "api_sig": "stale"}, "S"), base)

    def test_secret_changes_the_signature(self):
        self.assertNotEqual(lastfm.sign({"a": "1"}, "S1"),
                            lastfm.sign({"a": "1"}, "S2"))

    def test_signed_adds_signature_and_json_format(self):
        out = lastfm.signed({"method": "m", "api_key": "K"}, "S")
        self.assertEqual(out["format"], "json")
        self.assertEqual(out["api_sig"], lastfm.sign({"method": "m", "api_key": "K"}, "S"))
        self.assertEqual(out["method"], "m")  # originals preserved

    def test_signed_does_not_mutate_its_input(self):
        params = {"method": "m"}
        lastfm.signed(params, "S")
        self.assertEqual(params, {"method": "m"})


class CredentialResolutionTest(unittest.TestCase):
    """Most specific wins: the user's own key, then the environment, then the
    application key SpinSense ships with."""

    def setUp(self):
        self._env = {k: os.environ.pop(k, None)
                     for k in ("SPINSENSE_LASTFM_KEY", "SPINSENSE_LASTFM_SECRET")}
        self._builtin = (lastfm.BUILTIN_API_KEY, lastfm.BUILTIN_API_SECRET)
        lastfm.BUILTIN_API_KEY, lastfm.BUILTIN_API_SECRET = "BK", "BS"

    def tearDown(self):
        lastfm.BUILTIN_API_KEY, lastfm.BUILTIN_API_SECRET = self._builtin
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_built_in_is_the_default(self):
        self.assertEqual(lastfm.credentials({}), ("BK", "BS"))
        self.assertFalse(lastfm.uses_own_credentials({}))

    def test_environment_overrides_the_built_in(self):
        os.environ["SPINSENSE_LASTFM_KEY"] = "EK"
        os.environ["SPINSENSE_LASTFM_SECRET"] = "ES"
        self.assertEqual(lastfm.credentials({}), ("EK", "ES"))

    def test_user_config_beats_everything(self):
        os.environ["SPINSENSE_LASTFM_KEY"] = "EK"
        os.environ["SPINSENSE_LASTFM_SECRET"] = "ES"
        cfg = {"API_Key": "UK", "API_Secret": "US"}
        self.assertEqual(lastfm.credentials(cfg), ("UK", "US"))
        self.assertTrue(lastfm.uses_own_credentials(cfg))

    def test_half_a_pair_is_ignored_at_every_tier(self):
        # Mixing a user key with the built-in secret signs a request Last.fm
        # rejects, with an error that points nowhere useful.
        self.assertEqual(lastfm.credentials({"API_Key": "UK"}), ("BK", "BS"))
        self.assertEqual(lastfm.credentials({"API_Secret": "US"}), ("BK", "BS"))
        os.environ["SPINSENSE_LASTFM_KEY"] = "EK"
        self.assertEqual(lastfm.credentials({}), ("BK", "BS"))

    def test_whitespace_only_values_do_not_count(self):
        self.assertEqual(lastfm.credentials({"API_Key": "  ", "API_Secret": " "}),
                         ("BK", "BS"))

    def test_no_credentials_anywhere_resolves_empty(self):
        lastfm.BUILTIN_API_KEY, lastfm.BUILTIN_API_SECRET = "", ""
        self.assertEqual(lastfm.credentials({}), ("", ""))

    def test_connected_needs_a_session_as_well_as_a_key(self):
        self.assertFalse(lastfm.is_connected({}))
        self.assertTrue(lastfm.is_connected({"Session_Key": "SK"}))

    def test_a_session_alone_is_not_enough_without_a_key(self):
        lastfm.BUILTIN_API_KEY, lastfm.BUILTIN_API_SECRET = "", ""
        self.assertFalse(lastfm.is_connected({"Session_Key": "SK"}))


class CallbackUrlTest(unittest.TestCase):
    """The origin comes from the browser and ends up inside a URL we hand to
    Last.fm, so it is validated rather than trusted."""

    def test_builds_the_callback_from_a_plain_origin(self):
        self.assertEqual(lastfm.callback_url("http://truenas.local:3313"),
                         "http://truenas.local:3313/api/lastfm/callback")

    def test_https_and_bare_hosts_work(self):
        self.assertEqual(lastfm.callback_url("https://spinsense.example"),
                         "https://spinsense.example/api/lastfm/callback")

    def test_a_trailing_slash_is_tolerated(self):
        self.assertEqual(lastfm.callback_url("http://10.0.0.5:3313/"),
                         "http://10.0.0.5:3313/api/lastfm/callback")

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(lastfm.callback_url("  http://h:3313  "),
                         "http://h:3313/api/lastfm/callback")

    def test_anything_that_is_not_a_bare_http_origin_is_rejected(self):
        for bad in ("", "   ", "not a url", "ftp://h", "javascript:alert(1)",
                    "http://", "//h", "http://h/some/path", "http://h/?a=1",
                    "http://h/#frag"):
            with self.subTest(origin=bad):
                self.assertIsNone(lastfm.callback_url(bad))


class WebAuthUrlTest(unittest.TestCase):
    def test_carries_the_key_and_an_encoded_callback(self):
        url = lastfm.web_auth_url("KEY", "http://h:3313/api/lastfm/callback")
        self.assertTrue(url.startswith(lastfm.AUTH_URL))
        self.assertIn("api_key=KEY", url)
        # The callback is a URL inside a URL; unencoded it would truncate.
        self.assertIn("cb=http%3A%2F%2Fh%3A3313%2Fapi%2Flastfm%2Fcallback", url)

    def test_carries_no_token_of_ours(self):
        # The whole point of the redirect flow: Last.fm mints the token.
        self.assertNotIn("token=", lastfm.web_auth_url("KEY", "http://h/cb"))

    def test_manual_url_still_carries_a_token(self):
        self.assertIn("token=TOK", lastfm.auth_url("KEY", "TOK"))


class BuildScrobbleParamsTest(unittest.TestCase):
    def play(self, **over):
        base = {"artist": "M83", "title": "Midnight City", "timestamp": 1700000000,
                "album": "Hurry Up", "duration_secs": 244}
        base.update(over)
        return base

    def test_indexes_each_play(self):
        params = lastfm.build_scrobble_params([self.play(), self.play(title="Wait")])
        self.assertEqual(params["track[0]"], "Midnight City")
        self.assertEqual(params["track[1]"], "Wait")
        self.assertEqual(params["timestamp[0]"], "1700000000")

    def test_optional_fields_are_omitted_not_blank(self):
        params = lastfm.build_scrobble_params(
            [self.play(album=None, duration_secs=None)])
        self.assertNotIn("album[0]", params)
        self.assertNotIn("duration[0]", params)

    def test_unknown_album_is_not_sent(self):
        # "Unknown Album" is our own placeholder, not something to scrobble.
        params = lastfm.build_scrobble_params([self.play(album="Unknown Album")])
        self.assertNotIn("album[0]", params)


class ReadScrobbleResultTest(unittest.TestCase):
    """Last.fm returns a list with an @attr summary for a batch, but a bare
    object for a single scrobble, and the counts are strings."""

    def test_batch_shape(self):
        self.assertEqual(
            lastfm.read_scrobble_result(
                {"scrobbles": {"@attr": {"accepted": "3", "ignored": "1"}}}),
            (3, 1))

    def test_single_accepted_shape(self):
        self.assertEqual(
            lastfm.read_scrobble_result(
                {"scrobbles": {"scrobble": {"ignoredMessage": {"code": "0"}}}}),
            (1, 0))

    def test_single_ignored_shape(self):
        self.assertEqual(
            lastfm.read_scrobble_result(
                {"scrobbles": {"scrobble": {"ignoredMessage": {"code": "1"}}}}),
            (0, 1))

    def test_junk_is_zero_not_a_crash(self):
        for body in (None, {}, "nope", {"scrobbles": None},
                     {"scrobbles": {"@attr": {"accepted": "many"}}}):
            with self.subTest(body=body):
                self.assertEqual(lastfm.read_scrobble_result(body), (0, 0))


class StaleScrobbleTest(unittest.TestCase):
    def test_recent_plays_are_fresh(self):
        self.assertFalse(lastfm.is_too_old({"timestamp": 1000}, now=1000 + 3600))

    def test_two_week_old_plays_are_stale(self):
        self.assertTrue(
            lastfm.is_too_old({"timestamp": 1000}, now=1000 + 15 * 24 * 3600))


class _LastFmHarness(unittest.TestCase):
    """Temp config.json + temp DB, with _api_post replaced by a scripted queue."""

    def setUp(self):
        fd, self.cfg_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db_path)

        self._orig_cfg = config_manager.CONFIG_PATH
        config_manager.CONFIG_PATH = self.cfg_path
        self._orig_db = play_history.DB_PATH
        play_history.DB_PATH = self.db_path

        self.calls = []
        self.responses = []
        self._orig_post = lastfm._api_post

        async def fake_post(params):
            self.calls.append(params)
            if not self.responses:
                return {}, None
            return self.responses.pop(0)

        lastfm._api_post = fake_post

    def tearDown(self):
        lastfm._api_post = self._orig_post
        config_manager.CONFIG_PATH = self._orig_cfg
        play_history.DB_PATH = self._orig_db
        for path in (self.cfg_path, self.db_path):
            try:
                os.remove(path)
            except OSError:
                pass

    def write_config(self, **lastfm_fields):
        cfg = config_manager.get_default_config()
        cfg["LastFM"].update(lastfm_fields)
        with open(self.cfg_path, "w") as f:
            json.dump(cfg, f)

    def connected(self, **over):
        fields = {"Enabled": True, "API_Key": "K", "API_Secret": "S",
                  "Session_Key": "SK", "Username": "u", "Scrobble_Since": 0}
        fields.update(over)
        self.write_config(**fields)

    def add_play(self, title="T", played_at=None, listened=200, duration=213,
                 scrobbled_at=None):
        # Default to a recent play: Last.fm refuses timestamps over 14 days old,
        # so a fixture anchored at the epoch would be retired before submission.
        if played_at is None:
            played_at = int(time.time()) - 3600
        pid = play_history.record_play(title, "A", "Al", None, db_path=self.db_path,
                                       duration_secs=duration)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE plays SET played_at = ?, ended_at = ?, scrobbled_at = ? WHERE id = ?",
                (played_at, played_at + listened, scrobbled_at, pid))
        return pid

    def scrobbled_ids(self):
        with sqlite3.connect(self.db_path) as conn:
            return [r[0] for r in conn.execute(
                "SELECT id FROM plays WHERE scrobbled_at IS NOT NULL ORDER BY id")]


class AuthFlowTest(_LastFmHarness):
    def test_start_returns_the_approval_url(self):
        self.write_config()
        self.responses = [({"token": "TOK"}, None)]
        token, err = asyncio.run(lastfm.request_token("K", "S"))
        self.assertEqual(err, "")
        self.assertEqual(token, "TOK")
        self.assertIn("api_key=K", lastfm.auth_url("K", "TOK"))
        self.assertIn("token=TOK", lastfm.auth_url("K", "TOK"))

    def test_start_requires_both_credentials(self):
        token, err = asyncio.run(lastfm.request_token("K", ""))
        self.assertIsNone(token)
        self.assertTrue(err)
        self.assertEqual(self.calls, [])  # never hits the network

    def test_api_error_surfaces_its_message(self):
        self.write_config()
        self.responses = [({"error": 10, "message": "Invalid API key"}, None)]
        token, err = asyncio.run(lastfm.request_token("K", "S"))
        self.assertIsNone(token)
        self.assertEqual(err, "Invalid API key")

    def test_complete_persists_the_session_and_stamps_the_cutoff(self):
        self.write_config(API_Key="K", API_Secret="S")
        self.responses = [({"session": {"key": "SESSION", "name": "gregory"}}, None)]
        username, err = asyncio.run(lastfm.complete_auth("K", "S", "TOK"))
        self.assertEqual((username, err), ("gregory", ""))
        cfg = lastfm.settings()
        self.assertEqual(cfg["Session_Key"], "SESSION")
        self.assertTrue(cfg["Enabled"])
        # The cutoff is what stops a fresh connection dumping old history.
        self.assertGreater(cfg["Scrobble_Since"], 0)

    def test_complete_without_a_token_does_not_call_out(self):
        self.write_config()
        username, err = asyncio.run(lastfm.complete_auth("K", "S", ""))
        self.assertTrue(err)
        self.assertEqual(self.calls, [])

    def test_disconnect_clears_the_session_but_keeps_credentials(self):
        self.connected()
        lastfm.disconnect()
        cfg = lastfm.settings()
        self.assertEqual(cfg["Session_Key"], "")
        self.assertFalse(cfg["Enabled"])
        self.assertEqual(cfg["API_Key"], "K")      # the app registration stays
        self.assertEqual(cfg["API_Secret"], "S")


class PendingQueueTest(_LastFmHarness):
    def test_only_eligible_unscrobbled_plays_queue(self):
        self.connected()
        self.add_play("eligible", played_at=1000, listened=200)
        self.add_play("too short", played_at=2000, listened=5)
        self.add_play("already sent", played_at=3000, listened=200, scrobbled_at=1)
        titles = [p["title"] for p in lastfm.pending(db_path=self.db_path)]
        self.assertEqual(titles, ["eligible"])

    def test_scrobble_since_excludes_pre_connection_history(self):
        self.connected(Scrobble_Since=5000)
        self.add_play("before", played_at=1000, listened=200)
        self.add_play("after", played_at=9000, listened=200)
        titles = [p["title"] for p in lastfm.pending(db_path=self.db_path)]
        self.assertEqual(titles, ["after"])


class FlushTest(_LastFmHarness):
    def test_disconnected_never_calls_out(self):
        self.write_config()
        self.add_play()
        result = asyncio.run(lastfm.flush(db_path=self.db_path))
        self.assertEqual(self.calls, [])
        self.assertEqual(result["submitted"], 0)

    def test_successful_flush_marks_rows_and_never_resubmits(self):
        self.connected()
        pid = self.add_play(listened=200)
        self.responses = [({"scrobbles": {"@attr": {"accepted": "1", "ignored": "0"}}}, None)]
        result = asyncio.run(lastfm.flush(db_path=self.db_path))
        self.assertTrue(result["ok"])
        self.assertEqual(result["submitted"], 1)
        self.assertEqual(self.scrobbled_ids(), [pid])

        # Second flush has nothing to do — the whole point of the stamp.
        self.calls.clear()
        asyncio.run(lastfm.flush(db_path=self.db_path))
        self.assertEqual(self.calls, [])

    def test_network_failure_leaves_rows_pending(self):
        self.connected()
        self.add_play()
        self.responses = [(None, "Timed out talking to Last.fm")]
        result = asyncio.run(lastfm.flush(db_path=self.db_path))
        self.assertFalse(result["ok"])
        self.assertEqual(self.scrobbled_ids(), [])   # retried next sweep

    def test_retryable_service_error_leaves_rows_pending(self):
        self.connected()
        self.add_play()
        self.responses = [({"error": 16, "message": "Temporarily unavailable"}, None)]
        asyncio.run(lastfm.flush(db_path=self.db_path))
        self.assertEqual(self.scrobbled_ids(), [])

    def test_permanent_error_retires_rows_rather_than_looping(self):
        # Not retryable: resubmitting would fail identically forever.
        self.connected()
        self.add_play()
        self.responses = [({"error": 6, "message": "Invalid parameters"}, None)]
        asyncio.run(lastfm.flush(db_path=self.db_path))
        self.assertEqual(len(self.scrobbled_ids()), 1)

    def test_invalid_session_disables_scrobbling_and_asks_for_reauth(self):
        self.connected()
        self.add_play()
        self.responses = [({"error": 9, "message": "Invalid session key"}, None)]
        result = asyncio.run(lastfm.flush(db_path=self.db_path))
        self.assertTrue(result["needs_reauth"])
        self.assertFalse(lastfm.settings()["Enabled"])
        self.assertEqual(self.scrobbled_ids(), [])   # kept for after reconnecting

    def test_ignored_scrobbles_are_still_retired(self):
        self.connected()
        self.add_play()
        self.responses = [({"scrobbles": {"@attr": {"accepted": "0", "ignored": "1"}}}, None)]
        result = asyncio.run(lastfm.flush(db_path=self.db_path))
        self.assertEqual(result["ignored"], 1)
        self.assertEqual(len(self.scrobbled_ids()), 1)

    def test_stale_plays_are_retired_without_being_submitted(self):
        self.connected()
        old = self.add_play("ancient", played_at=int(time.time()) - 20 * 24 * 3600)
        asyncio.run(lastfm.flush(db_path=self.db_path))
        self.assertEqual(self.calls, [])            # never offered to Last.fm
        self.assertEqual(self.scrobbled_ids(), [old])

    def test_batch_is_capped(self):
        self.connected()
        base = int(time.time()) - 7200
        for i in range(lastfm.MAX_BATCH + 10):
            self.add_play(f"t{i}", played_at=base + i, listened=200)
        self.responses = [({"scrobbles": {"@attr": {"accepted": "50", "ignored": "0"}}}, None)]
        asyncio.run(lastfm.flush(db_path=self.db_path))
        submitted = [k for k in self.calls[0] if k.startswith("track[")]
        self.assertEqual(len(submitted), lastfm.MAX_BATCH)

    def test_the_request_is_signed_and_carries_the_session(self):
        self.connected()
        self.add_play()
        self.responses = [({"scrobbles": {"@attr": {"accepted": "1", "ignored": "0"}}}, None)]
        asyncio.run(lastfm.flush(db_path=self.db_path))
        sent = self.calls[0]
        self.assertEqual(sent["method"], "track.scrobble")
        self.assertEqual(sent["sk"], "SK")
        unsigned = {k: v for k, v in sent.items() if k not in ("api_sig", "format")}
        self.assertEqual(sent["api_sig"], lastfm.sign(unsigned, "S"))


class NowPlayingTest(_LastFmHarness):
    def test_silent_when_disconnected(self):
        self.write_config()
        asyncio.run(lastfm.update_now_playing({"artist": "A", "title": "T"}))
        self.assertEqual(self.calls, [])

    def test_silent_when_the_toggle_is_off(self):
        self.connected(Scrobble_Now_Playing=False)
        asyncio.run(lastfm.update_now_playing({"artist": "A", "title": "T"}))
        self.assertEqual(self.calls, [])

    def test_sends_when_connected(self):
        self.connected()
        asyncio.run(lastfm.update_now_playing(
            {"artist": "M83", "title": "Wait", "album": "Hurry Up", "duration_secs": 300}))
        sent = self.calls[0]
        self.assertEqual(sent["method"], "track.updateNowPlaying")
        self.assertEqual(sent["artist"], "M83")
        self.assertEqual(sent["album"], "Hurry Up")

    def test_incomplete_track_is_skipped(self):
        self.connected()
        asyncio.run(lastfm.update_now_playing({"artist": "", "title": "T"}))
        self.assertEqual(self.calls, [])

    def test_api_failure_is_swallowed(self):
        self.connected()
        self.responses = [(None, "boom")]
        asyncio.run(lastfm.update_now_playing({"artist": "A", "title": "T"}))  # no raise


class StatusTest(_LastFmHarness):
    def test_disconnected(self):
        self.write_config()
        st = lastfm.status(db_path=self.db_path)
        self.assertFalse(st["connected"])
        self.assertFalse(st["using_own_key"])
        self.assertEqual(st["pending"], 0)

    def test_own_credentials_without_a_session_is_not_connected(self):
        # Having a key — theirs or ours — is necessary but not sufficient;
        # scrobbling needs a per-user session from the approval flow.
        self.write_config(API_Key="K", API_Secret="S")
        st = lastfm.status(db_path=self.db_path)
        self.assertTrue(st["using_own_key"])
        self.assertTrue(st["can_connect"])
        self.assertFalse(st["connected"])

    def test_connected_reports_the_queue(self):
        self.connected()
        self.add_play(listened=200)
        st = lastfm.status(db_path=self.db_path)
        self.assertTrue(st["connected"])
        self.assertEqual(st["username"], "u")
        self.assertEqual(st["pending"], 1)


if __name__ == "__main__":
    unittest.main()


class ReviewWindowTest(_LastFmHarness):
    """Last.fm has no API to edit or delete a scrobble, so the only place a bad
    identification can be caught is before it is sent. Finished plays are held
    briefly; deleting or correcting one in that window actually prevents the
    scrobble rather than arriving too late to matter."""

    def test_a_fresh_play_is_held(self):
        self.connected(Submit_Delay_Mins=10)
        self.add_play(played_at=int(time.time()) - 60, listened=200)
        self.assertEqual(lastfm.pending(db_path=self.db_path), [])

    def test_an_aged_play_is_released(self):
        self.connected(Submit_Delay_Mins=10)
        self.add_play(played_at=int(time.time()) - 3600, listened=200)
        self.assertEqual(len(lastfm.pending(db_path=self.db_path)), 1)

    def test_include_held_sees_everything(self):
        self.connected(Submit_Delay_Mins=10)
        self.add_play("fresh", played_at=int(time.time()) - 60, listened=200)
        self.add_play("old", played_at=int(time.time()) - 3600, listened=200)
        self.assertEqual(len(lastfm.pending(db_path=self.db_path)), 1)
        self.assertEqual(
            len(lastfm.pending(db_path=self.db_path, include_held=True)), 2)

    def test_a_zero_delay_holds_nothing(self):
        self.connected(Submit_Delay_Mins=0)
        self.add_play(played_at=int(time.time()) - 5, listened=200)
        self.assertEqual(len(lastfm.pending(db_path=self.db_path)), 1)

    def test_the_sweep_leaves_held_plays_alone(self):
        self.connected(Submit_Delay_Mins=10)
        self.add_play(played_at=int(time.time()) - 60, listened=200)
        result = asyncio.run(lastfm.flush(db_path=self.db_path))
        self.assertEqual(self.calls, [])
        self.assertEqual(result["held"], 1)
        self.assertIn("review window", result["detail"])

    def test_send_now_releases_them(self):
        self.connected(Submit_Delay_Mins=10)
        self.add_play(played_at=int(time.time()) - 60, listened=200)
        self.responses = [({"scrobbles": {"@attr": {"accepted": "1", "ignored": "0"}}}, None)]
        result = asyncio.run(lastfm.flush(db_path=self.db_path, release_held=True))
        self.assertEqual(result["submitted"], 1)
        self.assertEqual(len(self.scrobbled_ids()), 1)

    def test_deleting_a_held_play_prevents_the_scrobble(self):
        # The whole point of the window: correcting or removing a bad
        # identification in SpinSense stops it ever reaching Last.fm.
        self.connected(Submit_Delay_Mins=10)
        pid = self.add_play(played_at=int(time.time()) - 60, listened=200)
        play_history.delete_play(pid, db_path=self.db_path)
        self.assertEqual(
            lastfm.pending(db_path=self.db_path, include_held=True), [])

    def test_status_reports_what_is_waiting_and_what_is_held(self):
        self.connected(Submit_Delay_Mins=10)
        self.add_play("fresh", played_at=int(time.time()) - 60, listened=200)
        self.add_play("old", played_at=int(time.time()) - 3600, listened=200)
        st = lastfm.status(db_path=self.db_path)
        self.assertEqual(st["pending"], 1)
        self.assertEqual(st["held"], 1)
        self.assertEqual(st["delay_mins"], 10)

    def test_a_malformed_delay_falls_back_to_the_default(self):
        self.connected(Submit_Delay_Mins=10)
        self.assertEqual(lastfm.submit_delay_secs({"Submit_Delay_Mins": "soon"}), 600)
        self.assertEqual(lastfm.submit_delay_secs({"Submit_Delay_Mins": -5}), 0)
