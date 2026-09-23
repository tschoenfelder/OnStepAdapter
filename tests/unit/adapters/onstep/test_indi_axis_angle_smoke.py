import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from onstep_adapter.indi_config import IndiRuntimeConfig
from onstep_adapter.tools import indi_axis_angle_smoke as smoke


def config():
    return IndiRuntimeConfig(
        host="127.0.0.1", port=7624, device="LX200 OnStep",
        observer_lat=50.336, observer_lon=8.533, observer_alt_m=304.0,
        flip_request_deg=1.0, tracking_stop_deg=1.75,
        flip_allowance_seconds=120.0, reserve_seconds=30.0,
        safe_meridian_flip_via_home=True,
    )


class IndiAxisAngleSmokeTests(unittest.TestCase):
    def test_abort_after_home_stops_without_return_motion(self):
        client = Mock()
        client.observe_mount.return_value = SimpleNamespace(
            parked=True, slewing=False, tracking=False, at_limit=False,
            status_live=True, coordinates_live=True, raw_status="nNPET260",
        )
        client.unpark.return_value = SimpleNamespace(unparked_confirmed=True)
        with (
            patch.object(smoke, "load_indi_config", return_value=config()),
            patch.object(smoke, "OnStepClient", return_value=client),
            patch.object(smoke, "wait_live", side_effect=[
                client.observe_mount.return_value,
                SimpleNamespace(
                    parked=False, tracking=False, slewing=False, at_limit=False,
                ),
            ]),
            patch.object(smoke, "confirm", side_effect=[
                None, smoke.OperatorAbort("unsafe unparked pose")
            ]),
        ):
            self.assertEqual(smoke.run("unused.toml", goto_timeout_s=120), 1)
        client.emergency_stop.assert_called_once()
        client.route_park_to_home.assert_not_called()
        client.go_home.assert_not_called()
        client.park.assert_not_called()
        client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
