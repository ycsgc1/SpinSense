"""Round-trip + validation checks for config_manager.

The module reads/writes a single JSON file at CONFIG_PATH; the tests redirect
that path at a tempfile so production config.json is never touched.
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

import config_manager  # noqa: E402


class ConfigRoundTripTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        # Empty the file so load_config sees a "missing/invalid" file and
        # regenerates from defaults. The tempfile creation leaves a 0-byte file
        # behind, which json.load() rejects.
        self._orig_path = config_manager.CONFIG_PATH
        config_manager.CONFIG_PATH = self.path

    def tearDown(self):
        config_manager.CONFIG_PATH = self._orig_path
        try:
            os.remove(self.path)
        except OSError:
            pass

    def test_defaults_round_trip(self):
        defaults = config_manager.get_default_config()
        self.assertTrue(config_manager.save_config(defaults))
        loaded = config_manager.load_config()
        self.assertEqual(loaded, defaults)

    def test_modified_values_persist(self):
        cfg = config_manager.get_default_config()
        cfg["Audio"]["Volume_Threshold"] = 0.0123
        cfg["Hardware"]["Mic_Device"] = "Scarlett Solo USB"
        cfg["LastFM"]["Submit_Delay_Mins"] = 45
        self.assertTrue(config_manager.save_config(cfg))

        # Force a fresh read from disk.
        loaded = config_manager.load_config()
        self.assertAlmostEqual(loaded["Audio"]["Volume_Threshold"], 0.0123)
        self.assertEqual(loaded["Hardware"]["Mic_Device"], "Scarlett Solo USB")
        self.assertEqual(loaded["LastFM"]["Submit_Delay_Mins"], 45)

    def test_invalid_int_type_rejected(self):
        cfg = config_manager.get_default_config()
        cfg["LastFM"]["Submit_Delay_Mins"] = "not-a-number"
        self.assertFalse(config_manager.save_config(cfg))

    def test_invalid_threshold_type_rejected(self):
        cfg = config_manager.get_default_config()
        cfg["Audio"]["Volume_Threshold"] = "loud"
        self.assertFalse(config_manager.save_config(cfg))

    def test_load_with_missing_file_regenerates_defaults(self):
        # Remove the file so the "not os.path.exists" branch fires.
        os.remove(self.path)
        loaded = config_manager.load_config()
        self.assertEqual(loaded, config_manager.get_default_config())
        # And the file should be on disk now.
        self.assertTrue(os.path.exists(self.path))
        with open(self.path) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk, config_manager.get_default_config())

    def test_setup_wizard_state_defaults_pending(self):
        defaults = config_manager.get_default_config()
        self.assertEqual(defaults["System"]["Setup_Wizard_State"], "pending")

    def test_setup_wizard_state_accepts_legal_values(self):
        for value in ("pending", "skipped", "completed"):
            cfg = config_manager.get_default_config()
            cfg["System"]["Setup_Wizard_State"] = value
            self.assertTrue(
                config_manager.save_config(cfg),
                f"expected '{value}' to validate",
            )
            loaded = config_manager.load_config()
            self.assertEqual(loaded["System"]["Setup_Wizard_State"], value)

    def test_setup_wizard_state_rejects_unknown(self):
        cfg = config_manager.get_default_config()
        cfg["System"]["Setup_Wizard_State"] = "abandoned"
        self.assertFalse(config_manager.save_config(cfg))

    def test_default_volume_threshold_is_minus_40_db(self):
        # 0.01 = -40 dB exactly; cleaner than 0.0062 / 0.015 once we display in dB.
        defaults = config_manager.get_default_config()
        self.assertEqual(defaults["Audio"]["Volume_Threshold"], 0.01)

    def test_retrigger_on_track_change_round_trips(self):
        # Default must be False.
        defaults = config_manager.get_default_config()
        self.assertFalse(defaults["Audio"]["Retrigger_On_Track_Change"])

        # Setting to True must survive a save+load cycle.
        cfg = config_manager.get_default_config()
        cfg["Audio"]["Retrigger_On_Track_Change"] = True
        self.assertTrue(config_manager.save_config(cfg))
        loaded = config_manager.load_config()
        self.assertTrue(loaded["Audio"]["Retrigger_On_Track_Change"])

    def test_new_song_silence_default_is_3(self):
        defaults = config_manager.get_default_config()
        self.assertEqual(defaults["Audio"]["New_Song_Silence_Interval"], 3.0)

    def test_rescan_wait_interval_default_and_round_trips(self):
        defaults = config_manager.get_default_config()
        self.assertEqual(defaults["Audio"]["Rescan_Wait_Interval"], 5.0)

        cfg = config_manager.get_default_config()
        cfg["Audio"]["Rescan_Wait_Interval"] = 7.5
        self.assertTrue(config_manager.save_config(cfg))
        loaded = config_manager.load_config()
        self.assertAlmostEqual(loaded["Audio"]["Rescan_Wait_Interval"], 7.5)

    def test_fallback_provider_defaults_none(self):
        defaults = config_manager.get_default_config()
        self.assertEqual(defaults["Audio"]["Fallback_Provider"], "none")
        self.assertEqual(defaults["Audio"]["AudD_API_Token"], "")

    def test_fallback_provider_round_trips(self):
        for provider in ("audd", "acoustid", "none"):
            cfg = config_manager.get_default_config()
            cfg["Audio"]["Fallback_Provider"] = provider
            self.assertTrue(config_manager.save_config(cfg), f"{provider} should validate")
            loaded = config_manager.load_config()
            self.assertEqual(loaded["Audio"]["Fallback_Provider"], provider)

    def test_fallback_provider_rejects_unknown(self):
        cfg = config_manager.get_default_config()
        cfg["Audio"]["Fallback_Provider"] = "spotify"
        self.assertFalse(config_manager.save_config(cfg))

    def test_audd_token_round_trips(self):
        cfg = config_manager.get_default_config()
        cfg["Audio"]["AudD_API_Token"] = "tok_abc123"
        self.assertTrue(config_manager.save_config(cfg))
        loaded = config_manager.load_config()
        self.assertEqual(loaded["Audio"]["AudD_API_Token"], "tok_abc123")


class CorruptConfigIsNeverOverwrittenTest(unittest.TestCase):
    """load_config() runs on every page request (the setup-wizard middleware).
    It used to regenerate defaults *and write them back* on any read failure, so
    one truncated read — the engine writing the file, a half-saved hand edit —
    permanently replaced the AudD token, the Last.fm session and the calibrated
    threshold. Serving defaults is fine; persisting them is data loss."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._orig = config_manager.CONFIG_PATH
        config_manager.CONFIG_PATH = self.path

    def tearDown(self):
        config_manager.CONFIG_PATH = self._orig
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _load_and_assert_untouched(self, raw):
        with open(self.path, "w") as f:
            f.write(raw)
        cfg = config_manager.load_config()
        self.assertEqual(cfg["Audio"]["Volume_Threshold"], 0.01)  # defaults served
        with open(self.path) as f:
            self.assertEqual(f.read(), raw)                       # file untouched

    def test_truncated_json_is_left_on_disk(self):
        self._load_and_assert_untouched('{"Audio": {"Volume_Threshold": 0.0')

    def test_invalid_types_are_left_on_disk(self):
        self._load_and_assert_untouched(
            '{"Audio": {"Volume_Threshold": "not a number"}}')

    def test_a_recoverable_file_survives_to_the_next_read(self):
        # The real scenario: a bad read now, a good read a moment later.
        good = json.dumps({"Audio": {"AudD_API_Token": "secret"}})
        with open(self.path, "w") as f:
            f.write(good[:12])
        config_manager.load_config()
        with open(self.path, "w") as f:
            f.write(good)
        self.assertEqual(
            config_manager.load_config()["Audio"]["AudD_API_Token"], "secret")

    def test_a_missing_file_is_still_created(self):
        os.remove(self.path)
        config_manager.load_config()
        self.assertTrue(os.path.exists(self.path))


