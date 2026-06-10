import asyncio
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

import core_engine  # noqa: E402


class BuildStatusPayloadTest(unittest.TestCase):
    def test_playing_carries_track_and_phase(self):
        st = {"in_song": True, "title": "T", "artist": "A", "album": "Al",
              "art_url": "u", "isrc": "i", "genre": "g", "release_year": 2001}
        msg = core_engine.build_status_payload("playing", 0.4, st)
        self.assertEqual(msg["type"], "live_status")
        p = msg["payload"]
        self.assertEqual(p["phase"], "playing")
        self.assertEqual(p["status_msg"], "Playing")
        self.assertEqual(p["rms_level"], 0.4)
        self.assertEqual(p["track"]["title"], "T")
        self.assertEqual(p["track"]["release_year"], 2001)

    def test_scanning_keeps_current_track_but_marks_phase(self):
        # Invariant: phase frames keep the existing track so dedupe is unaffected.
        st = {"in_song": True, "title": "T", "artist": "A", "album": "Al", "art_url": "u"}
        p = core_engine.build_status_payload("scanning", 0.2, st)["payload"]
        self.assertEqual(p["phase"], "scanning")
        self.assertEqual(p["track"]["title"], "T")

    def test_listening_when_not_in_song(self):
        p = core_engine.build_status_payload("listening", 0.0, {"in_song": False})["payload"]
        self.assertEqual(p["status_msg"], "Listening")
        self.assertEqual(p["track"]["title"], "")


class RescanCommandTest(unittest.TestCase):
    def setUp(self):
        core_engine.state["force_scan"] = False
        core_engine.state["back_off"] = True

    def test_rescan_sets_force_and_clears_backoff(self):
        reply = asyncio.run(core_engine._handle_command({"cmd": "rescan"}))
        self.assertEqual(reply, {"ok": True})
        self.assertTrue(core_engine.state["force_scan"])
        self.assertFalse(core_engine.state["back_off"])

    def test_unknown_cmd_still_rejected(self):
        reply = asyncio.run(core_engine._handle_command({"cmd": "bogus"}))
        self.assertFalse(reply["ok"])


class RecognizeRetryTest(unittest.TestCase):
    def setUp(self):
        self.phases = []
        self.handled = []
        # Capture phase publishes instead of hitting the socket.
        async def fake_publish(phase):
            self.phases.append(phase)
        async def fake_capture(sample_len):
            return b""
        async def fake_handle(track):
            self.handled.append(track)
            core_engine.state["in_song"] = True
            core_engine.state["back_off"] = False
        self._orig = (core_engine._publish_phase, core_engine._capture_sample,
                      core_engine._handle_match, core_engine._identify_shazam)
        core_engine._publish_phase = fake_publish
        core_engine._capture_sample = fake_capture
        core_engine._handle_match = fake_handle
        core_engine.state["back_off"] = False
        core_engine.state["in_song"] = False
        self._orig_wait = core_engine.runtime["rescan_wait"]
        core_engine.runtime["rescan_wait"] = 0  # don't sleep in tests

    def tearDown(self):
        (core_engine._publish_phase, core_engine._capture_sample,
         core_engine._handle_match, core_engine._identify_shazam) = self._orig
        core_engine.runtime["rescan_wait"] = self._orig_wait

    def test_all_miss_sets_no_match_and_backoff(self):
        async def always_none(_wav):
            return None
        core_engine._identify_shazam = always_none
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(
            self.phases,
            ["scanning", "identifying", "scanning", "retrying",
             "scanning", "retrying", "no_match"],
        )
        self.assertEqual(self.handled, [])
        self.assertTrue(core_engine.state["back_off"])
        self.assertFalse(core_engine.state["in_song"])

    def test_match_on_third_attempt_handles_and_no_backoff(self):
        self.calls = 0
        async def third(_wav):
            self.calls += 1
            return {"title": "Hit"} if self.calls == 3 else None
        core_engine._identify_shazam = third
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.handled, [{"title": "Hit"}])
        self.assertFalse(core_engine.state["back_off"])
        self.assertNotIn("no_match", self.phases)


