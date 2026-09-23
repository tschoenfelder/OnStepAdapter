import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from onstep_adapter.indi_config import load_indi_config
from onstep_adapter.indi_status import IndiStatusReader
from onstep_adapter.indi_transport import IndiProperty


def prop(name, values, revision=1, *, age=0, state="Ok"):
    return IndiProperty(
        "LX200 OnStep", name, "Text", state, "ro", values,
        time.monotonic() - age, revision,
    )


class FakeTransport:
    is_open = True

    def __init__(self):
        self.properties = {
            "CONNECTION": prop("CONNECTION", {"CONNECT": "On"}),
            "OnStep Status": prop("OnStep Status", {
                ":GU# return": "nNpET260", "Error": "None"
            }),
            "EQUATORIAL_EOD_COORD": prop("EQUATORIAL_EOD_COORD", {
                "RA": "9.1841667", "DEC": "57.5352778"
            }),
            "TELESCOPE_PIER_SIDE": prop("TELESCOPE_PIER_SIDE", {
                "PIER_EAST": "On", "PIER_WEST": "Off"
            }),
        }

    def get_property(self, device, name):
        return self.properties.get(name)


def config():
    path = Path(__file__).resolve().parents[4] / "config.indi.example.toml"
    return load_indi_config(path)


class IndiStatusTests(unittest.TestCase):
    def test_initial_cached_snapshot_cannot_authorize_motion(self):
        fake = FakeTransport()
        reader = IndiStatusReader(fake, config(), baseline_accepted=lambda: False)
        result = reader.read()
        self.assertFalse(result.status_live)
        self.assertFalse(result.coordinates_live)
        self.assertIsNone(result.ha_deg)
        self.assertTrue(result.motion_refused)
        self.assertFalse(result.mechanical_safe)
        self.assertIn("time_site_authority_unestablished", result.blockers)
        self.assertIn("home_authority_unestablished", result.blockers)

    def test_live_controller_updates_report_geometry_but_not_mechanical_authority(self):
        fake = FakeTransport()
        reader = IndiStatusReader(
            fake, config(), baseline_accepted=lambda: True,
            clock=lambda: datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc),
        )
        reader.read()
        fake.properties["OnStep Status"] = prop("OnStep Status", {
            ":GU# return": "NpET260", "Error": "None"
        }, revision=2)
        fake.properties["EQUATORIAL_EOD_COORD"] = prop("EQUATORIAL_EOD_COORD", {
            "RA": "9.1841667", "DEC": "57.5352778"
        }, revision=2)
        result = reader.read()
        self.assertTrue(result.status_live and result.coordinates_live)
        self.assertTrue(result.tracking)
        self.assertEqual(result.pier_side, "east")
        self.assertIsNotNone(result.ha_deg)
        self.assertFalse(result.mechanical_safe)
        self.assertTrue(result.motion_refused)
        self.assertIn("home_authority_unestablished", result.blockers)

    def test_confirmed_home_authority_is_reported_but_does_not_unlock_motion(self):
        fake = FakeTransport()
        reader = IndiStatusReader(
            fake, config(), baseline_accepted=lambda: True,
            home_authority=lambda: True,
        )
        reader.read()
        fake.properties["OnStep Status"] = prop("OnStep Status", {
            ":GU# return": "nNpHET260", "Error": "None"
        }, revision=2)
        fake.properties["EQUATORIAL_EOD_COORD"] = prop("EQUATORIAL_EOD_COORD", {
            "RA": "9.1841667", "DEC": "57.5352778"
        }, revision=2)
        result = reader.read()
        self.assertTrue(result.home_authority)
        self.assertNotIn("home_authority_unestablished", result.blockers)
        self.assertFalse(result.mechanical_safe)
        self.assertTrue(result.motion_refused)

    def test_stale_or_conflicting_inputs_remain_refused(self):
        fake = FakeTransport()
        reader = IndiStatusReader(fake, config(), baseline_accepted=lambda: True)
        reader.read()
        fake.properties["OnStep Status"] = prop("OnStep Status", {
            ":GU# return": "NpET260", "Error": "None"
        }, revision=2, age=30)
        fake.properties["EQUATORIAL_EOD_COORD"] = prop("EQUATORIAL_EOD_COORD", {
            "RA": "9.1841667", "DEC": "57.5352778"
        }, revision=2)
        fake.properties["TELESCOPE_PIER_SIDE"] = prop("TELESCOPE_PIER_SIDE", {
            "PIER_EAST": "Off", "PIER_WEST": "On"
        }, revision=2)
        result = reader.read()
        self.assertIn("onstep_status_not_fresh", result.blockers)
        self.assertIn("pier_side_conflict", result.blockers)
        self.assertTrue(result.motion_refused)

    def test_park_coordinates_are_diagnostic_only(self):
        fake = FakeTransport()
        fake.properties["OnStep Status"] = prop("OnStep Status", {
            ":GU# return": "nNPET260", "Error": "None"
        })
        reader = IndiStatusReader(fake, config(), baseline_accepted=lambda: True)
        reader.read()
        fake.properties["OnStep Status"] = prop("OnStep Status", {
            ":GU# return": "nNPET260", "Error": "None"
        }, revision=2)
        fake.properties["EQUATORIAL_EOD_COORD"] = prop("EQUATORIAL_EOD_COORD", {
            "RA": "9", "DEC": "60"
        }, revision=2)
        result = reader.read()
        self.assertTrue(result.parked)
        self.assertIn("mechanical_terminal_state", result.blockers)
        self.assertFalse(result.mechanical_safe)

    def test_invalid_coordinates_and_limit_flag_block_motion(self):
        fake = FakeTransport()
        reader = IndiStatusReader(fake, config(), baseline_accepted=lambda: True)
        reader.read()
        fake.properties["OnStep Status"] = prop("OnStep Status", {
            ":GU# return": "NplET260", "Error": "None"
        }, revision=2)
        fake.properties["EQUATORIAL_EOD_COORD"] = prop("EQUATORIAL_EOD_COORD", {
            "RA": "nan", "DEC": "57"
        }, revision=2)
        result = reader.read()
        self.assertTrue(result.at_limit)
        self.assertIsNone(result.ha_deg)
        self.assertIn("coordinates_invalid", result.blockers)
        self.assertIn("onstep_limit_or_park_fault", result.blockers)
        self.assertTrue(result.motion_refused)


if __name__ == "__main__":
    unittest.main()
