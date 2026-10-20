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
        alt, az = mover._horizontal_from_equatorial(before.ha_deg, before.dec_deg)
        if axis == "dec":
            alt, az = mover._fold_altitude_path(alt + offset, az)
        else:
            az = (az + offset) % 360.0
        lst = (before.ra_hours + before.ha_deg / 15.0) % 24.0
        return mover._equatorial_from_horizontal(alt, az, lst)

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

    def test_continuous_altitude_folds_at_zenith(self):
        self.assertEqual(IndiAxisMover._fold_altitude_path(105.0, 0.0), (75.0, 180.0))
        self.assertEqual(IndiAxisMover._fold_altitude_path(120.0, 0.0), (60.0, 180.0))
        self.assertEqual(IndiAxisMover._fold_altitude_path(135.0, 0.0), (45.0, 180.0))

    @patch("onstep_adapter.indi_axis_motion.time.sleep")
    def test_one_five_fifteen_degree_horizontal_jogs(self, unused_sleep):
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
    def test_dec_jog_uses_altaz_translation_not_ra_plus_twelve(self, unused_sleep):
        before = snapshot(ha=0.0, ra=8.0, dec=50.0)
        seed, _, _ = self.make_mover([])
        mover, fake, _ = self.make_mover(
            self.completed_samples(seed, before, "dec", 15.0)
        )
        mover.move("dec", 15.0)
        target = fake.commands[1][1]
        ra_jump = abs(((target["RA"] - before.ra_hours + 12.0) % 24.0) - 12.0)
        self.assertLess(ra_jump, 6.0)

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

    def test_untrusted_state_never_commands_motion(self):
        for before in (
            snapshot(status_live=False), snapshot(parked=True),
            snapshot(tracking=True), snapshot(at_limit=True), snapshot(ha=None),
        ):
            mover, fake, _ = self.make_mover([before])
            with self.assertRaises(RuntimeError):
                mover.move("ra", 1.0)
            self.assertEqual(fake.commands, [])

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