class ScanDecisionTest(unittest.TestCase):
    def d(self, vol, thr, in_song, sc, new_song_silence, back_off):
        return core_engine._scan_decision(vol, thr, in_song, sc, new_song_silence, back_off)

    def test_loud_idle_scans(self):
        # New onset (not in a song) always scans, regardless of counter.
        self.assertEqual(self.d(0.5, 0.1, False, 0, 3, False), "scan")

    def test_loud_in_song_steady_ticks(self):
        self.assertEqual(self.d(0.5, 0.1, True, 0, 3, False), "tick")

    def test_brief_dip_below_interval_does_not_rescan(self):
        # 2s gap with a 3s interval — treat as the same song, just tick.
        self.assertEqual(self.d(0.5, 0.1, True, 2, 3, False), "tick")

    def test_gap_at_interval_rescans(self):
        # Gap reached the interval — a new song may have started.
        self.assertEqual(self.d(0.5, 0.1, True, 3, 3, False), "scan")

    def test_gap_past_interval_rescans(self):
        self.assertEqual(self.d(0.5, 0.1, True, 5, 3, False), "scan")

    def test_loud_but_backoff_waits_for_gap(self):
        self.assertEqual(self.d(0.5, 0.1, False, 0, 3, True), "wait_gap")

    def test_quiet_is_silence(self):
        self.assertEqual(self.d(0.0, 0.1, True, 0, 3, False), "silence")


class IdleBlipTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        async def fake_itunes(artist, title): return (None, None)
        async def fake_img(url): return ""
        def fake_publish_state(status, artist="", title="", album="", art_url="", art_base64=""):
            self.events.append(f"mqtt:{status}")
        async def fake_idle_blip(): self.events.append("idle_blip")
        async def fake_phase(p): self.events.append(f"phase:{p}")
        self._orig = (core_engine.fetch_itunes_metadata, core_engine.fetch_image_base64,
                      core_engine.publish_state, core_engine._publish_idle_blip, core_engine._publish_phase)
        core_engine.fetch_itunes_metadata = fake_itunes
        core_engine.fetch_image_base64 = fake_img
        core_engine.publish_state = fake_publish_state
        core_engine._publish_idle_blip = fake_idle_blip
        core_engine._publish_phase = fake_phase
        core_engine.state["last_song"] = ""

    def tearDown(self):
        (core_engine.fetch_itunes_metadata, core_engine.fetch_image_base64,
         core_engine.publish_state, core_engine._publish_idle_blip, core_engine._publish_phase) = self._orig
        core_engine.runtime["retrigger_on_track_change"] = False

    def test_blip_between_stop_and_play_when_flag_on(self):
        core_engine.runtime["retrigger_on_track_change"] = True
        asyncio.run(core_engine._handle_match({"title": "T", "artist": "A"}))
        self.assertIn("idle_blip", self.events)
        self.assertLess(self.events.index("mqtt:stopped"), self.events.index("idle_blip"))
        self.assertLess(self.events.index("idle_blip"), self.events.index("mqtt:playing"))

    def test_no_blip_when_flag_off(self):
        core_engine.runtime["retrigger_on_track_change"] = False
        asyncio.run(core_engine._handle_match({"title": "T2", "artist": "A"}))
        self.assertNotIn("idle_blip", self.events)
        self.assertNotIn("mqtt:stopped", self.events)
        self.assertIn("mqtt:playing", self.events)


class ShazamNormalizeTest(unittest.TestCase):
    def setUp(self):
        self._orig = core_engine.shazam.recognize

    def tearDown(self):
        core_engine.shazam.recognize = self._orig

    def test_maps_shazam_dict_to_normalized(self):
        async def fake_recognize(_wav):
            return {"track": {
                "title": "Song", "subtitle": "Band",
                "images": {"coverarthq": "hq.jpg", "coverart": "lq.jpg"},
                "isrc": "US1234567890",
                "genres": {"primary": "Rock"},
                "sections": [{"metadata": [{"title": "Released", "text": "1979"}]}],
            }}
        core_engine.shazam.recognize = fake_recognize
        n = asyncio.run(core_engine._identify_shazam(b""))
        self.assertEqual(n["title"], "Song")
        self.assertEqual(n["artist"], "Band")
        self.assertEqual(n["art_url"], "hq.jpg")     # coverarthq preferred
        self.assertEqual(n["isrc"], "US1234567890")
        self.assertEqual(n["genre"], "Rock")
        self.assertEqual(n["release_year"], 1979)
        self.assertIsNone(n["album"])                # Shazam gives no reliable album

    def test_no_track_returns_none(self):
        async def fake_recognize(_wav):
            return {"matches": []}
        core_engine.shazam.recognize = fake_recognize
        self.assertIsNone(asyncio.run(core_engine._identify_shazam(b"")))


