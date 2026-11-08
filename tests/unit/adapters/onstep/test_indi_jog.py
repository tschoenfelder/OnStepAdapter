import unittest
from unittest.mock import patch

from onstep_adapter.indi_axis_motion import IndiAxisMover, RaAxisUndefinedError
from onstep_adapter.indi_jog import (
    JOG_LEASE_EXPIRED, JOG_LIMIT, JOG_MAX_DURATION, JOG_RELEASED,
    IndiAxisJogger, JogLease, validate_jog_request,
)
from onstep_adapter.indi_transport import IndiProperty

from .test_indi_axis_motion import snapshot


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class JogTransport:
    def __init__(self, *, issue_error=None):
        self.commands = []
        self.issue_error = issue_error

    def set_switch(self, device, name, element):
        self.commands.append((name, element))

    def issue_switch(self, device, name, element, *, timeout=3.0):
        self.commands.append((name, element))
        if self.issue_error and name.startswith("TELESCOPE_MOTION"):
            raise RuntimeError(self.issue_error)

    def issue_switch_off(self, device, name, element, *, timeout=3.0):
        self.commands.append((name, element, "Off"))

    def get_property(self, device, name):
        if name == "TELESCOPE_SLEW_RATE":
            return IndiProperty(
                device, name, "Switch", "Ok", "rw",
                {str(i): "Off" for i in range(10)}, 0.0, 1,
            )
        return None


def moving(rev, dec=20.0, **changes):
    return snapshot(dec=dec, revision=rev, slewing=True, **changes)


def stationary(rev, dec=20.0, **changes):
    return snapshot(dec=dec, revision=rev, **changes)


