import time
import unittest
from dataclasses import replace
from unittest.mock import Mock

from onstep_adapter.indi_guiding import IndiGuideController
from onstep_adapter.indi_meridian import IndiMeridianState
from onstep_adapter.indi_mount import IndiMount
from onstep_adapter.indi_status import IndiMountSnapshot
from onstep_adapter.indi_transport import IndiProperty


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def snapshot(**changes):
    base = IndiMountSnapshot(
        raw_status="NpET260", motion_state="tracking", pier_side="east",
        ra_hours=10.0, dec_deg=20.0, ha_deg=-10.0,
        meridian_distance_deg=10.0, tracking=True, slewing=False,
        parked=False, at_home=False, at_limit=False, status_age_ms=10.0,
        coordinates_age_ms=10.0, status_live=True, coordinates_live=True,
        time_site_authority=True, home_authority=True, mechanical_safe=False,
        motion_refused=True, blockers=(), status_revision=2,
        coordinates_revision=2,
    )
    return replace(base, **changes)


def meridian(phase="pre_meridian_allowed", *, stop=False, flip=False):
    return IndiMeridianState(
        phase=phase, ha_deg=-10.0, pier_side="east", flip_required=flip,
        tracking_stop_required=stop, seconds_to_flip_boundary=100.0,
        seconds_to_hard_stop=200.0, latest_safe_flip_start_seconds=50.0,
        limit_warning=flip or stop, blockers=(),
    )


def guide_property(name, revision, *, state="Ok"):
    elements = (
        {"TIMED_GUIDE_N": "0", "TIMED_GUIDE_S": "0"}
        if name.endswith("NS")
        else {"TIMED_GUIDE_E": "0", "TIMED_GUIDE_W": "0"}
    )
    return IndiProperty(
        "LX200 OnStep", name, "Number", state, "rw", elements,
        time.monotonic(), revision,
    )


class GuideTransport:
    def __init__(self, *, completion_state="Ok"):
        self.is_open = True
        self.commands = []
        self.completion_state = completion_state
        self.properties = {
            "TELESCOPE_TIMED_GUIDE_NS": guide_property(
                "TELESCOPE_TIMED_GUIDE_NS", 1
            ),
            "TELESCOPE_TIMED_GUIDE_WE": guide_property(
                "TELESCOPE_TIMED_GUIDE_WE", 1
            ),
        }

    def get_property(self, device, name):
        return self.properties.get(name)

    def issue_number(self, device, name, element, value, *, timeout):
        self.commands.append((name, element, value))
        return self.properties[name].revision

    def wait_property(self, device, name, *, timeout, after_revision):
        current = self.properties[name]
        updated = guide_property(
            name, max(current.revision, after_revision) + 1,
            state=self.completion_state,
        )
        self.properties[name] = updated
        return updated


