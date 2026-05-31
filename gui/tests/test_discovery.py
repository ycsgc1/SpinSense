import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import unittest


class TestServiceInfoBuilder(unittest.TestCase):
    def test_build_service_info_uses_type_and_port(self):
        import discovery
        info = discovery.build_service_info(port=3313, service_name="Living Room", version="1.0")
        self.assertEqual(info.type, "_spinsense._tcp.local.")
        self.assertTrue(info.name.endswith("._spinsense._tcp.local."))
        self.assertIn("Living Room", info.name)
        self.assertEqual(info.port, 3313)
        self.assertEqual(info.properties.get(b"version"), b"1.0")

    def test_build_service_info_defaults_name_from_hostname(self):
        import discovery
        info = discovery.build_service_info(port=3313, service_name="", version="1.0")
        self.assertTrue(len(info.name) > len("._spinsense._tcp.local."))


class TestEnabledFlag(unittest.TestCase):
    def test_is_enabled_reads_config(self):
        import discovery
        self.assertTrue(discovery.is_enabled({"Discovery": {"mDNS": {"Enabled": True}}}))
        self.assertFalse(discovery.is_enabled({"Discovery": {"mDNS": {"Enabled": False}}}))
        self.assertTrue(discovery.is_enabled({}))  # default on
