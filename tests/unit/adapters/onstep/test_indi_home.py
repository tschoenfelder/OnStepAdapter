import time
import unittest
from types import SimpleNamespace

from onstep_adapter.indi_home import IndiHomeRouter
from onstep_adapter.indi_transport import IndiProperty


def status(raw, revision, *, error="None"):
    return IndiProperty(
        "LX200 OnStep", "OnStep Status", "Text", "Ok", "ro",
        {":GU# return": raw, "Error": error}, time.monotonic(), revision,
    )


class FakeReader:
    def __init__(self, *, live=True, blockers=(), parked=True, at_home=False, tracking=False):
        self.live = live
        self.blockers = blockers
        self.parked = parked
        self.at_home = at_home
        self.tracking = tracking

    def read(self):
        return SimpleNamespace(
            status_live=self.live, parked=self.parked, at_home=self.at_home,
            tracking=self.tracking, at_limit=False, slewing=False,
            raw_status="nNpHET260" if self.at_home else "nNPET260" if self.parked else "nNpET260",
            blockers=self.blockers,
        )


class FakeTransport:
    def __init__(self, updates, *, initial="nNPET260"):
        self.current = status(initial, 1)
        self.updates = list(updates)
        self.commands = []

    def get_property(self, device, name):
        return self.current

    def wait_property(self, device, name, *, timeout, after_revision=-1):
        if name != "OnStep Status":
            return IndiProperty(
                device, name, "Switch", "Ok", "rw", {"GO": "Off"},
                time.monotonic(), 1,
            )
        if not self.updates:
            raise TimeoutError("no fresh status")
        self.current = self.updates.pop(0)
        if self.current.revision <= after_revision:
            raise AssertionError("test provided a stale revision")
        return self.current

    def issue_switch(self, device, name, element, *, timeout=3.0):
        self.commands.append((name, element))
        return 1

    def set_switch(self, device, name, element, *, timeout=3.0):
        self.commands.append((name, element))

    def request_switch(self, device, name, element, *, timeout=3.0):
        self.commands.append((name, element))