class HandleMatchArtTest(unittest.TestCase):
    def setUp(self):
        self.published = []
        async def fake_itunes(artist, title):
            return self.itunes_return
        async def fake_img(url):
            return "b64" if url else ""
        async def fake_phase(p):
            return None
        async def fake_blip():
            return None
        def fake_publish(status, artist="", title="", album="", art_url="", art_base64=""):
            self.published.append(dict(status=status, album=album, art_url=art_url))
        self._orig = (core_engine.fetch_itunes_metadata, core_engine.fetch_image_base64,
                      core_engine._publish_phase, core_engine._publish_idle_blip,
                      core_engine.publish_state)
        core_engine.fetch_itunes_metadata = fake_itunes
        core_engine.fetch_image_base64 = fake_img
        core_engine._publish_phase = fake_phase
        core_engine._publish_idle_blip = fake_blip
        core_engine.publish_state = fake_publish
        core_engine.state["last_song"] = ""
        self.itunes_return = (None, None)

    def tearDown(self):
        (core_engine.fetch_itunes_metadata, core_engine.fetch_image_base64,
         core_engine._publish_phase, core_engine._publish_idle_blip,
         core_engine.publish_state) = self._orig

    def test_itunes_art_is_primary(self):
        self.itunes_return = ("iTunes Album", "itunes_art.jpg")
        n = {"title": "T", "artist": "A", "album": "Backend Album",
             "art_url": "backend_art.jpg", "isrc": None, "genre": None, "release_year": None}
        asyncio.run(core_engine._handle_match(n))
        self.assertEqual(core_engine.state["art_url"], "itunes_art.jpg")
        self.assertEqual(core_engine.state["album"], "iTunes Album")

    def test_backend_art_used_when_itunes_has_none(self):
        self.itunes_return = (None, None)
        n = {"title": "T", "artist": "A", "album": "Backend Album",
             "art_url": "backend_art.jpg", "isrc": None, "genre": None, "release_year": None}
        asyncio.run(core_engine._handle_match(n))
        self.assertEqual(core_engine.state["art_url"], "backend_art.jpg")
        self.assertEqual(core_engine.state["album"], "Backend Album")

    def test_falls_back_to_unknown_album_and_empty_art(self):
        self.itunes_return = (None, None)
        n = {"title": "T", "artist": "A", "album": None,
             "art_url": None, "isrc": None, "genre": None, "release_year": None}
        asyncio.run(core_engine._handle_match(n))
        self.assertEqual(core_engine.state["art_url"], "")
        self.assertEqual(core_engine.state["album"], "Unknown Album")


class EscalatingSampleTest(unittest.TestCase):
    def setUp(self):
        self.lengths = []
        self.sleeps = []

        async def fake_phase(p):
            return None

        async def fake_capture(sample_len):
            self.lengths.append(sample_len)
            return b""

        async def always_none(_wav):
            return None

        async def fake_pause(seconds):
            self.sleeps.append(seconds)

        self._orig = (core_engine._publish_phase, core_engine._capture_sample,
                      core_engine._identify_shazam, core_engine._rescan_pause)
        self._orig_base = core_engine.runtime["sample_len"]
        self._orig_wait = core_engine.runtime["rescan_wait"]
        core_engine._publish_phase = fake_phase
        core_engine._capture_sample = fake_capture
        core_engine._identify_shazam = always_none
        core_engine._rescan_pause = fake_pause
        core_engine.runtime["sample_len"] = 5.0
        core_engine.runtime["rescan_wait"] = 5.0
        core_engine.state["back_off"] = False
        core_engine.state["in_song"] = False

    def tearDown(self):
        (core_engine._publish_phase, core_engine._capture_sample,
         core_engine._identify_shazam, core_engine._rescan_pause) = self._orig
        core_engine.runtime["sample_len"] = self._orig_base
        core_engine.runtime["rescan_wait"] = self._orig_wait

    def test_sample_length_escalates_1x_2x_3x(self):
        asyncio.run(core_engine.recognize_audio())
        # base 5s -> 5, 10, 15 across the three attempts.
        self.assertEqual(self.lengths, [5.0, 10.0, 15.0])

    def test_wait_runs_between_attempts_not_after_last(self):
        asyncio.run(core_engine.recognize_audio())
        # Two gaps between three attempts; no trailing sleep.
        self.assertEqual(self.sleeps, [5.0, 5.0])

    def test_cap_limits_runaway_length(self):
        core_engine.runtime["sample_len"] = 30.0  # 3x would be 90s
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.lengths, [30.0, 60.0, 60.0])  # capped at 60


