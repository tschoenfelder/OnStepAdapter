import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from onstep_adapter.indi_axis_motion import (
    RA_UNDEFINED_AT_HOME, AxisMotionMode, IndiAxisMover, RaAxisUndefinedError,
)
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
    def make_mover(
        self, samples, *, issue_error=None, ra_defined=True, **movement_limits
    ):
        """``ra_defined``: start as if a DEC move already defined RA (most tests
        are not about the DEC-first rule; ``DecFirstTests`` covers it)."""
        fake = FakeTransport(issue_error=issue_error)
        sequence = iter(samples)
        stops = []
        mover = IndiAxisMover(
            fake, "LX200 OnStep", 50.336, 8.533, lambda: next(sequence),
            lambda: stops.append("emergency_stop"),
            **movement_limits,
        )
        if ra_defined:
            mover._ra_defined = True
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
    def test_configured_dec_bounds_are_inclusive_and_enforced(self, unused_sleep):
        samples = [
            snapshot(ha=None, dec=79.0, revision=1),
            snapshot(ha=None, dec=79.5, revision=2),
            snapshot(ha=None, dec=80.0, revision=3),
            snapshot(ha=None, dec=80.0, revision=4),
            snapshot(ha=None, dec=80.0, revision=5),
            snapshot(ha=None, dec=80.0, revision=6),
        ]
        mover, _, stops = self.make_mover(
            samples, min_dec_deg=-80.0, max_dec_deg=80.0
        )
        result = mover.move("dec", 1.0)
        self.assertEqual(result.final_dec_deg, 80.0)
        self.assertEqual(stops, [])

        mover, fake, _ = self.make_mover(
            [snapshot(ha=None, dec=79.5)], min_dec_deg=-80.0, max_dec_deg=80.0
        )
        with self.assertRaisesRegex(RuntimeError, "Projected DEC"):
            mover.move("dec", 1.0)
        self.assertEqual(fake.commands, [])

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_dec_move_from_outside_the_bound_may_only_approach_it(self, unused_sleep):
        # HOME is DEC +90, outside the default +80 bound: a DEC move toward
        # the bound runs, a DEC move away from it and any RA move do not.
        samples = [
            snapshot(ha=None, dec=90.0, revision=1, at_home=True),
            snapshot(ha=None, dec=89.5, revision=2),
            snapshot(ha=None, dec=89.0, revision=3),
            snapshot(ha=None, dec=89.0, revision=4),
            snapshot(ha=None, dec=89.0, revision=5),
        ]
        mover, _, stops = self.make_mover(samples, ra_defined=False)
        result = mover.move("dec", -1.0)
        self.assertAlmostEqual(result.final_dec_deg, 89.0)
        self.assertEqual(stops, [])

        for axis, offset in (("dec", 0.5), ("ra", 1.0)):
            with self.subTest(axis=axis, offset=offset):
                mover, fake, _ = self.make_mover([snapshot(ha=None, dec=85.0)])
                with self.assertRaisesRegex(RuntimeError, "Projected DEC"):
                    mover.move(axis, offset)
                self.assertEqual(fake.commands, [])

        mover, fake, _ = self.make_mover([snapshot(ha=None, dec=-85.0)])
        with self.assertRaisesRegex(RuntimeError, "Projected DEC"):
            mover.move("dec", -1.0)
        self.assertEqual(fake.commands, [])

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_projected_altitude_allows_minus_two_degrees_inclusively(
        self, unused_sleep
    ):
        target_dec = 50.336 - 92.0
        samples = [
            snapshot(ha=0.0, dec=target_dec + 1.0, revision=1),
            snapshot(ha=0.0, dec=target_dec + 0.5, revision=2),
            snapshot(ha=0.0, dec=target_dec, revision=3),
            snapshot(ha=0.0, dec=target_dec, revision=4),
            snapshot(ha=0.0, dec=target_dec, revision=5),
            snapshot(ha=0.0, dec=target_dec, revision=6),
        ]
        mover, _, stops = self.make_mover(samples, min_alt_deg=-2.0)
        result = mover.move("dec", -1.0)
        self.assertAlmostEqual(result.final_dec_deg, target_dec)
        self.assertEqual(stops, [])

    def test_projected_altitude_below_configured_floor_is_refused(self):
        mover, fake, _ = self.make_mover([
            snapshot(ha=0.0, dec=50.336 - 92.0),
        ], min_alt_deg=-2.0)
        with self.assertRaisesRegex(RuntimeError, "Projected altitude"):
            mover.move("dec", -1.0)
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
        common = dict(
            ha=None, time_site_authority=False, home_authority=False,
            blockers=blockers[:-1],
        )
        samples = [
            snapshot(
                ha=None, revision=1, at_home=True, time_site_authority=False,
                home_authority=False, blockers=blockers,
            ),
            snapshot(dec=19.5, revision=2, **common),
            snapshot(dec=19.0, revision=3, **common),
            snapshot(dec=19.0, revision=4, **common),
            snapshot(dec=19.0, revision=5, **common),
            snapshot(dec=19.0, revision=6, **common),
        ]
        mover, _, stops = self.make_mover(samples)
        result = mover.move("dec", -1.0)
        self.assertAlmostEqual(result.measured_deg, -1.0)
        self.assertEqual(result.requested_arcsec, -3600.0)
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


