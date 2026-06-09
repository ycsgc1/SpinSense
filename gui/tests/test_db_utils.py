"""Python mirror of gui/static/db_utils.js, used to assert the conversion
behavior we depend on across the wizard, Settings, and Dashboard. The JS
implementation is not directly executable here; we mirror its math and
test the mirror. Any change to db_utils.js must be paralleled here."""
import math
import unittest


def rms_to_db(rms: float) -> float:
    if rms <= 0:
        return -120.0
    return max(-120.0, 20.0 * math.log10(rms))


def db_to_rms(db: float) -> float:
    return 10.0 ** (db / 20.0)


def format_db(db: float) -> str:
    return f"{db:.1f} dB"


class DbUtilsTest(unittest.TestCase):
    def test_zero_clamps_to_floor(self):
        self.assertEqual(rms_to_db(0.0), -120.0)
        self.assertEqual(rms_to_db(-0.5), -120.0)

    def test_very_small_clamps_to_floor(self):
        # 10^(-120/20) = 1e-6 — anything quieter than this floors out
        self.assertEqual(rms_to_db(1e-12), -120.0)

    def test_quiet_music_below_old_floor_is_representable(self):
        # -100 dB (rms 1e-5) used to clamp to -80; now it round-trips.
        self.assertAlmostEqual(rms_to_db(1e-5), -100.0, places=6)

    def test_unity_is_zero_db(self):
        self.assertAlmostEqual(rms_to_db(1.0), 0.0, places=6)

    def test_known_conversions(self):
        # 0.0002 ≈ -73.98 dB (the user's working threshold)
        self.assertAlmostEqual(rms_to_db(0.0002), -73.9794, places=3)
        # 0.01 = -40 dB exactly
        self.assertAlmostEqual(rms_to_db(0.01), -40.0, places=6)
        # 0.1 = -20 dB exactly
        self.assertAlmostEqual(rms_to_db(0.1), -20.0, places=6)

    def test_round_trip(self):
        for db in (-80.0, -60.0, -40.0, -20.0, -1.0, 0.0):
            self.assertAlmostEqual(rms_to_db(db_to_rms(db)), db, places=6)

    def test_format_db(self):
        self.assertEqual(format_db(-61.5), "-61.5 dB")
        self.assertEqual(format_db(0.0), "0.0 dB")


if __name__ == "__main__":
    unittest.main()
