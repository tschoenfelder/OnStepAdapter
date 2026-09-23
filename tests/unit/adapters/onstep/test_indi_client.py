import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace

from onstep_adapter.indi_config import load_indi_config
from onstep_adapter.indi_client import OnStepIndiClient
from onstep_adapter.indi_transport import IndiProperty


def prop(name, values):
    return IndiProperty("LX200 OnStep", name, "Switch", "Ok", "rw", values, 0.0, 1)


class FakeTransport:
    def __init__(self, *, safe_flip=True, guard_minutes=(12, 8), safe_state="Ok", time_age_s=0,
                 host="127.0.0.1", reject_time=False, reject_location=False):
        self.closed = False
        self.settings = []
        self.writes = []
        self.host = host
        self.is_open = False
        self.reject_time = reject_time
        self.reject_location = reject_location
        self.properties = {}
        self.safe_flip = safe_flip
        self.guard_minutes = guard_minutes
        self.safe_state = safe_state
        self.time_age_s = time_age_s

    def connect(self, *, timeout):
        self.closed = False
        self.is_open = True
        self.properties["CONNECTION"] = prop("CONNECTION", {"CONNECT": "On"})

    def wait_property(self, device, name, *, timeout):
        if name == "CONNECTION":
            return prop(name, {"CONNECT": "On"})
        if name == "TIME_UTC":
            timestamp = datetime.now(timezone.utc) - timedelta(seconds=self.time_age_s)
            return prop(name, {"UTC": timestamp.isoformat()})
        if name == "SAFE_MERIDIAN_FLIP":
            result = prop(name, {"ON": "On" if self.safe_flip else "Off"})
            return IndiProperty(
                result.device, result.name, result.kind, self.safe_state,
                result.permission, result.values, result.received_at, result.revision,
            )
        if name == "Minutes Past Meridian":
            return prop(name, {"East": str(self.guard_minutes[0]), "West": str(self.guard_minutes[1])})
        raise AssertionError(name)

    def set_switch(self, device, name, element, *, timeout):
        self.settings.append((name, element))
        self.safe_flip = True
        return prop(name, {"ON": "On"})

    def close(self):
        self.closed = True
        self.is_open = False

    def set_text(self, device, name, values, *, timeout):
        self.writes.append((name, values))
        if self.reject_time:
            raise RuntimeError("time rejected")
        result = prop(name, values)
        self.properties[name] = result
        return result

    def set_number(self, device, name, values, *, timeout):
        self.writes.append((name, values))
        if self.reject_location:
            raise RuntimeError("location rejected")
        result = prop(name, values)
        self.properties[name] = result
        return result

    def get_property(self, device, name):
        return self.properties.get(name)


CONFIG = """[indi]
host = "127.0.0.1"
port = 7624
device = "LX200 OnStep"
safe_meridian_flip_via_home = true
[observer]
lat = 50.336
lon = 8.533
alt_m = 304.0
[meridian]
flip_request_deg = 1.0
tracking_stop_deg = 1.75
flip_allowance_seconds = 120.0
reserve_seconds = 30.0
"""


def configured_client(fake):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "config.toml"
        path.write_text(CONFIG, encoding="ascii")
        settings = load_indi_config(path)
    return OnStepIndiClient(
        config=settings, transport=fake,
        clock=lambda: datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone(timedelta(hours=2))),
        auto_supervise=False,
    )