def ra_samples(start_rev, *, ra=10.0, degrees=1.0, **changes):
    """The five readings one completed RA-axis move of ``degrees`` takes."""
    end = ra - degrees / 15
    points = [ra, ra - degrees / 30, end, end, end]
    return [
        snapshot(ra=value, revision=start_rev + index, **changes)
        for index, value in enumerate(points)
    ]


def dec_samples(start_rev, *, dec=20.0, degrees=1.0, end_changes=None, **changes):
    """The five readings one completed DEC-axis move of ``degrees`` takes."""
    end = dec + degrees
    points = [dec, dec + degrees / 2, end, end, end]
    samples = []
    for index, value in enumerate(points):
        extra = dict(changes)
        if index and end_changes:
            extra.update(end_changes)
        samples.append(snapshot(dec=value, revision=start_rev + index, **extra))
    return samples


class DecFirstTests(unittest.TestCase):
    """At HOME the mount points at the pole: RA has a value but no meaning
    until DEC has moved, so the first move after connecting or HOME is DEC."""

    def make_mover(self, samples):
        fake = FakeTransport()
        sequence = iter(samples)
        stops = []
        mover = IndiAxisMover(
            fake, "LX200 OnStep", 50.336, 8.533, lambda: next(sequence),
            lambda: stops.append("emergency_stop"),
        )
        return mover, fake, stops

    def test_ra_move_right_after_connecting_is_refused_unsent(self):
        mover, fake, stops = self.make_mover([snapshot()])
        self.assertFalse(mover.ra_axis_defined)
        with self.assertRaises(RaAxisUndefinedError) as raised:
            mover.move("ra", 1.0)
        self.assertEqual(str(raised.exception), RA_UNDEFINED_AT_HOME)
        self.assertIsInstance(raised.exception, RuntimeError)
        self.assertEqual(fake.commands, [])
        self.assertEqual(stops, [])

    def test_require_ra_defined_refuses_before_any_reading(self):
        mover, _, _ = self.make_mover([])
        with self.assertRaises(RaAxisUndefinedError):
            mover.require_ra_defined("ra")
        mover.require_ra_defined("dec")  # DEC is always allowed

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_completed_dec_move_unlocks_ra(self, unused_sleep):
        mover, fake, stops = self.make_mover(dec_samples(1) + ra_samples(10))
        mover.move("dec", 1.0)
        self.assertTrue(mover.ra_axis_defined)
        result = mover.move("ra", 1.0)
        self.assertAlmostEqual(result.measured_deg, 1.0)
        self.assertEqual(stops, [])

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_dec_move_away_from_home_unlocks_ra(self, unused_sleep):
        samples = dec_samples(
            1, degrees=-1.0, at_home=True, end_changes={"at_home": False}
        )
        mover, _, _ = self.make_mover(samples + ra_samples(10))
        mover.move("dec", -1.0)
        self.assertTrue(mover.ra_axis_defined)
        mover.move("ra", 1.0)

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_reaching_home_again_requires_dec_first_again(self, unused_sleep):
        samples = dec_samples(1) + [snapshot(revision=10, at_home=True)]
        mover, fake, _ = self.make_mover(samples)
        mover.move("dec", 1.0)
        sent = len(fake.commands)
        with self.assertRaises(RaAxisUndefinedError):
            mover.move("ra", 1.0)
        self.assertFalse(mover.ra_axis_defined)
        self.assertEqual(len(fake.commands), sent)

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_mark_ra_undefined_requires_dec_first_again(self, unused_sleep):
        mover, fake, _ = self.make_mover(dec_samples(1) + [snapshot(revision=10)])
        mover.move("dec", 1.0)
        mover.mark_ra_undefined()
        sent = len(fake.commands)
        with self.assertRaises(RaAxisUndefinedError):
            mover.move("ra", 1.0)
        self.assertEqual(len(fake.commands), sent)

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_failed_dec_move_leaves_ra_undefined(self, unused_sleep):
        mover, _, stops = self.make_mover([
            snapshot(dec=20.0, revision=1), snapshot(dec=19.5, revision=2),
        ])
        with self.assertRaisesRegex(RuntimeError, "reversed"):
            mover.move("dec", 1.0)
        self.assertEqual(stops, ["emergency_stop"])
        self.assertFalse(mover.ra_axis_defined)

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_ra_moves_keep_ra_defined(self, unused_sleep):
        mover, _, _ = self.make_mover(
            dec_samples(1) + ra_samples(10) + ra_samples(20, ra=10 - 1 / 15)
        )
        mover.move("dec", 1.0)
        mover.move("ra", 1.0)
        mover.move("ra", 1.0)
        self.assertTrue(mover.ra_axis_defined)

    def test_mount_facade_reports_the_client_state(self):
        client = Mock()
        client.ra_axis_defined = False
        self.assertFalse(IndiMount(client).ra_axis_defined)
        client.ra_axis_defined = True
        self.assertTrue(IndiMount(client).ra_axis_defined)


if __name__ == "__main__":
    unittest.main()
