from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from onstep_adapter.indi_config import IndiRuntimeConfig
from onstep_adapter.indi_mode import IndiOperatingMode
from onstep_adapter.indi_stop import IndiStopResult
from onstep_adapter.indi_terrestrial import IndiTerrestrialController


CONFIG = IndiRuntimeConfig(
    host="127.0.0.1", port=7624, device="LX200 OnStep",
    observer_lat=50.336, observer_lon=8.533, observer_alt_m=304,
    flip_request_deg=1, tracking_stop_deg=1.75,
    flip_allowance_seconds=120, reserve_seconds=30,
    safe_meridian_flip_via_home=True,
    mode_state_path="mode.json", terrestrial_stop_hold_seconds=0,
)


def sample(*, ra=10.0, dec=20.0, alt=40.0, tracking=False, **changes):
    values = dict(
        status_live=True, coordinates_live=True, horizontal_live=True,
        park_evidence_consistent=False,
        altitude_deg=alt, ra_hours=ra, dec_deg=dec, parked=False,
        at_home=False, slewing=False, guiding=False, tracking=tracking,
        at_limit=False, blockers=(),
    )
    values.update(changes)
    return SimpleNamespace(**values)


class Mode:
    def require(self, expected):
        assert expected is IndiOperatingMode.TERRESTRIAL


class Transport:
    def __init__(self):
        self.commands = []

    def get_property(self, device, name):
        assert name == "TELESCOPE_SLEW_RATE"
        return SimpleNamespace(values={str(value): "Off" for value in range(10)})

    def set_switch(self, device, name, element):
        self.commands.append(("set", name, element))

    def issue_switch(self, device, name, element):
        self.commands.append(("issue", name, element))


def controller(samples, *, reference_ra=10.0, config=CONFIG):
    queue = list(samples)
    transport = Transport()
    stops = []

    def observe():
        if not queue:
            raise AssertionError("No mocked terrestrial sample remains")
        return queue.pop(0)

    def stop(**kwargs):
        stops.append("stop")
        return IndiStopResult(True, True, True, 2, "nNpET260", ())

    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    result = IndiTerrestrialController(
        transport, config, Mode(), observe=observe, stop=stop,
        clock=lambda: now,
    )
    result.capture_park_reference(sample(
        ra=reference_ra, parked=True, park_evidence_consistent=True
    ))
    result.authorize_stationary_start(sample(ra=reference_ra))
    return result, transport, stops


@patch("onstep_adapter.indi_terrestrial.time.sleep")
def test_near_side_toward_horizon_uses_north_and_stops_early(unused_sleep):
    mover, transport, stops = controller([
        sample(), sample(dec=25, alt=35, slewing=True),
        sample(dec=29.6, alt=30.4, slewing=True),
        sample(dec=29.7, alt=30.3), sample(dec=29.7, alt=30.3),
    ])
    result = mover.move_toward_north_horizon()
    assert result.indi_direction == "MOTION_NORTH"
    assert result.commanded_deg == 10
    assert result.stop_confirmed and result.endpoint_is_estimated
    assert transport.commands == [
        ("set", "TELESCOPE_SLEW_RATE", "9"),
        ("issue", "TELESCOPE_MOTION_NS", "MOTION_NORTH"),
    ]
    assert result.slew_rate_element == "9"
    assert stops == ["stop"]


@patch("onstep_adapter.indi_terrestrial.time.sleep")
def test_far_side_reverses_toward_and_outward_direction(unused_sleep):
    toward, transport, _ = controller([
        sample(ra=22), sample(ra=22, dec=25, slewing=True),
        sample(ra=22, dec=29.6, slewing=True), sample(ra=22, dec=29.7),
        sample(ra=22, dec=29.7),
    ])
    result = toward.move_toward_north_horizon()
    assert result.pole_branch == "far"
    assert result.indi_direction == "MOTION_SOUTH"

    away, _, _ = controller([
        sample(ra=22), sample(ra=22, dec=25, slewing=True),
        sample(ra=22, dec=29.6, slewing=True), sample(ra=22, dec=29.7),
        sample(ra=22, dec=29.7),
    ])
    assert away.move_away_from_north_horizon().indi_direction == "MOTION_NORTH"