class IndiClientTests(unittest.TestCase):
    def test_public_context_manager_closes_only_local_transport(self):
        from onstep_adapter import OnStepClient

        fake = FakeTransport()
        with OnStepClient(transport=fake) as client:
            self.assertIsInstance(client, OnStepIndiClient)
            self.assertTrue(fake.is_open)
        self.assertTrue(fake.closed)
        self.assertEqual(fake.settings, [])

    def test_repeat_connect_does_not_discard_accepted_baseline(self):
        fake = FakeTransport()
        client = configured_client(fake)
        client.connect()
        client.sync_time_location(user_approved=True)
        with self.assertRaisesRegex(RuntimeError, "already connected"):
            client.connect()
        self.assertTrue(client.session_baseline_accepted)
        client.close()

    def test_close_releases_socket_even_if_supervisor_shutdown_fails(self):
        fake = FakeTransport()
        client = configured_client(fake)
        client.connect()

        class StuckSupervisor:
            def close(self):
                raise TimeoutError("worker did not stop")

        client._supervisor = StuckSupervisor()
        with self.assertRaises(TimeoutError):
            client.close()
        self.assertTrue(fake.closed)

    def test_default_enables_safe_home_route_without_disabling_on_close(self):
        fake = FakeTransport(safe_flip=False)
        client = OnStepIndiClient(transport=fake)
        status = client.connect()

        self.assertEqual(fake.settings, [("SAFE_MERIDIAN_FLIP", "ON")])
        self.assertTrue(status.safe_meridian_flip_enabled)
        self.assertEqual(status.meridian_policy.hard_stop_deg, 1.75)
        self.assertFalse(status.controller_time_verified)
        self.assertFalse(status.astronomical_motion_ready)
        client.close()
        self.assertTrue(fake.closed)
        self.assertEqual(fake.settings, [("SAFE_MERIDIAN_FLIP", "ON")])

    def test_bad_guard_closes_client(self):
        fake = FakeTransport(guard_minutes=(12, 0))
        client = OnStepIndiClient(transport=fake)
        with self.assertRaises(ValueError):
            client.connect()
        self.assertTrue(fake.closed)

    def test_alert_safe_flip_blocks_connection(self):
        fake = FakeTransport(safe_state="Alert")
        client = OnStepIndiClient(transport=fake)
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            client.connect()
        self.assertTrue(fake.closed)

    def test_old_published_snapshot_does_not_block_observation_or_authorize_motion(self):
        fake = FakeTransport(time_age_s=3600)
        client = OnStepIndiClient(transport=fake)
        status = client.connect()
        self.assertTrue(status.device_connected)
        self.assertGreater(status.time_snapshot_age_seconds, 3500)
        self.assertFalse(status.controller_time_verified)
        self.assertFalse(status.astronomical_motion_ready)
        self.assertFalse(fake.closed)
        client.close()

    def test_recent_published_snapshot_also_does_not_verify_controller_time(self):
        status = OnStepIndiClient(transport=FakeTransport()).connect()
        self.assertLess(status.time_snapshot_age_seconds, 120)
        self.assertFalse(status.controller_time_verified)
        self.assertFalse(status.astronomical_motion_ready)

    def test_user_approved_sync_uses_raspberry_time_and_configured_site(self):
        fake = FakeTransport()
        client = configured_client(fake)
        client.connect()
        result = client.sync_time_location(user_approved=True)
        self.assertTrue(result.time_accepted and result.location_accepted)
        self.assertEqual(result.time_authority, "accepted_not_independently_read_back")
        self.assertEqual(fake.writes[0], ("TIME_UTC", {
            "UTC": "2026-09-22T10:00:00", "OFFSET": "+2.00"
        }))
        self.assertEqual(fake.writes[1][0], "GEOGRAPHIC_COORD")
        self.assertEqual(fake.writes[1][1]["LONG"], 8.533)
        self.assertIn("tracking", result.warning)
        self.assertTrue(client.session_baseline_accepted)

    def test_observed_other_client_change_invalidates_only_local_authority(self):
        fake = FakeTransport()
        client = configured_client(fake)
        client.connect()
        client.sync_time_location(user_approved=True)
        self.assertTrue(client.session_baseline_accepted)
        fake.properties["TIME_UTC"] = prop(
            "TIME_UTC", {"UTC": "2026-09-22T10:01:00", "OFFSET": "+2.00"}
        )
        self.assertFalse(client.session_baseline_accepted)
        self.assertEqual([name for name, _ in fake.writes], ["TIME_UTC", "GEOGRAPHIC_COORD"])

    def test_device_disconnect_invalidates_local_authority_without_stop(self):
        fake = FakeTransport()
        client = configured_client(fake)
        client.connect()
        client.sync_time_location(user_approved=True)
        fake.properties["CONNECTION"] = prop("CONNECTION", {"CONNECT": "Off"})
        self.assertFalse(client.session_baseline_accepted)
        self.assertEqual(len(fake.writes), 2)

    def test_no_approval_sends_nothing(self):
        fake = FakeTransport()
        client = configured_client(fake)
        client.connect()
        with self.assertRaises(PermissionError):
            client.sync_time_location(user_approved=False)
        self.assertEqual(fake.writes, [])

    def test_partial_location_failure_is_reported_without_full_authority(self):
        fake = FakeTransport(reject_location=True)
        client = configured_client(fake)
        client.connect()
        result = client.sync_time_location(user_approved=True)
        self.assertTrue(result.time_accepted)
        self.assertFalse(result.location_accepted)
        self.assertEqual(result.error, "location rejected")
        self.assertFalse(client._session_time_site_accepted)

    def test_time_rejection_does_not_send_site(self):
        fake = FakeTransport(reject_time=True)
        client = configured_client(fake)
        client.connect()
        result = client.sync_time_location(user_approved=True)
        self.assertEqual(result.time_authority, "unestablished")
        self.assertEqual([name for name, _ in fake.writes], ["TIME_UTC"])

    def test_remote_host_cannot_be_used_as_raspberry_clock(self):
        fake = FakeTransport(host="rasppiserver3")
        client = configured_client(fake)
        client.connect()
        with self.assertRaisesRegex(ValueError, "beside the local"):
            client.sync_time_location(user_approved=True)
        self.assertEqual(fake.writes, [])

    def test_unsynced_client_observes_without_stopping_existing_mount(self):
        fake = FakeTransport()
        client = configured_client(fake)
        client.connect()
        observed = client.observe_mount()
        self.assertFalse(observed.time_site_authority)
        self.assertTrue(observed.motion_refused)
        self.assertEqual(fake.writes, [])

    def test_home_authority_is_local_and_cleared_when_client_closes(self):
        fake = FakeTransport()
        client = configured_client(fake)
        client.connect()
        client._config = replace(client._config, home_motion_enabled=True)
        self.assertFalse(client.home_authority_established)
        client._home_router = SimpleNamespace(
            authority_established=True,
            park_to_home=lambda **kwargs: "confirmed",
        )
        fake.properties["OnStep Status"] = prop(
            "OnStep Status", {":GU# return": "nNpHET260", "Error": "None"}
        )
        self.assertEqual(client.route_park_to_home(), "confirmed")
        self.assertTrue(client.home_authority_established)
        fake.properties["OnStep Status"] = prop(
            "OnStep Status", {":GU# return": "nNpHlET260", "Error": "None"}
        )
        self.assertFalse(client.home_authority_established)
        client._home_router.authority_established = True
        client.close()
        self.assertFalse(client.home_authority_established)
        self.assertTrue(fake.closed)

    def test_home_motion_is_disabled_without_deferred_hardware_proof(self):
        fake = FakeTransport()
        client = configured_client(fake)
        client.connect()
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            client.go_home()
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            client.unpark()
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            client.park()
        self.assertEqual(fake.writes, [])
        client.close()

    def test_supervisor_has_client_lifetime_only(self):
        fake = FakeTransport()
        client = configured_client(fake)
        client.connect()
        self.assertFalse(client._supervisor.is_running)
        client.start_supervision()
        self.assertTrue(client._supervisor.is_running)
        worker = client._supervisor
        client.close()
        self.assertFalse(worker.is_running)
        self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()