class IndiHomeRouteTests(unittest.TestCase):
    def test_go_home_only_when_explicitly_requested(self):
        transport = FakeTransport([
            status("pET260", 2),
            status("nNpHET260", 3),
            status("nNpHET260", 4),
        ], initial="nNpET260")
        router = IndiHomeRouter(
            transport, FakeReader(parked=False), "LX200 OnStep"
        )

        result = router.go_home()

        self.assertTrue(result.confirmed)
        self.assertEqual(result.destination, "home")
        self.assertTrue(router.authority_established)
        self.assertEqual(transport.commands, [("TELESCOPE_HOME", "GO")])

    def test_park_requires_home_then_confirms_two_park_reports(self):
        transport = FakeTransport([
            status("pET260", 2),
            status("nNPET260", 3),
            status("nNPET260", 4),
        ], initial="nNpHET260")
        router = IndiHomeRouter(
            transport, FakeReader(parked=False, at_home=True), "LX200 OnStep"
        )
        router.authority_established = True

        result = router.park()

        self.assertTrue(result.confirmed)
        self.assertEqual(result.destination, "park")
        self.assertFalse(router.authority_established)
        self.assertEqual(transport.commands, [("TELESCOPE_PARK", "PARK")])

    def test_park_away_from_home_refuses_without_motion(self):
        transport = FakeTransport([], initial="nNpET260")
        router = IndiHomeRouter(
            transport, FakeReader(parked=False), "LX200 OnStep"
        )
        result = router.park()
        self.assertFalse(result.confirmed)
        self.assertEqual(transport.commands, [])

    def test_failed_park_requests_stop_but_not_home(self):
        transport = FakeTransport([
            status("nNpHET260", 2, error="Motor fault"),
            status("nNpHET260", 3),
            status("nNpHET260", 4),
        ], initial="nNpHET260")
        router = IndiHomeRouter(
            transport, FakeReader(parked=False, at_home=True), "LX200 OnStep"
        )
        result = router.park()
        self.assertFalse(result.confirmed)
        self.assertIsNotNone(result.stop_result)
        self.assertNotIn(("TELESCOPE_HOME", "GO"), transport.commands)

    def test_go_home_requires_tracking_off(self):
        transport = FakeTransport([], initial="NpET260")
        router = IndiHomeRouter(
            transport, FakeReader(parked=False, tracking=True), "LX200 OnStep"
        )
        result = router.go_home()
        self.assertFalse(result.confirmed)
        self.assertEqual(transport.commands, [])

    def test_existing_home_flag_alone_does_not_establish_authority(self):
        transport = FakeTransport([], initial="nNpHET260")
        router = IndiHomeRouter(
            transport, FakeReader(parked=False, at_home=True), "LX200 OnStep"
        )
        result = router.go_home(timeout=0.01)
        self.assertFalse(result.confirmed)
        self.assertFalse(router.authority_established)
        self.assertEqual(transport.commands[0], ("TELESCOPE_HOME", "GO"))

    def test_unpark_never_requests_home_motion_or_grants_home_authority(self):
        transport = FakeTransport([
            status("NpET260", 2),
            status("nNpET260", 3),
        ])
        router = IndiHomeRouter(transport, FakeReader(), "LX200 OnStep")

        result = router.unpark()

        self.assertTrue(result.unparked_confirmed)
        self.assertFalse(router.authority_established)
        self.assertEqual(result.final_raw_status, "nNpET260")
        self.assertEqual(transport.commands, [
            ("TELESCOPE_PARK", "UNPARK"),
            ("TELESCOPE_TRACK_STATE", "TRACK_OFF"),
        ])

    def test_park_to_home_needs_two_fresh_stationary_home_updates(self):
        transport = FakeTransport([
            status("NpET260", 2),
            status("nNpET260", 3),
            status("nNpET260", 4),
            status("nNpHET260", 5),
            status("nNpHET260", 6),
        ])
        router = IndiHomeRouter(transport, FakeReader(), "LX200 OnStep")

        result = router.park_to_home()

        self.assertTrue(result.authority_established)
        self.assertEqual(result.stage, "home_confirmed")
        self.assertEqual(result.final_raw_status, "nNpHET260")
        self.assertTrue(router.authority_established)
        self.assertEqual(transport.commands, [
            ("TELESCOPE_PARK", "UNPARK"),
            ("TELESCOPE_TRACK_STATE", "TRACK_OFF"),
            ("TELESCOPE_HOME", "GO"),
        ])

    def test_stale_or_disconnected_park_status_refuses_without_commands(self):
        for reader in (FakeReader(live=False), FakeReader(blockers=("indi_device_disconnected",))):
            with self.subTest(reader=reader):
                transport = FakeTransport([])
                result = IndiHomeRouter(transport, reader, "LX200 OnStep").park_to_home()
                self.assertFalse(result.authority_established)
                self.assertEqual(result.stage, "preflight")
                self.assertEqual(transport.commands, [])

    def test_fault_during_home_revokes_authority_and_requests_stop(self):
        transport = FakeTransport([
            status("nNpET260", 2),
            status("nNpET260", 3),
            status("nNpET260", 4),
            status("nNpHlET260", 5),
            status("nNpHET260", 6),
            status("nNpHET260", 7),
        ])
        router = IndiHomeRouter(transport, FakeReader(), "LX200 OnStep")

        result = router.park_to_home()

        self.assertFalse(result.authority_established)
        self.assertFalse(router.authority_established)
        self.assertEqual(result.stage, "home")
        self.assertIsNotNone(result.stop_result)
        self.assertTrue(result.stop_result.stopped_confirmed)
        self.assertEqual(transport.commands[-2:], [
            ("TELESCOPE_ABORT_MOTION", "ABORT"),
            ("TELESCOPE_TRACK_STATE", "TRACK_OFF"),
        ])
        self.assertNotIn(("TELESCOPE_PARK", "PARK"), transport.commands)

    def test_no_home_confirmation_requests_stop_without_claiming_authority(self):
        transport = FakeTransport([
            status("nNpET260", 2),
            status("nNpET260", 3),
            status("nNpET260", 4),
        ])
        result = IndiHomeRouter(transport, FakeReader(), "LX200 OnStep").park_to_home(
            home_timeout=0.01
        )
        self.assertFalse(result.authority_established)
        self.assertEqual(result.stage, "home")
        self.assertIn(("TELESCOPE_ABORT_MOTION", "ABORT"), transport.commands)


if __name__ == "__main__":
    unittest.main()
