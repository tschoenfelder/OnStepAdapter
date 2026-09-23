import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from onstep_adapter.indi_meridian import IndiMeridianState
from onstep_adapter.indi_status import IndiMountSnapshot
from onstep_adapter.indi_tracking import enable_tracking_via_indi


def snapshot(revision=1, **changes):
    base = IndiMountSnapshot(
        raw_status="nNpET260", motion_state="unparked", pier_side="east",
        ra_hours=10.0, dec_deg=20.0, ha_deg=-10.0,
        meridian_distance_deg=10.0, tracking=False, slewing=False,
        parked=False, at_home=False, at_limit=False, status_age_ms=10,
        coordinates_age_ms=10, status_live=True, coordinates_live=True,
        time_site_authority=True, home_authority=True, mechanical_safe=False,
        motion_refused=True, blockers=(), status_revision=revision,
        coordinates_revision=revision,
    )
    return replace(base, **changes)


def meridian(phase="pre_meridian_allowed", stop=False):
    return IndiMeridianState(
        phase=phase, ha_deg=-10.0, pier_side="east", flip_required=False,
        tracking_stop_required=stop, seconds_to_flip_boundary=100,
        seconds_to_hard_stop=200, latest_safe_flip_start_seconds=50,
        limit_warning=False, blockers=(),
    )


class TrackingTransport:
    def __init__(self, error=None):
        self.commands = []
        self.error = error

    def set_switch(self, device, name, element):
        self.commands.append((name, element))
        if self.error:
            raise RuntimeError(self.error)


class IndiTrackingTests(unittest.TestCase):
    @patch("onstep_adapter.indi_tracking.time.sleep")
    def test_track_on_requires_two_fresh_tracking_reports(self, unused_sleep):
        observations = iter([
            snapshot(1), snapshot(2, tracking=True), snapshot(3, tracking=True),
        ])
        transport = TrackingTransport()
        stop = Mock()
        result = enable_tracking_via_indi(
            transport, "LX200 OnStep", observe=lambda: next(observations),
            meridian_status=lambda: meridian(), emergency_stop=stop,
        )
        self.assertTrue(result.command_accepted)
        self.assertTrue(result.tracking_confirmed)
        self.assertEqual(result.consecutive_tracking_polls, 2)
        self.assertEqual(transport.commands, [("TELESCOPE_TRACK_STATE", "TRACK_ON")])
        stop.assert_not_called()

    def test_tracking_refuses_without_astronomical_authority(self):
        for current, state in (
            (snapshot(home_authority=False, blockers=("home_authority_unestablished",)), meridian()),
            (snapshot(time_site_authority=False, blockers=("time_site_authority_unestablished",)), meridian("unknown")),
            (snapshot(parked=True), meridian("mechanical_terminal")),
            (snapshot(at_home=True), meridian("mechanical_terminal")),
            (snapshot(at_limit=True), meridian("firmware_limit", True)),
            (snapshot(), meridian("flip_required")),
        ):
            with self.subTest(current=current, phase=state.phase):
                transport = TrackingTransport()
                result = enable_tracking_via_indi(
                    transport, "LX200 OnStep", observe=lambda: current,
                    meridian_status=lambda: state, emergency_stop=Mock(),
                )
                self.assertFalse(result.command_accepted)
                self.assertFalse(result.tracking_confirmed)
                self.assertEqual(transport.commands, [])

    @patch("onstep_adapter.indi_tracking.time.sleep")
    def test_unconfirmed_tracking_requests_emergency_stop(self, unused_sleep):
        observations = iter([
            snapshot(1), snapshot(2, tracking=True, at_limit=True),
        ])
        stop = Mock()
        result = enable_tracking_via_indi(
            TrackingTransport(), "LX200 OnStep",
            observe=lambda: next(observations), meridian_status=lambda: meridian(),
            emergency_stop=stop,
        )
        self.assertTrue(result.command_accepted)
        self.assertFalse(result.tracking_confirmed)
        stop.assert_called_once()

    def test_driver_rejection_is_reported_without_claiming_tracking(self):
        stop = Mock()
        result = enable_tracking_via_indi(
            TrackingTransport("rejected"), "LX200 OnStep",
            observe=lambda: snapshot(), meridian_status=lambda: meridian(),
            emergency_stop=stop,
        )
        self.assertFalse(result.command_accepted)
        self.assertFalse(result.tracking_confirmed)
        self.assertEqual(result.error, "rejected")
        stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