class AuddAdapterTest(unittest.TestCase):
    def setUp(self):
        self._orig_post = core_engine._audd_post
        self._orig_token = core_engine.runtime["audd_token"]
        core_engine.runtime["audd_token"] = "tok"

    def tearDown(self):
        core_engine._audd_post = self._orig_post
        core_engine.runtime["audd_token"] = self._orig_token

    def test_maps_audd_result_to_normalized(self):
        result = {
            "artist": "Band", "title": "Song", "album": "The Album",
            "release_date": "1979-10-12",
            "apple_music": {
                "genreNames": ["Rock", "Pop"], "isrc": "US1234567890",
                "artwork": {"url": "https://art/{w}x{h}bb.jpg"},
            },
        }
        n = core_engine._audd_to_normalized(result)
        self.assertEqual(n["title"], "Song")
        self.assertEqual(n["artist"], "Band")
        self.assertEqual(n["album"], "The Album")
        self.assertEqual(n["release_year"], 1979)
        self.assertEqual(n["isrc"], "US1234567890")
        self.assertEqual(n["genre"], "Rock")
        self.assertEqual(n["art_url"], "https://art/600x600bb.jpg")  # {w}/{h} resolved

    def test_maps_missing_fields_to_none(self):
        n = core_engine._audd_to_normalized({"artist": "A", "title": "T"})
        self.assertIsNone(n["album"])
        self.assertIsNone(n["isrc"])
        self.assertIsNone(n["genre"])
        self.assertIsNone(n["release_year"])
        self.assertIsNone(n["art_url"])

    def test_identify_success(self):
        async def fake_post(_wav, _token):
            return {"status": "success", "result": {"artist": "A", "title": "T"}}
        core_engine._audd_post = fake_post
        n = asyncio.run(core_engine._identify_audd(b""))
        self.assertEqual(n["title"], "T")

    def test_identify_no_match_returns_none(self):
        async def fake_post(_wav, _token):
            return {"status": "success", "result": None}
        core_engine._audd_post = fake_post
        self.assertIsNone(asyncio.run(core_engine._identify_audd(b"")))

    def test_identify_network_error_returns_none(self):
        async def fake_post(_wav, _token):
            return None  # _audd_post swallows errors -> None
        core_engine._audd_post = fake_post
        self.assertIsNone(asyncio.run(core_engine._identify_audd(b"")))

    def test_identify_no_token_skips_post(self):
        core_engine.runtime["audd_token"] = ""
        self.called = False
        async def fake_post(_wav, _token):
            self.called = True
            return None
        core_engine._audd_post = fake_post
        self.assertIsNone(asyncio.run(core_engine._identify_audd(b"")))
        self.assertFalse(self.called)


