import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from onstep_adapter.indi_axis_motion import AxisMotionMode, IndiAxisMover
from onstep_adapter.indi_mount import IndiMount
from onstep_adapter.indi_status import IndiMountSnapshot


def snapshot(ha=-30.0, ra=10.0, dec=20.0, revision=1, **changes):
    base = IndiMountSnapshot(
        raw_status="nNpET260", motion_state="unparked", pier_side="east",
        ra_hours=ra, dec_deg=dec, ha_deg=ha,
        meridian_distance_deg=abs(ha) if ha is not None else None,
        tracking=False, slewing=False,
        parked=False, at_home=False, at_limit=False, status_age_ms=50.0,
        coordinates_age_ms=50.0, status_live=True, coordinates_live=True,
        time_site_authority=True, home_authority=True,
        mechanical_safe=False, motion_refused=True, blockers=(),
        status_revision=revision, coordinates_revision=revision,
    )
    return replace(base, **changes)


class FakeTransport:
    def __init__(self, *, issue_error=None):
        self.commands = []
        self.issue_error = issue_error

    def set_switch(self, device, name, element):
        self.commands.append((name, element))

    def issue_switch(self, device, name, element, *, timeout=3.0):
        self.commands.append((name, element))

    def issue_numbers(self, device, name, values):
        self.commands.append((name, values))
        if self.issue_error:
            raise RuntimeError(self.issue_error)


