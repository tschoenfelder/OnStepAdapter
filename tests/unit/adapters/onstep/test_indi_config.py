import tempfile
import unittest
from pathlib import Path

from onstep_adapter.indi_config import load_indi_config
from onstep_adapter.meridian_policy import derive_meridian_policy


class IndiConfigTests(unittest.TestCase):
    def test_example_file_is_valid_and_matches_live_guard_policy(self):
        path = Path(__file__).resolve().parents[4] / "config.indi.example.toml"
        config = load_indi_config(path)
        policy = derive_meridian_policy(
            east_guard_minutes=12,
            west_guard_minutes=8,
            flip_request_deg=config.flip_request_deg,
            hard_stop_deg=config.tracking_stop_deg,
            flip_allowance_seconds=config.flip_allowance_seconds,
            reserve_seconds=config.reserve_seconds,
        )
        self.assertEqual(policy.flip_request_deg, 1.0)
        self.assertEqual(policy.hard_stop_deg, 1.75)

    def test_configured_stop_above_firmware_guard_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "below both firmware"):
            derive_meridian_policy(
                east_guard_minutes=12, west_guard_minutes=8,
                flip_request_deg=1, hard_stop_deg=2.25,
            )

    def test_bad_site_and_reversed_boundaries_are_rejected(self):
        path = Path(__file__).resolve().parents[4] / "config.indi.example.toml"
        original = path.read_text(encoding="ascii")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config.toml"
            target.write_text(original.replace("lat = 50.336", "lat = 95.0"), encoding="ascii")
            with self.assertRaisesRegex(ValueError, "latitude"):
                load_indi_config(target)
            target.write_text(
                original.replace("tracking_stop_deg = 1.75", "tracking_stop_deg = 0.5"),
                encoding="ascii",
            )
            with self.assertRaisesRegex(ValueError, "flip must precede"):
                load_indi_config(target)


if __name__ == "__main__":
    unittest.main()