class IndiGuidingTests(unittest.TestCase):
    def controller(
        self, transport=None, *, current=None, state=None,
        authority_policy="strict", stop=None,
    ):
        clock = FakeClock()
        current_snapshot = current or snapshot()
        revision = current_snapshot.status_revision

        def observe():
            nonlocal revision
            revision += 1
            return replace(current_snapshot, status_revision=revision)

        return IndiGuideController(
            transport or GuideTransport(), "LX200 OnStep",
            observe=observe,
            meridian_status=lambda: state or meridian(),
            emergency_stop=stop or Mock(), authority_policy=authority_policy,
            monotonic=clock.monotonic, sleeper=clock.sleep,
        )

    def test_all_directions_map_to_standard_indi_guide_properties(self):
        expected = {
            "north": ("TELESCOPE_TIMED_GUIDE_NS", "TIMED_GUIDE_N"),
            "south": ("TELESCOPE_TIMED_GUIDE_NS", "TIMED_GUIDE_S"),
            "east": ("TELESCOPE_TIMED_GUIDE_WE", "TIMED_GUIDE_E"),
            "west": ("TELESCOPE_TIMED_GUIDE_WE", "TIMED_GUIDE_W"),
        }
        for direction, mapped in expected.items():
            with self.subTest(direction=direction):
                transport = GuideTransport()
                result = self.controller(transport).pulse(direction, 100)
                self.assertTrue(result.pulse_completed)
                self.assertTrue(result.tracking_preserved)
                self.assertEqual(transport.commands, [(*mapped, 100)])

    def test_short_aliases_and_mount_compatibility_wrapper(self):
        client = Mock()
        client.guide_pulse.return_value.pulse_completed = True
        mount = IndiMount(client)
        self.assertTrue(mount.guide("e", 100))
        client.guide_pulse.assert_called_once_with(
            "e", 100, command_timeout=3.0
        )

    def test_tracking_off_and_unsafe_states_issue_no_pulse(self):
        cases = (
            (snapshot(tracking=False), meridian()),
            (snapshot(parked=True), meridian("mechanical_terminal")),
            (snapshot(slewing=True), meridian()),
            (snapshot(at_limit=True), meridian("firmware_limit", stop=True)),
            (snapshot(), meridian("hard_stop", stop=True, flip=True)),
        )
        for current, state in cases:
            with self.subTest(current=current, phase=state.phase):
                transport = GuideTransport()
                result = self.controller(
                    transport, current=current, state=state
                ).pulse("east", 100)
                self.assertFalse(result.command_accepted)
                self.assertFalse(result.pulse_completed)
                self.assertEqual(transport.commands, [])

    def test_strict_refuses_missing_authority_but_controller_managed_warns(self):
        current = snapshot(
            coordinates_live=False, time_site_authority=False,
            home_authority=False, ha_deg=None,
            blockers=(
                "coordinates_not_fresh", "time_site_authority_unestablished",
                "home_authority_unestablished", "hour_angle_unavailable",
            ),
        )
        strict_transport = GuideTransport()
        strict = self.controller(
            strict_transport, current=current, state=meridian("unknown")
        ).pulse("north", 100)
        self.assertFalse(strict.command_accepted)
        self.assertEqual(strict_transport.commands, [])

        delegated_transport = GuideTransport()
        delegated = self.controller(
            delegated_transport, current=current, state=meridian("unknown"),
            authority_policy="controller_managed",
        ).pulse("north", 100)
        self.assertTrue(delegated.pulse_completed)
        self.assertIn("time_site_authority_unestablished", delegated.warnings)
        self.assertIn("home_authority_unestablished", delegated.warnings)

    def test_flip_recommendation_warns_but_does_not_block_active_tracking(self):
        result = self.controller(
            state=meridian("flip_required", flip=True)
        ).pulse("west", 100)
        self.assertTrue(result.pulse_completed)
        self.assertIn("meridian_flip_required", result.warnings)

    def test_long_request_is_split_into_bounded_chunks(self):
        transport = GuideTransport()
        result = self.controller(transport).pulse("south", 1200)
        self.assertTrue(result.pulse_completed)
        self.assertEqual(result.chunks_requested, 3)
        self.assertEqual(result.chunks_completed, 3)
        self.assertEqual([command[2] for command in transport.commands], [500, 500, 200])

    def test_chunking_never_emits_a_below_minimum_remainder(self):
        transport = GuideTransport()
        result = self.controller(transport).pulse("south", 510)
        self.assertTrue(result.pulse_completed)
        self.assertEqual([command[2] for command in transport.commands], [490, 20])

    def test_indi_ok_does_not_finish_until_onstep_clears_guide_flag(self):
        clock = FakeClock()
        observations = iter([
            snapshot(raw_status="NpEW260", guiding=False, status_revision=2),
            snapshot(raw_status="NpGEW260", guiding=True, status_revision=3),
            snapshot(raw_status="NpEW260", guiding=False, status_revision=4),
            snapshot(raw_status="NpEW260", guiding=False, status_revision=5),
        ])
        controller = IndiGuideController(
            GuideTransport(), "LX200 OnStep", observe=lambda: next(observations),
            meridian_status=lambda: meridian(), emergency_stop=Mock(),
            monotonic=clock.monotonic, sleeper=clock.sleep,
        )

        result = controller.pulse("east", 100)

        self.assertTrue(result.pulse_completed)
        self.assertEqual(result.final_raw_status, "NpEW260")
        self.assertGreaterEqual(clock.now, 0.15)

    def test_invalid_direction_or_duration_never_issues_a_pulse(self):
        transport = GuideTransport()
        controller = self.controller(transport)
        for direction, duration in (("up", 100), ("north", 19), ("north", 5001)):
            with self.subTest(direction=direction, duration=duration):
                with self.assertRaises(ValueError):
                    controller.pulse(direction, duration)
        self.assertEqual(transport.commands, [])

    def test_overlapping_pulse_is_refused_without_a_second_command(self):
        transport = GuideTransport()
        controller = self.controller(transport)
        controller._lock.acquire()
        try:
            result = controller.pulse("east", 100)
        finally:
            controller._lock.release()

        self.assertFalse(result.command_accepted)
        self.assertFalse(result.pulse_completed)
        self.assertEqual(result.error, "another guide pulse is active")
        self.assertEqual(transport.commands, [])

    def test_driver_alert_after_issue_requests_emergency_stop(self):
        transport = GuideTransport(completion_state="Alert")
        stop = Mock()
        result = self.controller(transport, stop=stop).pulse("east", 100)
        self.assertTrue(result.command_accepted)
        self.assertFalse(result.pulse_completed)
        self.assertIn("rejected guide pulse", result.error)
        stop.assert_called_once()

    def test_tracking_loss_after_pulse_requests_emergency_stop(self):
        observations = iter([snapshot(), snapshot(tracking=False)])
        transport = GuideTransport()
        stop = Mock()
        controller = IndiGuideController(
            transport, "LX200 OnStep", observe=lambda: next(observations),
            meridian_status=lambda: meridian(), emergency_stop=stop,
            monotonic=FakeClock().monotonic,
            sleeper=lambda _seconds: None,
        )
        result = controller.pulse("east", 100)
        self.assertFalse(result.pulse_completed)
        self.assertFalse(result.tracking_preserved)
        stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