class IndiAxisMotionTests(unittest.TestCase):
    def make_mover(self, samples, *, issue_error=None):
        fake = FakeTransport(issue_error=issue_error)
        sequence = iter(samples)
        stops = []
        mover = IndiAxisMover(
            fake, "LX200 OnStep", 50.336, 8.533, lambda: next(sequence),
            lambda: stops.append("emergency_stop"),
        )
        return mover, fake, stops

    def test_sky_modes_are_explicitly_refused_without_commands(self):
        for mode in ("sidereal", "lunar", "solar"):
            with self.subTest(mode=mode):
                mover, fake, stops = self.make_mover([snapshot()])
                with self.assertRaisesRegex(ValueError, "terrestrial"):
                    mover.move("dec", 1.0, mode=mode)
                self.assertEqual(fake.commands, [])
                self.assertEqual(stops, [])

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_one_five_ten_degrees_both_axes(self, unused_sleep):
        for axis in ("ra", "dec"):
            for magnitude in (1.0, 5.0, 10.0):
                for sign in (-1, 1):
                    with self.subTest(axis=axis, offset=sign * magnitude):
                        target = sign * magnitude
                        def point(deg, rev):
                            return snapshot(
                                ra=10 - deg / 15 if axis == "ra" else 10,
                                dec=20 + deg if axis == "dec" else 20,
                                revision=rev,
                            )
                        samples = [
                            point(0, 1), point(target / 2, 2),
                            point(target, 3), point(target, 4),
                            point(target, 5), point(target, 6),
                        ]
                        mover, fake, stops = self.make_mover(samples)
                        result = mover.move(axis, target)
                        self.assertTrue(result.stop_confirmed)
                        self.assertAlmostEqual(result.measured_deg, target)
                        self.assertEqual(stops, [])
                        self.assertEqual(fake.commands[0], ("ON_COORD_SET", "SLEW"))
                        self.assertEqual(fake.commands[1][0], "EQUATORIAL_EOD_COORD")
                        self.assertAlmostEqual(fake.commands[1][1]["DEC"], 20 + target if axis == "dec" else 20)
                        self.assertEqual(fake.commands[2], ("TELESCOPE_TRACK_STATE", "TRACK_OFF"))

    def test_refuses_untrusted_or_out_of_corridor_without_command(self):
        for before in (
            snapshot(status_live=False), snapshot(parked=True),
            snapshot(tracking=True), snapshot(at_limit=True),
        ):
            with self.subTest(before=before):
                mover, fake, _ = self.make_mover([before])
                with self.assertRaises(RuntimeError):
                    mover.move("ra", 10.0)
                self.assertEqual(fake.commands, [])

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_wrong_direction_triggers_emergency_stop(self, unused_sleep):
        mover, fake, stops = self.make_mover([
            snapshot(revision=1), snapshot(ra=10.05, revision=2),
        ])
        with self.assertRaisesRegex(RuntimeError, "reversed"):
            mover.move("ra", 1.0)
        self.assertEqual(stops, ["emergency_stop"])
        self.assertEqual(fake.commands[0], ("ON_COORD_SET", "SLEW"))

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_small_transient_braking_overshoot_can_settle_on_target(self, unused_sleep):
        mover, fake, stops = self.make_mover([
            snapshot(dec=20.0, revision=1),
            snapshot(dec=18.814, revision=2, slewing=True),
            snapshot(dec=19.0, revision=3),
            snapshot(dec=19.0, revision=4),
            snapshot(dec=19.0, revision=5),
            snapshot(dec=19.0, revision=6),
        ])
        result = mover.move("dec", -1.0)
        self.assertAlmostEqual(result.measured_deg, -1.0)
        self.assertEqual(stops, [])

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_dec_move_uses_hour_angle_for_stationary_other_axis(self, unused_sleep):
        mover, fake, stops = self.make_mover([
            snapshot(ra=10.0, dec=20.0, ha=-30.0, revision=1),
            snapshot(ra=10.001, dec=22.5, ha=-30.0, revision=2, slewing=True),
            snapshot(ra=10.010, dec=25.0, ha=-30.0, revision=3),
            snapshot(ra=10.011, dec=25.0, ha=-30.0, revision=4),
            snapshot(ra=10.012, dec=25.0, ha=-30.0, revision=5),
            snapshot(ra=10.013, dec=25.0, ha=-30.0, revision=6),
        ])
        result = mover.move("dec", 5.0)
        self.assertAlmostEqual(result.measured_deg, 5.0)
        self.assertEqual(stops, [])

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_dec_arrival_ignores_transient_ra_ha_frame_change(self, unused_sleep):
        mover, fake, stops = self.make_mover([
            snapshot(ra=10.0, dec=20.0, ha=-30.0, revision=1),
            snapshot(
                ra=10.2, dec=21.0, ha=-33.0, revision=2, slewing=True
            ),
            snapshot(
                ra=10.0, dec=21.0, ha=-29.9, revision=3,
                coordinates_revision=2,
            ),
            snapshot(
                ra=10.0, dec=21.0, ha=-29.9, revision=4,
                coordinates_revision=2,
            ),
            snapshot(
                ra=10.0, dec=21.0, ha=-29.9, revision=5,
                coordinates_revision=2,
            ),
        ])
        result = mover.move("dec", 1.0)
        self.assertAlmostEqual(result.measured_deg, 1.0)
        self.assertEqual(stops, [])

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_arrival_does_not_require_duplicate_coordinate_publication(self, unused_sleep):
        stale_coordinates = ("coordinates_not_fresh",)
        mover, fake, stops = self.make_mover([
            snapshot(dec=20.0, revision=1),
            snapshot(dec=21.0, revision=2, slewing=True),
            snapshot(
                dec=21.0, revision=3, coordinates_revision=2,
                coordinates_live=False, blockers=stale_coordinates,
            ),
            snapshot(
                dec=21.0, revision=4, coordinates_revision=2,
                coordinates_live=False, blockers=stale_coordinates,
            ),
            snapshot(
                dec=21.0, revision=5, coordinates_revision=2,
                coordinates_live=False, blockers=stale_coordinates,
            ),
        ])
        result = mover.move("dec", 1.0)
        self.assertTrue(result.stop_confirmed)
        self.assertEqual(stops, [])

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_manual_move_is_home_neutral_and_does_not_require_clock_authority(
        self, unused_sleep
    ):
        blockers = (
            "time_site_authority_unestablished", "hour_angle_unavailable",
            "home_authority_unestablished", "mechanical_terminal_state",
        )
        samples = [
            snapshot(
                ha=None, revision=1, at_home=True, time_site_authority=False,
                home_authority=False, blockers=blockers,
            ),
            snapshot(ha=None, ra=10 - 1 / 30, revision=2, at_home=False,
                     time_site_authority=False, home_authority=False,
                     blockers=blockers[:-1]),
            snapshot(ha=None, ra=10 - 1 / 15, revision=3,
                     time_site_authority=False, home_authority=False,
                     blockers=blockers[:-1]),
            snapshot(ha=None, ra=10 - 1 / 15, revision=4,
                     time_site_authority=False, home_authority=False,
                     blockers=blockers[:-1]),
            snapshot(ha=None, ra=10 - 1 / 15, revision=5,
                     time_site_authority=False, home_authority=False,
                     blockers=blockers[:-1]),
            snapshot(ha=None, ra=10 - 1 / 15, revision=6,
                     time_site_authority=False, home_authority=False,
                     blockers=blockers[:-1]),
        ]
        mover, _, stops = self.make_mover(samples)
        result = mover.move("ra", 1.0)
        self.assertAlmostEqual(result.measured_deg, 1.0)
        self.assertEqual(result.requested_arcsec, 3600.0)
        self.assertEqual(stops, [])

    def test_compatibility_manual_api_converts_arcseconds_to_degrees(self):
        client = Mock()
        client.move_axis_deg.return_value = "result"
        mount = IndiMount(client)
        self.assertEqual(mount.move_ra(3600, mode="manual"), "result")
        client.move_axis_deg.assert_called_with(
            "ra", 1.0, timeout_s=30.0, mode=AxisMotionMode.TERRESTRIAL
        )
        self.assertEqual(mount.move_dec(-18000, mode="manual"), "result")
        client.move_axis_deg.assert_called_with(
            "dec", -5.0, timeout_s=30.0, mode=AxisMotionMode.TERRESTRIAL
        )
        with self.assertRaises(ValueError):
            mount.move_ra(10, mode="center")

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_target_rejection_triggers_emergency_stop(self, unused_sleep):
        mover, _, stops = self.make_mover([
            snapshot(revision=1),
        ], issue_error="target rejected")
        with self.assertRaisesRegex(RuntimeError, "target rejected"):
            mover.move("ra", 1.0)
        self.assertEqual(stops, ["emergency_stop"])

    def test_invalid_angles_never_command_motion(self):
        mover, fake, _ = self.make_mover([])
        for angle in (0, 29 / 3600, 10.1, float("nan")):
            with self.assertRaises(ValueError):
                mover.move("dec", angle)
        self.assertEqual(fake.commands, [])

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_issue_14_calibration_seed_sizes_are_observed(self, unused_sleep):
        for arcsec in (110, 159, 195, 283, 897, 1595):
            with self.subTest(arcsec=arcsec):
                degrees = arcsec / 3600.0
                samples = [
                    snapshot(revision=1),
                    snapshot(ra=10 - degrees / 30, revision=2),
                    snapshot(ra=10 - degrees / 15, revision=3),
                    snapshot(ra=10 - degrees / 15, revision=4),
                    snapshot(ra=10 - degrees / 15, revision=5),
                    snapshot(ra=10 - degrees / 15, revision=6),
                ]
                mover, _, _ = self.make_mover(samples)
                result = mover.move("ra", degrees)
                self.assertAlmostEqual(result.requested_arcsec, arcsec)
                self.assertAlmostEqual(result.measured_deg, degrees)


if __name__ == "__main__":
    unittest.main()
