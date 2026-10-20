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
        tracking=False, slewing=False, parked=False, at_home=False,
        at_limit=False, status_age_ms=50.0, coordinates_age_ms=50.0,
        status_live=True, coordinates_live=True, time_site_authority=True,
        home_authority=True, mechanical_safe=False, motion_refused=True,
        blockers=(), status_revision=revision,
        coordinates_revision=revision,
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
    def make_mover(self, samples, *, issue_error=None, **movement_limits):
        fake = FakeTransport(issue_error=issue_error)
        sequence = iter(samples)
        stops = []
        mover = IndiAxisMover(
            fake, "LX200 OnStep", 50.336, 8.533, lambda: next(sequence),
            lambda: stops.append("emergency_stop"), **movement_limits,
        )
        return mover, fake, stops

    def target_for(self, mover, before, axis, offset):
        ra, dec, _ = mover._equatorial_axis_target(
            before.ra_hours, before.dec_deg, before.ha_deg, axis, offset
        )
        return ra, dec

    def completed_samples(self, mover, before, axis, offset):
        target_ra, target_dec = self.target_for(mover, before, axis, offset)
        lst = (before.ra_hours + before.ha_deg / 15.0) % 24.0
        target_ha = ((lst - target_ra + 12) % 24 - 12) * 15
        return [
            before,
            snapshot(ha=target_ha, ra=target_ra, dec=target_dec, revision=2, slewing=True),
            snapshot(ha=target_ha, ra=target_ra, dec=target_dec, revision=3),
            snapshot(ha=target_ha, ra=target_ra, dec=target_dec, revision=4),
            snapshot(ha=target_ha, ra=target_ra, dec=target_dec, revision=5),
        ]

    def test_altaz_equatorial_round_trip(self):
        mover, _, _ = self.make_mover([])
        for ha in (-170.0, -30.0, 0.0, 75.0, 170.0):
            for dec in (-70.0, 0.0, 48.0, 80.0):
                alt, az = mover._horizontal_from_equatorial(ha, dec)
                ra, result_dec = mover._equatorial_from_horizontal(alt, az, 8.0)
                expected_ra = (8.0 - ha / 15.0) % 24.0
                self.assertLess(abs(((ra - expected_ra + 12) % 24) - 12), 1e-10)
                self.assertAlmostEqual(result_dec, dec, places=9)

    def test_equatorial_to_altaz_matches_independent_cardinal_references(self):
        mover, _, _ = self.make_mover([])
        latitude = 50.336
        references = (
            # HA, DEC, expected ALT, expected AZ.  These are analytic
            # horizon/meridian identities, not values produced by the inverse.
            (0.0, latitude, 90.0, None),
            (0.0, latitude - 90.0, 0.0, 180.0),
            (180.0, 90.0 - latitude, 0.0, 0.0),
            (-90.0, 0.0, 0.0, 90.0),
            (90.0, 0.0, 0.0, 270.0),
        )
        for ha, dec, expected_alt, expected_az in references:
            with self.subTest(ha=ha, dec=dec):
                alt, az = mover._horizontal_from_equatorial(ha, dec)
                self.assertAlmostEqual(alt, expected_alt, delta=2e-6)
                if expected_az is not None:  # Azimuth is undefined at zenith.
                    az_error = (az - expected_az + 180.0) % 360.0 - 180.0
                    self.assertAlmostEqual(az_error, 0.0, delta=2e-6)

    def test_altaz_to_equatorial_matches_independent_cardinal_references(self):
        mover, _, _ = self.make_mover([])
        latitude = 50.336
        lst = 8.0
        references = (
            # ALT, AZ, expected RA, expected DEC.
            (90.0, 0.0, lst, latitude),
            (0.0, 180.0, lst, latitude - 90.0),
            (0.0, 0.0, (lst - 12.0) % 24.0, 90.0 - latitude),
            (0.0, 90.0, (lst + 6.0) % 24.0, 0.0),
            (0.0, 270.0, (lst - 6.0) % 24.0, 0.0),
        )
        for alt, az, expected_ra, expected_dec in references:
            with self.subTest(alt=alt, az=az):
                ra, dec = mover._equatorial_from_horizontal(alt, az, lst)
                self.assertLess(
                    abs(((ra - expected_ra + 12.0) % 24.0) - 12.0), 1e-10
                )
                self.assertAlmostEqual(dec, expected_dec, places=9)

    def test_continuous_altitude_folds_at_zenith(self):
        self.assertEqual(IndiAxisMover._fold_altitude_path(105.0, 0.0), (75.0, 180.0))
        self.assertEqual(IndiAxisMover._fold_altitude_path(120.0, 0.0), (60.0, 180.0))
        self.assertEqual(IndiAxisMover._fold_altitude_path(135.0, 0.0), (45.0, 180.0))

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_one_five_fifteen_degree_equatorial_axis_jogs(self, unused_sleep):
        for axis in ("ra", "dec"):
            for offset in (-15.0, -5.0, -1.0, 1.0, 5.0, 15.0):
                with self.subTest(axis=axis, offset=offset):
                    before = snapshot()
                    seed, _, _ = self.make_mover([])
                    samples = self.completed_samples(seed, before, axis, offset)
                    mover, fake, stops = self.make_mover(samples)
                    result = mover.move(axis, offset)
                    target_ra, target_dec = self.target_for(mover, before, axis, offset)
                    self.assertAlmostEqual(result.measured_deg, offset)
                    self.assertEqual(fake.commands[0], ("ON_COORD_SET", "SLEW"))
                    self.assertAlmostEqual(fake.commands[1][1]["RA"], target_ra)
                    self.assertAlmostEqual(fake.commands[1][1]["DEC"], target_dec)
                    self.assertEqual(fake.commands[2], ("TELESCOPE_TRACK_STATE", "TRACK_OFF"))
                    self.assertEqual(stops, [])

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_dec_jog_preserves_ra_and_changes_only_dec(self, unused_sleep):
        before = snapshot(ha=0.0, ra=8.0, dec=50.0)
        seed, _, _ = self.make_mover([])
        mover, fake, _ = self.make_mover(
            self.completed_samples(seed, before, "dec", 15.0)
        )
        mover.move("dec", 15.0)
        target = fake.commands[1][1]
        self.assertEqual(target["RA"], before.ra_hours)
        self.assertEqual(target["DEC"], before.dec_deg - 15.0)

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_ra_jog_preserves_dec_and_changes_hour_angle_by_requested_angle(
        self, unused_sleep,
    ):
        before = snapshot(ha=-1.0, ra=22.0, dec=48.0)
        seed, _, _ = self.make_mover([])
        mover, fake, _ = self.make_mover(
            self.completed_samples(seed, before, "ra", 15.0)
        )
        mover.move("ra", 15.0)
        target = fake.commands[1][1]
        self.assertEqual(target["DEC"], before.dec_deg)
        self.assertEqual(target["RA"], 21.0)

    def test_axis_target_invariants_over_dense_coordinate_grid(self):
        mover, _, _ = self.make_mover([])
        for ra in (0.0, 0.01, 1.0, 11.99, 12.0, 22.861111111111, 23.99):
            for dec in (-79.0, -50.0, 0.0, 48.933055555556, 79.0):
                for ha in (-179.0, -90.0, -1.211534815052, 0.0, 90.0, 179.0):
                    for offset in (-15.0, -5.0, -1.0, 1.0, 5.0, 15.0):
                        with self.subTest(ra=ra, dec=dec, ha=ha, offset=offset):
                            target_ra, target_dec, target_ha = (
                                mover._equatorial_axis_target(
                                    ra, dec, ha, "dec", offset
                                )
                            )
                            self.assertEqual(target_ra, ra)
                            self.assertEqual(target_dec, dec - offset)
                            self.assertEqual(target_ha, ha)

                            target_ra, target_dec, target_ha = (
                                mover._equatorial_axis_target(
                                    ra, dec, ha, "ra", offset
                                )
                            )
                            self.assertAlmostEqual(
                                target_ra, (ra - offset / 15.0) % 24.0,
                                places=12,
                            )
                            self.assertEqual(target_dec, dec)
                            self.assertEqual(target_ha, ha + offset)

    def test_ra_axis_target_wraps_at_zero_and_twenty_four_hours(self):
        mover, _, _ = self.make_mover([])
        positive = mover._equatorial_axis_target(0.25, 20.0, 0.0, "ra", 15.0)
        negative = mover._equatorial_axis_target(23.75, 20.0, 0.0, "ra", -15.0)
        self.assertEqual(positive, (23.25, 20.0, 15.0))
        self.assertEqual(negative, (0.75, 20.0, -15.0))

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_repeated_positive_dec_jogs_move_away_from_upper_bound(
        self, unused_sleep,
    ):
        ra = 22.861111111111
        dec = 48.933055555556
        for expected_dec in (33.933055555556, 18.933055555556, 3.933055555556):
            before = snapshot(ha=0.0, ra=ra, dec=dec)
            seed, _, _ = self.make_mover([])
            mover, fake, _ = self.make_mover(
                self.completed_samples(seed, before, "dec", 15.0)
            )
            mover.move("dec", 15.0)
            target = fake.commands[1][1]
            self.assertEqual(target["RA"], ra)
            self.assertAlmostEqual(target["DEC"], expected_dec, places=10)
            self.assertLess(target["DEC"], dec)
            self.assertGreater(80.0 - target["DEC"], 80.0 - dec)
            dec = target["DEC"]

    def test_repeated_negative_dec_jog_stops_only_when_target_crosses_upper_bound(self):
        mover, _, _ = self.make_mover([])
        ra = 22.861111111111
        dec = 48.933055555556
        first = mover._equatorial_axis_target(ra, dec, 0.0, "dec", -15.0)
        second = mover._equatorial_axis_target(ra, first[1], 0.0, "dec", -15.0)
        third = mover._equatorial_axis_target(ra, second[1], 0.0, "dec", -15.0)
        self.assertAlmostEqual(first[1], 63.933055555556, places=10)
        self.assertAlmostEqual(second[1], 78.933055555556, places=10)
        self.assertAlmostEqual(third[1], 93.933055555556, places=10)
        self.assertLessEqual(second[1], mover.max_dec_deg)
        self.assertGreater(third[1], mover.max_dec_deg)

        refusing, fake, stops = self.make_mover([snapshot(
            ha=0.0, ra=ra, dec=second[1],
        )])
        with self.assertRaisesRegex(RuntimeError, "DEC leaves"):
            refusing.move("dec", -15.0)
        self.assertEqual(fake.commands, [])
        self.assertEqual(stops, [])

    def test_sky_modes_are_refused_without_commands(self):
        for mode in ("sidereal", "lunar", "solar"):
            mover, fake, _ = self.make_mover([snapshot()])
            with self.assertRaisesRegex(ValueError, "terrestrial"):
                mover.move("dec", 1.0, mode=mode)
            self.assertEqual(fake.commands, [])

    def test_invalid_angles_never_command_motion(self):
        mover, fake, _ = self.make_mover([])
        for angle in (0, 29 / 3600, 15.1, float("nan")):
            with self.assertRaises(ValueError):
                mover.move("dec", angle)
        self.assertEqual(fake.commands, [])

    def test_dec_limit_rejection_happens_before_any_indi_write(self):
        for dec, offset in ((79.0, -5.0), (-79.0, 5.0)):
            with self.subTest(dec=dec, offset=offset):
                mover, fake, stops = self.make_mover([snapshot(dec=dec)])
                with self.assertRaisesRegex(RuntimeError, "DEC leaves"):
                    mover.move("dec", offset)
                self.assertEqual(fake.commands, [])
                self.assertEqual(stops, [])

    def test_altitude_limit_rejection_happens_before_any_indi_write(self):
        mover, fake, stops = self.make_mover([
            snapshot(ha=180.0, ra=0.0, dec=0.0),
        ])
        with self.assertRaisesRegex(RuntimeError, "altitude leaves"):
            mover.move("ra", 1.0)
        self.assertEqual(fake.commands, [])
        self.assertEqual(stops, [])

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_near_zenith_dec_move_does_not_cross_meridian_or_change_ra(
        self, unused_sleep,
    ):
        before = snapshot(ha=-1.211534815052, ra=22.861111111111,
                          dec=48.933055555556)
        seed, _, _ = self.make_mover([])
        mover, fake, _ = self.make_mover(
            self.completed_samples(seed, before, "dec", 15.0)
        )

        mover.move("dec", 15.0)

        target = fake.commands[1][1]
        self.assertEqual(target["RA"], before.ra_hours)
        self.assertAlmostEqual(target["DEC"], 33.933055555556, places=10)
        lst = (before.ra_hours + before.ha_deg / 15.0) % 24.0
        target_ha = ((lst - target["RA"] + 12.0) % 24.0 - 12.0) * 15.0
        self.assertAlmostEqual(target_ha, before.ha_deg, places=10)

    def test_untrusted_state_never_commands_motion(self):
        for before in (
            snapshot(status_live=False), snapshot(parked=True),
            snapshot(tracking=True), snapshot(at_limit=True),
        ):
            mover, fake, _ = self.make_mover([before])
            with self.assertRaises(RuntimeError):
                mover.move("ra", 1.0)
            self.assertEqual(fake.commands, [])

    @patch("onstep_adapter.indi_axis_motion.local_sidereal_hours", return_value=8.0)
    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_missing_indi_hour_angle_is_derived_from_utc_and_longitude(
        self, unused_sleep, sidereal_time,
    ):
        before = snapshot(ha=None, ra=10.0, dec=20.0)
        derived = replace(before, ha_deg=-30.0, meridian_distance_deg=30.0)
        seed, _, _ = self.make_mover([])
        samples = self.completed_samples(seed, derived, "dec", 1.0)
        samples[0] = before
        mover, fake, stops = self.make_mover(samples)

        result = mover.move("dec", 1.0)

        expected_ra, expected_dec = self.target_for(mover, derived, "dec", 1.0)
        self.assertEqual(result.measured_deg, 1.0)
        self.assertEqual(fake.commands[0], ("ON_COORD_SET", "SLEW"))
        self.assertEqual(fake.commands[1][0], "EQUATORIAL_EOD_COORD")
        self.assertAlmostEqual(fake.commands[1][1]["RA"], expected_ra, places=10)
        self.assertAlmostEqual(fake.commands[1][1]["DEC"], expected_dec, places=10)
        self.assertNotAlmostEqual(fake.commands[1][1]["RA"], before.ra_hours + 12.0)
        self.assertEqual(stops, [])
        sidereal_time.assert_called_once()

    @patch(
        "onstep_adapter.indi_axis_motion.local_sidereal_hours",
        return_value=22.780342123440857,
    )
    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_first_dec_plus_15_from_recorded_zenith_snapshot_issues_expected_indi_target(
        self, unused_sleep, unused_sidereal_time,
    ):
        """Regression for the first failed Raspberry move on 2026-10-20."""
        before = snapshot(
            ha=None,
            ra=22.861111111111,
            dec=48.933055555556,
            revision=100,
        )
        expected_ra = 22.861111111111
        expected_dec = 33.933055555556
        target_ha = (
            (22.780342123440857 - expected_ra + 12.0) % 24.0 - 12.0
        ) * 15.0
        samples = [
            before,
            snapshot(ha=target_ha, ra=expected_ra, dec=expected_dec,
                     revision=101, slewing=True),
            snapshot(ha=target_ha, ra=expected_ra, dec=expected_dec, revision=102),
            snapshot(ha=target_ha, ra=expected_ra, dec=expected_dec, revision=103),
            snapshot(ha=target_ha, ra=expected_ra, dec=expected_dec, revision=104),
        ]
        mover, fake, stops = self.make_mover(samples)

        result = mover.move("dec", 15.0)

        self.assertEqual(fake.commands[0], ("ON_COORD_SET", "SLEW"))
        self.assertEqual(fake.commands[1][0], "EQUATORIAL_EOD_COORD")
        self.assertAlmostEqual(fake.commands[1][1]["RA"], expected_ra, places=10)
        self.assertAlmostEqual(fake.commands[1][1]["DEC"], expected_dec, places=10)
        self.assertEqual(fake.commands[2], ("TELESCOPE_TRACK_STATE", "TRACK_OFF"))
        self.assertEqual(result.measured_deg, 15.0)
        self.assertEqual(stops, [])

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_target_rejection_triggers_emergency_stop(self, unused_sleep):
        mover, _, stops = self.make_mover([snapshot()], issue_error="target rejected")
        with self.assertRaisesRegex(RuntimeError, "target rejected"):
            mover.move("ra", 1.0)
        self.assertEqual(stops, ["emergency_stop"])

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


if __name__ == "__main__":
    unittest.main()