@patch("onstep_adapter.indi_terrestrial.time.sleep")
def test_final_step_is_clipped_to_configured_horizon_margin(unused_sleep):
    mover, _, _ = controller([
        sample(alt=5), sample(dec=24.6, alt=0.4, slewing=True),
        sample(dec=24.7, alt=0.3), sample(dec=24.7, alt=0.3),
    ])
    result = mover.move_toward_north_horizon(10)
    assert result.clipped
    assert result.commanded_deg == 5
    assert result.final_altitude_deg == 0.3


def test_ambiguous_pole_branch_refuses_without_motion():
    mover, transport, stops = controller([sample(ra=16)])
    with pytest.raises(RuntimeError, match="ambiguous"):
        mover.move_toward_north_horizon()
    assert transport.commands == [] and stops == []


def test_stationary_unpark_anchor_allows_delayed_first_step_preflight():
    mover, transport, stops = controller([
        sample(status_live=False, coordinates_live=False, horizontal_live=False),
    ])
    with pytest.raises(AssertionError, match="No mocked terrestrial sample remains"):
        mover.move_toward_north_horizon()
    assert transport.commands == [
        ("set", "TELESCOPE_SLEW_RATE", "9"),
        ("issue", "TELESCOPE_MOTION_NS", "MOTION_NORTH"),
    ]
    assert stops == ["stop"]


def test_stale_position_different_from_unpark_anchor_refuses():
    mover, transport, stops = controller([
        sample(ra=11.0, status_live=False, coordinates_live=False, horizontal_live=False),
    ])
    with pytest.raises(RuntimeError, match="Fresh"):
        mover.move_toward_north_horizon()
    assert transport.commands == [] and stops == []


def test_legacy_slew_max_alias_resolves_to_highest_numeric_driver_rate():
    legacy = replace(CONFIG, terrestrial_slew_rate_element="SLEW_MAX")
    mover, transport, stops = controller([
        sample(), sample(dec=25, alt=35, slewing=True),
        sample(dec=29.6, alt=30.4, slewing=True),
        sample(dec=29.7, alt=30.3), sample(dec=29.7, alt=30.3),
    ], config=legacy)

    result = mover.move_toward_north_horizon()

    assert result.slew_rate_element == "9"
    assert transport.commands[0] == ("set", "TELESCOPE_SLEW_RATE", "9")
    assert stops == ["stop"]


@patch("onstep_adapter.indi_terrestrial.time.sleep")
def test_long_move_accepts_unchanged_status_while_coordinates_refresh(unused_sleep):
    mover, _, stops = controller([
        sample(),
        sample(dec=25, alt=35, slewing=True, status_live=False),
        sample(dec=29.6, alt=30.4, slewing=True, status_live=False),
        sample(dec=29.7, alt=30.3, status_live=False),
        sample(dec=29.7, alt=30.3, status_live=False),
    ])

    result = mover.move_toward_north_horizon()

    assert result.stop_confirmed
    assert stops == ["stop"]


def test_stale_coordinates_during_motion_trigger_stop():
    mover, _, stops = controller([
        sample(),
        sample(coordinates_live=False, horizontal_live=False, slewing=True),
    ])

    with pytest.raises(RuntimeError, match="coordinate feedback became stale"):
        mover.move_toward_north_horizon()

    assert stops == ["stop"]


@pytest.mark.parametrize(
    "unsafe,message",
    [
        ({"tracking": True}, "Tracking"),
        ({"at_limit": True}, "Fault"),
        ({"slewing": True}, "stationary"),
    ],
)
def test_unsafe_start_states_refuse_without_motion(unsafe, message):
    mover, transport, stops = controller([sample(**unsafe)])
    with pytest.raises(RuntimeError, match=message):
        mover.move_toward_north_horizon()
    assert transport.commands == [] and stops == []


def test_stale_start_without_verified_stationary_anchor_refuses():
    mover, transport, stops = controller([
        sample(status_live=False, coordinates_live=False, horizontal_live=False),
    ])
    mover._stationary_reference = None
    with pytest.raises(RuntimeError, match="Fresh"):
        mover.move_toward_north_horizon()
    assert transport.commands == [] and stops == []


def test_step_is_bounded_by_configured_maximum():
    mover, transport, _ = controller([])
    with pytest.raises(ValueError, match="max_step"):
        mover.move_toward_north_horizon(10.01)
    assert transport.commands == []
