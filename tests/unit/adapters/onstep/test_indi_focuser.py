import time
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from onstep_adapter.indi_config import load_indi_config
from onstep_adapter.indi_focuser import IndiFocuser
from onstep_adapter.indi_transport import IndiProperty


DEVICE = "LX200 OnStep"


def prop(name, element, value, revision=1, state="Ok"):
    return IndiProperty(
        DEVICE, name, "Number", state, "rw", {element: str(value)},
        time.monotonic(), revision,
    )


class FakeTransport:
    def __init__(self):
        self.is_open = True
        self.commands = []
        self.properties = {
            "CONNECTION": prop("CONNECTION", "CONNECT", "On"),
            "ABS_FOCUS_POSITION": prop(
                "ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION", 15145
            ),
            "FOCUS_MAX": prop("FOCUS_MAX", "FOCUS_MAX_VALUE", 100000),
        }
        self.updates = []
        self.max_after_issue = None

    def get_property(self, device, name):
        return self.properties.get(name)

    def wait_property(self, device, name, *, timeout, after_revision=-1):
        if not self.updates:
            raise TimeoutError("no fresh position")
        result = self.updates.pop(0)
        self.properties[name] = result
        return result

    def issue_number(self, device, name, element, value):
        self.commands.append((name, element, value))
        if self.max_after_issue is not None:
            self.properties["FOCUS_MAX"] = self.max_after_issue

    def request_switch(self, device, name, element, *, timeout):
        self.commands.append((name, element))


def config():
    path = Path(__file__).resolve().parents[4] / "config.indi.example.toml"
    return load_indi_config(path)


class IndiFocuserTests(unittest.TestCase):
    def test_conflicting_maximum_refuses_motion(self):
        fake = FakeTransport()
        focuser = IndiFocuser(fake, config())
        self.assertTrue(focuser.get_status().move_ready)
        fake.properties["FOCUS_MAX"] = prop("FOCUS_MAX", "FOCUS_MAX_VALUE", 1000, 2)
        snapshot = focuser.get_status()
        self.assertIn("focuser_limit_conflicts_with_position", snapshot.blockers)
        result = focuser.move_absolute(15200)
        self.assertFalse(result.reached)
        self.assertEqual(fake.commands, [])

    def test_static_driver_maximum_and_two_target_updates(self):
        fake = FakeTransport()
        focuser = IndiFocuser(fake, config())
        snapshot = focuser.get_status()
        self.assertEqual(snapshot.driver_maximum, 100000)
        self.assertEqual(snapshot.configured_maximum, 50000)
        self.assertTrue(snapshot.move_ready)
        fake.updates = [
            prop("ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION", 15170, 3, "Busy"),
            prop("ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION", 15200, 4),
            prop("ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION", 15200, 5),
        ]
        result = focuser.move_absolute(15200)
        self.assertTrue(result.reached)
        self.assertEqual(result.final_position, 15200)
        self.assertEqual(fake.commands, [
            ("ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION", 15200)
        ])

    def test_configured_ceiling_applies_below_driver_maximum(self):
        fake = FakeTransport()
        focuser = IndiFocuser(fake, config())
        with self.assertRaisesRegex(ValueError, "travel limits"):
            focuser.move_absolute(50001)
        self.assertEqual(fake.commands, [])

    def test_static_maximum_does_not_expire_while_position_must_be_fresh(self):
        fake = FakeTransport()
        old = time.monotonic() - 60
        maximum = fake.properties["FOCUS_MAX"]
        fake.properties["FOCUS_MAX"] = replace(maximum, received_at=old)
        focuser = IndiFocuser(fake, config())
        self.assertTrue(focuser.get_status().move_ready)
        position = fake.properties["ABS_FOCUS_POSITION"]
        fake.properties["ABS_FOCUS_POSITION"] = replace(position, received_at=old)
        self.assertIn("focuser_position_stale", focuser.get_status().blockers)

    def test_missing_configured_maximum_blocks_move(self):
        fake = FakeTransport()
        focuser = IndiFocuser(fake, replace(config(), focuser_max_position=None))
        self.assertIn("focuser_configured_maximum_missing", focuser.get_status().blockers)
        self.assertFalse(focuser.move_absolute(15200).reached)
        self.assertEqual(fake.commands, [])

    def test_stop_is_available_without_limit_authority_and_requires_stability(self):
        fake = FakeTransport()
        fake.updates = [
            prop("ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION", 15146, 2),
            prop("ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION", 15146, 3),
            prop("ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION", 15146, 4),
        ]
        self.assertTrue(IndiFocuser(fake, config()).stop())
        self.assertEqual(fake.commands, [("FOCUS_ABORT_MOTION", "ABORT")])

    def test_limit_change_during_move_requests_stop(self):
        fake = FakeTransport()
        focuser = IndiFocuser(fake, config())
        fake.updates = [
            prop("ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION", 15146, 3),
            prop("ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION", 15146, 4),
            prop("ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION", 15146, 5),
        ]
        fake.max_after_issue = prop("FOCUS_MAX", "FOCUS_MAX_VALUE", 1000, 3)
        result = focuser.move_absolute(15200)
        self.assertFalse(result.reached)
        self.assertIn("limit", result.error)
        self.assertEqual(fake.commands[-1], ("FOCUS_ABORT_MOTION", "ABORT"))

    def test_stop_command_is_not_blocked_by_active_move(self):
        class WaitingTransport(FakeTransport):
            def __init__(self):
                super().__init__()
                self.move_started = threading.Event()
                self.aborted = threading.Event()
                self.revision = 2

            def issue_number(self, device, name, element, value):
                super().issue_number(device, name, element, value)
                self.move_started.set()

            def wait_property(self, device, name, *, timeout, after_revision=-1):
                if self.aborted.is_set():
                    self.revision += 1
                    return prop(
                        "ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION",
                        15146, self.revision,
                    )
                time.sleep(min(timeout, 0.02))
                raise TimeoutError("no update")

            def request_switch(self, device, name, element, *, timeout):
                super().request_switch(device, name, element, timeout=timeout)
                self.aborted.set()

        fake = WaitingTransport()
        focuser = IndiFocuser(fake, config())
        outcome = []
        worker = threading.Thread(
            target=lambda: outcome.append(focuser.move_absolute(16145, timeout=2))
        )
        worker.start()
        self.assertTrue(fake.move_started.wait(1))
        self.assertTrue(focuser.stop(timeout=0.1))
        self.assertIn(("FOCUS_ABORT_MOTION", "ABORT"), fake.commands)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIn("cancelled", outcome[0].error)


if __name__ == "__main__":
    unittest.main()