class AuddFallbackFlowTest(unittest.TestCase):
    def setUp(self):
        self.handled = []
        self.audd_calls = 0
        self.shazam_calls = 0
        async def fake_phase(p):
            return None
        async def fake_capture(sample_len):
            return b""
        async def fake_pause(seconds):
            return None
        async def fake_handle(track):
            self.handled.append(track)
            core_engine.state["in_song"] = True
            core_engine.state["back_off"] = False
        self._orig = (core_engine._publish_phase, core_engine._capture_sample,
                      core_engine._rescan_pause, core_engine._handle_match,
                      core_engine._identify_shazam, core_engine._identify_audd)
        self._orig_flags = (core_engine.runtime["fallback_provider"],
                            core_engine.runtime["audd_token"],
                            core_engine.runtime["rescan_wait"])
        core_engine._publish_phase = fake_phase
        core_engine._capture_sample = fake_capture
        core_engine._rescan_pause = fake_pause
        core_engine._handle_match = fake_handle
        core_engine.runtime["rescan_wait"] = 0
        core_engine.state["back_off"] = False
        core_engine.state["in_song"] = False

    def tearDown(self):
        (core_engine._publish_phase, core_engine._capture_sample,
         core_engine._rescan_pause, core_engine._handle_match,
         core_engine._identify_shazam, core_engine._identify_audd) = self._orig
        (core_engine.runtime["fallback_provider"], core_engine.runtime["audd_token"],
         core_engine.runtime["rescan_wait"]) = self._orig_flags

    def _set(self, shazam_fn, audd_fn, provider="audd"):
        core_engine._identify_shazam = shazam_fn
        core_engine._identify_audd = audd_fn
        core_engine.runtime["fallback_provider"] = provider
        core_engine.runtime["audd_token"] = "tok"

    def test_audd_rescues_after_first_shazam_miss(self):
        async def shazam(_w):
            self.shazam_calls += 1
            return None
        async def audd(_w):
            self.audd_calls += 1
            return {"title": "AudD Hit", "artist": "A"}
        self._set(shazam, audd)
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.handled, [{"title": "AudD Hit", "artist": "A"}])
        self.assertEqual(self.shazam_calls, 1)   # broke out after attempt 0
        self.assertEqual(self.audd_calls, 1)
        self.assertFalse(core_engine.state["back_off"])

    def test_audd_miss_continues_shazam_escalation(self):
        async def shazam(_w):
            self.shazam_calls += 1
            return None
        async def audd(_w):
            self.audd_calls += 1
            return None
        self._set(shazam, audd)
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.handled, [])
        self.assertEqual(self.shazam_calls, 3)   # full ladder ran
        self.assertEqual(self.audd_calls, 1)     # AudD only on attempt 0
        self.assertTrue(core_engine.state["back_off"])

    def test_disabled_never_calls_audd(self):
        async def shazam(_w):
            self.shazam_calls += 1
            return None
        async def audd(_w):
            self.audd_calls += 1
            return {"title": "X", "artist": "Y"}
        self._set(shazam, audd, provider="none")
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.audd_calls, 0)
        self.assertEqual(self.handled, [])

class FallbackDispatchTest(unittest.TestCase):
    def setUp(self):
        self._orig = (core_engine._identify_audd, core_engine._identify_acoustid,
                      core_engine.runtime["fallback_provider"])
        self.calls = []
        async def fake_audd(_w):
            self.calls.append("audd"); return {"title": "AudD", "artist": "A"}
        async def fake_acoustid(_w):
            self.calls.append("acoustid"); return {"title": "AcoustID", "artist": "A"}
        core_engine._identify_audd = fake_audd
        core_engine._identify_acoustid = fake_acoustid

    def tearDown(self):
        (core_engine._identify_audd, core_engine._identify_acoustid,
         core_engine.runtime["fallback_provider"]) = self._orig

    def test_none_routes_to_nothing(self):
        core_engine.runtime["fallback_provider"] = "none"
        self.assertIsNone(asyncio.run(core_engine._identify_fallback(b"")))
        self.assertEqual(self.calls, [])

    def test_audd_routes_to_audd(self):
        core_engine.runtime["fallback_provider"] = "audd"
        n = asyncio.run(core_engine._identify_fallback(b""))
        self.assertEqual(n["title"], "AudD")
        self.assertEqual(self.calls, ["audd"])

    def test_acoustid_routes_to_acoustid(self):
        core_engine.runtime["fallback_provider"] = "acoustid"
        n = asyncio.run(core_engine._identify_fallback(b""))
        self.assertEqual(n["title"], "AcoustID")
        self.assertEqual(self.calls, ["acoustid"])


