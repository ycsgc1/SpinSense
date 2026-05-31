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
        cfg["MQTT"]["Broker"]["Host"] = "broker.example.com"
        cfg["MQTT"]["Broker"]["Port"] = 8883
        cfg["Hardware"]["Mic_Device"] = "Scarlett Solo USB"
        self.assertTrue(config_manager.save_config(cfg))

        # Force a fresh read from disk.
        loaded = config_manager.load_config()
        self.assertAlmostEqual(loaded["Audio"]["Volume_Threshold"], 0.0123)
        self.assertEqual(loaded["MQTT"]["Broker"]["Host"], "broker.example.com")
        self.assertEqual(loaded["MQTT"]["Broker"]["Port"], 8883)
        self.assertEqual(loaded["Hardware"]["Mic_Device"], "Scarlett Solo USB")

    def test_invalid_port_type_rejected(self):
        cfg = config_manager.get_default_config()
        cfg["MQTT"]["Broker"]["Port"] = "not-a-port"
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


if __name__ == "__main__":
    unittest.main()
