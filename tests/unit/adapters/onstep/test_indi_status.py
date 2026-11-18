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
            "HORIZONTAL_COORD": prop("HORIZONTAL_COORD", {
                "AZ": "359.0", "ALT": "45.0"
            }),
            "TELESCOPE_PARK": prop("TELESCOPE_PARK", {
                "PARK": "Off", "UNPARK": "On"
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

    def test_physical_park_confirmation_temporarily_trusts_current_revision(self):
        fake = FakeTransport()
        fake.properties["OnStep Status"] = prop("OnStep Status", {
            ":GU# return": "nNPET260", "Error": "None"
        })
        fake.properties["TELESCOPE_PARK"] = prop("TELESCOPE_PARK", {
            "PARK": "On", "UNPARK": "Off"
        })
        reader = IndiStatusReader(fake, config(), baseline_accepted=lambda: False)
        self.assertFalse(reader.read().status_live)

        revision = reader.trust_current_park_status()
        confirmed = reader.read()

        self.assertEqual(revision, 1)
        self.assertTrue(confirmed.status_live)
        self.assertTrue(confirmed.park_evidence_consistent)

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

    def test_horizontal_coordinates_are_derived_when_driver_does_not_publish_them(self):
        fake = FakeTransport()
        del fake.properties["HORIZONTAL_COORD"]
        reader = IndiStatusReader(
            fake, config(), baseline_accepted=lambda: False,
            clock=lambda: datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc),
        )
        reader.read()
        fake.properties["EQUATORIAL_EOD_COORD"] = prop(
            "EQUATORIAL_EOD_COORD", {"RA": "9.1841667", "DEC": "57.5352778"},
            revision=2,
        )

        result = reader.read()

        self.assertIsNotNone(result.altitude_deg)
        self.assertIsNotNone(result.azimuth_deg)
        self.assertTrue(result.horizontal_live)

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
        fake.properties["TELESCOPE_PARK"] = prop("TELESCOPE_PARK", {
            "PARK": "On", "UNPARK": "Off"
        }, revision=2)
        result = reader.read()
        self.assertTrue(result.parked)
        self.assertTrue(result.park_evidence_consistent)
        self.assertIn("mechanical_terminal_state", result.blockers)
        self.assertFalse(result.mechanical_safe)

    def test_idle_standard_park_state_reconciles_with_raw_park(self):
        fake = FakeTransport()
        fake.properties["OnStep Status"] = prop("OnStep Status", {
            ":GU# return": "nNPET260", "Error": "None"
        })
        fake.properties["TELESCOPE_PARK"] = prop(
            "TELESCOPE_PARK", {"PARK": "On", "UNPARK": "Off"},
            state="Idle",
        )
        reader = IndiStatusReader(fake, config(), baseline_accepted=lambda: False)

        result = reader.read()

        self.assertTrue(result.park_evidence_consistent)
        self.assertNotIn("park_state_conflict", result.blockers)

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

    def test_conflicting_standard_and_raw_park_evidence_is_explicit(self):
        fake = FakeTransport()
        reader = IndiStatusReader(fake, config(), baseline_accepted=lambda: True)
        reader.read()
        fake.properties["OnStep Status"] = prop("OnStep Status", {
            ":GU# return": "nNPET260", "Error": "None"
        }, revision=2)
        fake.properties["TELESCOPE_PARK"] = prop("TELESCOPE_PARK", {
            "PARK": "Off", "UNPARK": "On"
        }, revision=2)
        result = reader.read()
        self.assertFalse(result.park_evidence_consistent)
        self.assertIn("park_state_conflict", result.blockers)


if __name__ == "__main__":
    unittest.main()