class AcoustidAdapterTest(unittest.TestCase):
    def setUp(self):
        self._orig = (core_engine._chromaprint_fingerprint, core_engine._acoustid_lookup,
                      core_engine.ACOUSTID_CLIENT_KEY)

    def tearDown(self):
        (core_engine._chromaprint_fingerprint, core_engine._acoustid_lookup,
         core_engine.ACOUSTID_CLIENT_KEY) = self._orig

    def test_maps_best_score_result(self):
        results = [
            {"score": 0.3, "recordings": [{"title": "Wrong", "artists": [{"name": "X"}]}]},
            {"score": 0.9, "recordings": [{
                "title": "Right", "artists": [{"name": "Tones and I"}],
                "releasegroups": [{"title": "The Album"}]}]},
        ]
        n = core_engine._acoustid_to_normalized(results)
        self.assertEqual(n["title"], "Right")
        self.assertEqual(n["artist"], "Tones and I")
        self.assertEqual(n["album"], "The Album")
        self.assertIsNone(n["art_url"])

    def test_joins_multiple_artists(self):
        results = [{"score": 1.0, "recordings": [{
            "title": "T", "artists": [{"name": "A"}, {"name": "B"}]}]}]
        self.assertEqual(core_engine._acoustid_to_normalized(results)["artist"], "A, B")

    def test_no_recordings_or_title_returns_none(self):
        self.assertIsNone(core_engine._acoustid_to_normalized([]))
        self.assertIsNone(core_engine._acoustid_to_normalized([{"score": 1.0, "recordings": []}]))
        self.assertIsNone(core_engine._acoustid_to_normalized(
            [{"score": 1.0, "recordings": [{"artists": [{"name": "A"}]}]}]))  # no title

    def test_identify_success(self):
        async def fake_fp(_w):
            return (180, "AQADtMk")
        async def fake_lookup(_d, _f):
            return {"status": "ok", "results": [
                {"score": 1.0, "recordings": [{"title": "T", "artists": [{"name": "A"}]}]}]}
        core_engine._chromaprint_fingerprint = fake_fp
        core_engine._acoustid_lookup = fake_lookup
        core_engine.ACOUSTID_CLIENT_KEY = "key"
        n = asyncio.run(core_engine._identify_acoustid(b""))
        self.assertEqual(n["title"], "T")

    def test_identify_no_results_returns_none(self):
        async def fake_fp(_w):
            return (180, "fp")
        async def fake_lookup(_d, _f):
            return {"status": "ok", "results": []}
        core_engine._chromaprint_fingerprint = fake_fp
        core_engine._acoustid_lookup = fake_lookup
        core_engine.ACOUSTID_CLIENT_KEY = "key"
        self.assertIsNone(asyncio.run(core_engine._identify_acoustid(b"")))

    def test_identify_fingerprint_failure_skips_lookup(self):
        self.lookup_called = False
        async def fake_fp(_w):
            return None
        async def fake_lookup(_d, _f):
            self.lookup_called = True
            return {}
        core_engine._chromaprint_fingerprint = fake_fp
        core_engine._acoustid_lookup = fake_lookup
        core_engine.ACOUSTID_CLIENT_KEY = "key"
        self.assertIsNone(asyncio.run(core_engine._identify_acoustid(b"")))
        self.assertFalse(self.lookup_called)

    def test_identify_no_key_skips_fingerprint(self):
        self.fp_called = False
        async def fake_fp(_w):
            self.fp_called = True
            return (1, "x")
        core_engine._chromaprint_fingerprint = fake_fp
        core_engine.ACOUSTID_CLIENT_KEY = ""
        self.assertIsNone(asyncio.run(core_engine._identify_acoustid(b"")))
        self.assertFalse(self.fp_called)


class SilenceStepTest(unittest.TestCase):
    def s(self, sc, in_song, back_off, ns=3, stop=5):
        return core_engine._silence_step(sc, in_song, back_off, ns, stop)

    def test_backoff_survives_brief_dip(self):
        # The phantom post-scan zero / a 1-tick dip: below the interval -> stays armed.
        self.assertEqual(self.s(0, False, True), (1, True, False))

    def test_backoff_clears_after_qualifying_gap(self):
        # silence_counter reaches new_song_silence (3) -> back_off clears.
        self.assertEqual(self.s(2, False, True), (3, False, False))

    def test_in_song_stops_at_stopped_silence(self):
        self.assertEqual(self.s(4, True, False), (5, False, True))

    def test_in_song_below_stop_does_not_stop(self):
        self.assertEqual(self.s(3, True, False), (4, False, False))

    def test_not_in_song_never_stops(self):
        self.assertEqual(self.s(10, False, False), (11, False, False))