class TestTrackEndConfig(unittest.TestCase):
    """Track-end prediction knobs. The defaults must match
    core/core_engine.DEFAULT_CONFIG["Audio"] — the engine reads config.json raw,
    so a drift here means the two processes disagree about the feature."""

    def test_defaults(self):
        from config_manager import SpinSenseConfig
        audio = SpinSenseConfig().dict()["Audio"]
        self.assertTrue(audio["Track_End_Detection"])
        self.assertEqual(audio["Track_End_Grace_Secs"], 20.0)

    def test_defaults_match_the_engine(self):
        HERE = os.path.dirname(os.path.abspath(__file__))
        core_dir = os.path.join(os.path.dirname(os.path.dirname(HERE)), "core")
        if core_dir not in sys.path:
            sys.path.insert(0, core_dir)
        import core_engine  # noqa: PLC0415
        from config_manager import SpinSenseConfig

        engine_audio = core_engine.DEFAULT_CONFIG["Audio"]
        schema_audio = SpinSenseConfig().dict()["Audio"]
        # Every key, not a hand-kept list: a list is a thing to forget, and the
        # failure it lets through is silent — two processes reading the same
        # file and disagreeing about what an absent setting means.
        self.assertEqual(set(engine_audio), set(schema_audio))
        for key in schema_audio:
            self.assertEqual(engine_audio[key], schema_audio[key], key)

    def test_roundtrip_preserves_overrides(self):
        from config_manager import SpinSenseConfig
        data = SpinSenseConfig().dict()
        data["Audio"]["Track_End_Detection"] = False
        data["Audio"]["Track_End_Grace_Secs"] = 45.0
        out = SpinSenseConfig(**data).dict()
        self.assertFalse(out["Audio"]["Track_End_Detection"])
        self.assertEqual(out["Audio"]["Track_End_Grace_Secs"], 45.0)


class TestDiscoveryConfig(unittest.TestCase):
    def test_defaults_include_discovery(self):
        from config_manager import SpinSenseConfig
        cfg = SpinSenseConfig().dict()
        self.assertEqual(cfg["Discovery"]["mDNS"]["Enabled"], True)
        self.assertEqual(cfg["Discovery"]["mDNS"]["Service_Name"], "")

    def test_roundtrip_preserves_discovery(self):
        from config_manager import SpinSenseConfig
        data = SpinSenseConfig().dict()
        data["Discovery"]["mDNS"]["Enabled"] = False
        data["Discovery"]["mDNS"]["Service_Name"] = "Living Room"
        out = SpinSenseConfig(**data).dict()
        self.assertEqual(out["Discovery"]["mDNS"]["Enabled"], False)
        self.assertEqual(out["Discovery"]["mDNS"]["Service_Name"], "Living Room")


if __name__ == "__main__":
    unittest.main()
