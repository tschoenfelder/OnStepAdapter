import time
import unittest

from onstep_adapter.indi_stop import stop_mount_via_indi
from onstep_adapter.indi_client import OnStepIndiClient
from onstep_adapter.indi_transport import IndiProperty


def prop(name, values, revision=1, state="Ok"):
    return IndiProperty(
        "LX200 OnStep", name, "Text", state, "ro", values,
        time.monotonic(), revision,
    )


class FakeTransport:
    def __init__(self, statuses, *, abort_error=None, tracking_error=None):
        self.is_open = True
        self.commands = []
        self.statuses = list(statuses)
        self.abort_error = abort_error
        self.tracking_error = tracking_error

    def request_switch(self, device, name, element, *, timeout):
        self.commands.append((name, element))
        if self.abort_error:
            raise RuntimeError(self.abort_error)
        return prop(name, {element: "Off"})

    def set_switch(self, device, name, element, *, timeout):
        self.commands.append((name, element))
        if self.tracking_error:
            raise RuntimeError(self.tracking_error)
        return prop(name, {element: "On"})

    def issue_switch(self, device, name, element, *, timeout):
        self.commands.append((name, element))
        if name == "TELESCOPE_ABORT_MOTION" and self.abort_error:
            raise RuntimeError(self.abort_error)
        if name == "TELESCOPE_TRACK_STATE" and self.tracking_error:
            raise RuntimeError(self.tracking_error)
        return 1

    def get_property(self, device, name):
        return prop(name, {":GU# return": "NpET260"})

    def wait_property(self, device, name, *, timeout, after_revision):
        if not self.statuses:
            raise TimeoutError("no fresh status")
        return self.statuses.pop(0)


class IndiStopTests(unittest.TestCase):
    def test_requires_two_fresh_nontracking_updates(self):
        fake = FakeTransport([
            prop("OnStep Status", {":GU# return": "NpET260"}, 2),
            prop("OnStep Status", {":GU# return": "nNpET260"}, 3),
            prop("OnStep Status", {":GU# return": "nNpET260"}, 4),
        ])
        result = stop_mount_via_indi(fake, "LX200 OnStep")
        self.assertTrue(result.stopped_confirmed)
        self.assertEqual(result.consecutive_stopped_polls, 2)
        self.assertEqual(fake.commands, [
            ("TELESCOPE_ABORT_MOTION", "ABORT"),
            ("TELESCOPE_TRACK_STATE", "TRACK_OFF"),
        ])

    def test_abort_rejection_still_attempts_tracking_off_but_not_proven(self):
        fake = FakeTransport([
            prop("OnStep Status", {":GU# return": "nNpET260"}, 2),
            prop("OnStep Status", {":GU# return": "nNpET260"}, 3),
        ], abort_error="abort rejected")
        result = stop_mount_via_indi(fake, "LX200 OnStep")
        self.assertFalse(result.stopped_confirmed)
        self.assertTrue(result.tracking_off_accepted)
        self.assertEqual(len(fake.commands), 2)
        self.assertIn("abort rejected", result.errors[0])

    def test_no_fresh_second_poll_is_not_confirmed(self):
        fake = FakeTransport([
            prop("OnStep Status", {":GU# return": "nNpET260"}, 2),
        ])
        result = stop_mount_via_indi(fake, "LX200 OnStep")
        self.assertFalse(result.stopped_confirmed)
        self.assertEqual(result.consecutive_stopped_polls, 1)

    def test_client_emergency_stop_does_not_require_sync_authority(self):
        fake = FakeTransport([
            prop("OnStep Status", {":GU# return": "nNpET260"}, 2),
            prop("OnStep Status", {":GU# return": "nNpET260"}, 3),
        ])
        result = OnStepIndiClient(transport=fake).emergency_stop()
        self.assertTrue(result.stopped_confirmed)
        self.assertEqual(len(fake.commands), 2)


if __name__ == "__main__":
    unittest.main()
