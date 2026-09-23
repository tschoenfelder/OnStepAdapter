import unittest
from dataclasses import replace

from onstep_adapter.indi_meridian import IndiMeridianSupervisor, classify_meridian
from onstep_adapter.indi_status import IndiMountSnapshot
from onstep_adapter.meridian_policy import derive_meridian_policy


POLICY = derive_meridian_policy(east_guard_minutes=12, west_guard_minutes=8)


def snapshot(ha, *, side="east", authority=True, tracking=True, parked=False, at_limit=False):
    return IndiMountSnapshot(
        raw_status="NpET260", motion_state="tracking", pier_side=side,
        ra_hours=10.0, dec_deg=20.0, ha_deg=ha,
        meridian_distance_deg=abs(ha), tracking=tracking, slewing=False,
        parked=parked, at_home=False, at_limit=at_limit,
        status_age_ms=100, coordinates_age_ms=100, status_live=True,
        coordinates_live=True, time_site_authority=authority,
        home_authority=False, mechanical_safe=False, motion_refused=True,
        blockers=(),
    )


class IndiMeridianTests(unittest.TestCase):
    def test_boundaries_are_inclusive_only_on_preflip_pier(self):
        self.assertEqual(classify_meridian(snapshot(-0.1), POLICY).phase, "pre_meridian_allowed")
        self.assertEqual(classify_meridian(snapshot(0), POLICY).phase, "post_meridian_allowed")
        self.assertEqual(classify_meridian(snapshot(1), POLICY).phase, "flip_required")
        at_stop = classify_meridian(snapshot(1.75), POLICY)
        self.assertEqual(at_stop.phase, "hard_stop")
        self.assertTrue(at_stop.tracking_stop_required)
        self.assertEqual(classify_meridian(snapshot(1.75, side="west"), POLICY).phase, "post_flip")

    def test_unsynced_client_does_not_stop_existing_tracking(self):
        stops = []
        supervisor = IndiMeridianSupervisor(
            observe=lambda: snapshot(2.0, authority=False), policy=POLICY,
            stop=lambda: stops.append("stop"),
        )
        self.assertEqual(supervisor.tick().phase, "unknown")
        self.assertEqual(stops, [])

    def test_fresh_firmware_limit_stops_even_without_time_baseline(self):
        stops = []
        supervisor = IndiMeridianSupervisor(
            observe=lambda: snapshot(0, authority=False, at_limit=True), policy=POLICY,
            stop=lambda: stops.append("stop"),
        )
        self.assertEqual(supervisor.tick().phase, "firmware_limit")
        self.assertEqual(stops, ["stop"])

    def test_parked_is_excluded_and_conflicting_pier_is_unknown(self):
        parked = classify_meridian(snapshot(2, parked=True), POLICY)
        self.assertFalse(parked.tracking_stop_required)
        self.assertEqual(parked.phase, "mechanical_terminal")
        conflicting = replace(snapshot(2), blockers=("pier_side_conflict",))
        self.assertEqual(classify_meridian(conflicting, POLICY).phase, "unknown")

    def test_supervisor_emits_transitions_not_each_poll(self):
        current = [snapshot(0)]
        transitions = []
        supervisor = IndiMeridianSupervisor(
            observe=lambda: current[0], policy=POLICY, stop=lambda: None,
            on_transition=lambda state: transitions.append(state.phase),
        )
        supervisor.tick()
        supervisor.tick()
        current[0] = snapshot(1.0)
        supervisor.tick()
        self.assertEqual(transitions, ["post_meridian_allowed", "flip_required"])


if __name__ == "__main__":
    unittest.main()