class JogTests(unittest.TestCase):
    def make(self, samples, *, ra_defined=True, issue_error=None, **limits):
        transport = JogTransport(issue_error=issue_error)
        sequence = iter(samples)
        stops = []
        observe = lambda: next(sequence)  # noqa: E731
        mover = IndiAxisMover(
            transport, "LX200 OnStep", 50.336, 8.533, observe,
            lambda: stops.append("emergency_stop"), **limits,
        )
        mover._ra_defined = ra_defined
        jogger = IndiAxisJogger(transport, "LX200 OnStep", mover, observe)
        return jogger, mover, transport, stops

    @patch("onstep_adapter.indi_jog.time.sleep")
    def test_release_switches_motion_off_tracking_off_and_confirms_stop(self, sleep):
        clock = FakeClock()
        lease = JogLease(0.5, clock=clock)
        samples = [stationary(1), moving(2, dec=20.2), moving(3, dec=20.4),
                   stationary(4, dec=20.5), stationary(5, dec=20.5)]
        jogger, mover, transport, stops = self.make(samples)

        def tick(_seconds):
            clock.now += 0.1
            if len(sleep.call_args_list) == 3:
                lease.release()
        sleep.side_effect = tick
        result = jogger.run("dec", 1, lease, rate_index=5, clock=clock)

        self.assertEqual(result.ended_by, JOG_RELEASED)
        self.assertEqual(transport.commands, [
            ("TELESCOPE_SLEW_RATE", "5"),
            ("TELESCOPE_MOTION_NS", "MOTION_NORTH"),
            ("TELESCOPE_MOTION_NS", "MOTION_NORTH", "Off"),
            ("TELESCOPE_TRACK_STATE", "TRACK_OFF"),
        ])
        self.assertTrue(result.stop_confirmed)
        self.assertAlmostEqual(result.final_dec_deg, 20.5)
        self.assertEqual(stops, [])

    @patch("onstep_adapter.indi_jog.time.sleep")
    def test_a_missed_renewal_stops_the_jog(self, sleep):
        clock = FakeClock()
        lease = JogLease(0.3, clock=clock)
        sleep.side_effect = lambda _s: setattr(clock, "now", clock.now + 0.1)
        samples = [stationary(1)] + [moving(r) for r in range(2, 6)] + [
            stationary(10), stationary(11)]
        jogger, _, transport, stops = self.make(samples)
        result = jogger.run("ra", -1, lease, clock=clock)
        self.assertEqual(result.ended_by, JOG_LEASE_EXPIRED)
        self.assertIn(("TELESCOPE_MOTION_WE", "MOTION_EAST", "Off"), transport.commands)
        self.assertEqual(stops, [])

    @patch("onstep_adapter.indi_jog.time.sleep")
    def test_renewing_keeps_it_moving_until_max_duration(self, sleep):
        clock = FakeClock()
        lease = JogLease(0.3, clock=clock)

        def tick(_s):
            clock.now += 0.1
            lease.renew()
        sleep.side_effect = tick
        samples = [stationary(1)] + [moving(r) for r in range(2, 40)] + [
            stationary(50), stationary(51)]
        jogger, *_ = self.make(samples)
        result = jogger.run("dec", -1, lease, max_duration_s=2.0, clock=clock)
        self.assertEqual(result.ended_by, JOG_MAX_DURATION)
        self.assertGreaterEqual(result.moving_s, 2.0)

    @patch("onstep_adapter.indi_jog.time.sleep")
    def test_leaving_the_dec_bound_stops_the_jog(self, sleep):
        clock = FakeClock()
        lease = JogLease(2.0, clock=clock)
        sleep.side_effect = lambda _s: setattr(clock, "now", clock.now + 0.1)
        samples = [stationary(1, dec=79.8, ha=None), moving(2, dec=79.95, ha=None),
                   moving(3, dec=80.1, ha=None), stationary(4, dec=80.15, ha=None),
                   stationary(5, dec=80.15, ha=None)]
        jogger, *_ = self.make(samples)
        result = jogger.run("dec", 1, lease, clock=clock)
        self.assertEqual(result.ended_by, JOG_LIMIT)

    @patch("onstep_adapter.indi_jog.time.sleep")
    def test_from_home_a_dec_jog_toward_the_bound_runs_and_unlocks_ra(self, sleep):
        clock = FakeClock()
        lease = JogLease(0.3, clock=clock)
        sleep.side_effect = lambda _s: setattr(clock, "now", clock.now + 0.1)
        samples = [stationary(1, dec=90.0, ha=None, at_home=True),
                   moving(2, dec=89.8, ha=None), moving(3, dec=89.6, ha=None),
                   stationary(10, dec=89.5, ha=None), stationary(11, dec=89.5, ha=None)]
        jogger, mover, _, _ = self.make(samples, ra_defined=False)
        result = jogger.run("dec", -1, lease, clock=clock)
        self.assertEqual(result.ended_by, JOG_LEASE_EXPIRED)
        self.assertTrue(mover.ra_axis_defined)

    def test_an_ra_jog_before_dec_has_moved_is_refused_unsent(self):
        jogger, _, transport, stops = self.make([stationary(1)], ra_defined=False)
        with self.assertRaises(RaAxisUndefinedError):
            jogger.run("ra", 1, JogLease(0.5))
        self.assertEqual(transport.commands, [])
        self.assertEqual(stops, [])

    def test_an_ra_jog_at_home_is_refused_unsent(self):
        jogger, mover, transport, _ = self.make([stationary(1, at_home=True)])
        with self.assertRaises(RaAxisUndefinedError):
            jogger.run("ra", 1, JogLease(0.5))
        self.assertFalse(mover.ra_axis_defined)
        self.assertEqual(transport.commands, [])

    def test_a_lease_released_before_start_sends_nothing(self):
        lease = JogLease(0.5)
        lease.release()
        jogger, _, transport, _ = self.make([stationary(1)])
        result = jogger.run("dec", 1, lease)
        self.assertEqual(result.ended_by, JOG_RELEASED)
        self.assertEqual(transport.commands, [])

    def test_unsafe_state_is_refused_unsent(self):
        for before in (stationary(1, parked=True), stationary(1, tracking=True),
                       stationary(1, at_limit=True), stationary(1, status_live=False)):
            with self.subTest(before=before):
                jogger, _, transport, _ = self.make([before])
                with self.assertRaises(RuntimeError):
                    jogger.run("dec", 1, JogLease(0.5))
                self.assertEqual(transport.commands, [])

    @patch("onstep_adapter.indi_jog.time.sleep")
    def test_a_fault_while_moving_sends_the_emergency_stop(self, sleep):
        clock = FakeClock()
        sleep.side_effect = lambda _s: setattr(clock, "now", clock.now + 0.1)
        jogger, _, _, stops = self.make([stationary(1), moving(2, at_limit=True)])
        with self.assertRaises(RuntimeError):
            jogger.run("dec", 1, JogLease(2.0, clock=clock), clock=clock)
        self.assertEqual(stops, ["emergency_stop"])

    def test_a_rejected_motion_command_sends_the_emergency_stop(self):
        jogger, _, _, stops = self.make([stationary(1)], issue_error="rejected")
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            jogger.run("dec", 1, JogLease(0.5))
        self.assertEqual(stops, ["emergency_stop"])

    def test_invalid_requests_are_refused(self):
        bad = [
            dict(axis="x", direction=1), dict(axis="ra", direction=0),
            dict(axis="ra", direction=True), dict(axis="ra", direction=1, rate_index=-1),
            dict(axis="ra", direction=1, lease_s=0.1),
            dict(axis="ra", direction=1, lease_s=3.0),
            dict(axis="ra", direction=1, max_duration_s=0),
            dict(axis="ra", direction=1, max_duration_s=121),
        ]
        for request in bad:
            options = dict(rate_index=None, lease_s=0.5, max_duration_s=60, poll_s=0.1)
            options.update({k: v for k, v in request.items() if k not in {"axis", "direction"}})
            with self.subTest(request=request), self.assertRaises(ValueError):
                validate_jog_request(request["axis"], request["direction"], **options)

    def test_rate_index_beyond_the_controller_list_is_refused(self):
        jogger, _, transport, stops = self.make([stationary(1)])
        with self.assertRaisesRegex(ValueError, "slew rates"):
            jogger.run("dec", 1, JogLease(0.5), rate_index=10)
        self.assertNotIn("TELESCOPE_MOTION_NS", [c[0] for c in transport.commands])
        self.assertEqual(stops, [])


class JogLeaseTests(unittest.TestCase):
    def test_lease_expires_without_renewal_and_cannot_be_revived(self):
        clock = FakeClock()
        lease = JogLease(0.5, clock=clock)
        clock.now = 0.4
        self.assertTrue(lease.renew())
        clock.now = 0.8
        self.assertFalse(lease.expired())
        clock.now = 1.0
        self.assertTrue(lease.expired())
        self.assertFalse(lease.renew())

    def test_release_ends_it(self):
        lease = JogLease(0.5)
        lease.release()
        self.assertTrue(lease.expired() and lease.released)
        self.assertFalse(lease.renew())


if __name__ == "__main__":
    unittest.main()
