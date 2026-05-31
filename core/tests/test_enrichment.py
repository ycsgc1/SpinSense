import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

import unittest


class TestExtractEnrichment(unittest.TestCase):
    def test_pulls_isrc_genre_year_when_present(self):
        from core_engine import _extract_enrichment
        track = {
            "isrc": "USRC17607839",
            "genres": {"primary": "Rock"},
            "sections": [
                {"metadata": [{"title": "Released", "text": "1977"}]},
            ],
        }
        out = _extract_enrichment(track)
        self.assertEqual(out["isrc"], "USRC17607839")
        self.assertEqual(out["genre"], "Rock")
        self.assertEqual(out["release_year"], 1977)

    def test_missing_fields_are_none(self):
        from core_engine import _extract_enrichment
        out = _extract_enrichment({})
        self.assertIsNone(out["isrc"])
        self.assertIsNone(out["genre"])
        self.assertIsNone(out["release_year"])

    def test_non_numeric_year_is_none(self):
        from core_engine import _extract_enrichment
        track = {"sections": [{"metadata": [{"title": "Released", "text": "n/a"}]}]}
        self.assertIsNone(_extract_enrichment(track)["release_year"])


class TestMqttGating(unittest.TestCase):
    def test_connect_loop_returns_immediately_when_mqtt_disabled(self):
        """When MQTT_WANTED is False the connect loop must return without
        attempting a broker connection. Without the gate this coroutine would
        block forever retrying against a nonexistent broker, so a short timeout
        proves the gate works."""
        import asyncio
        import core_engine

        original = core_engine.MQTT_WANTED
        core_engine.MQTT_WANTED = False
        core_engine.MQTT_ENABLED = False
        try:
            asyncio.run(asyncio.wait_for(core_engine.connect_mqtt_loop(), timeout=2.0))
        finally:
            core_engine.MQTT_WANTED = original
        self.assertFalse(core_engine.MQTT_ENABLED)
