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
        at_limit=False, blockers=(), raw_status="nNpeEW280",
    )
    values.update(changes)
    return SimpleNamespace(**values)


class Mode:
    def require(self, expected):
        assert expected is IndiOperatingMode.TERRESTRIAL


class Transport:
    def __init__(self):
        self.commands = []
        self.revision = 0
        self.properties = {}

    def get_property(self, device, name):
        assert name == "TELESCOPE_SLEW_RATE"
        return SimpleNamespace(values={str(value): "Off" for value in range(10)})

    def set_switch(self, device, name, element):
        self.commands.append(("set", name, element))

    def issue_switch(self, device, name, element):
        self.commands.append(("issue", name, element))

    def issue_switches(self, device, name, values):
        self.commands.append(("issues", name, dict(values)))
        before_revision = self.revision
        self.revision += 1
        self.properties[name] = SimpleNamespace(
            values={key: "On" if enabled else "Off" for key, enabled in values.items()},
            state="Busy" if any(values.values()) else "Idle",
            revision=self.revision,
        )
        return before_revision

    def wait_property(self, device, name, *, timeout, after_revision=-1):
        prop = self.properties.get(name)
        if prop is None or prop.revision <= after_revision:
            raise TimeoutError(f"No fresh mocked property {name}")
        return prop


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
def test_dec_path_accumulates_motion_safely_across_celestial_pole(mocked_sleep):
    mover, transport, stops = controller([
        sample(ra=10.0, dec=85.0, alt=70.0),
        sample(ra=10.0, dec=89.0, alt=66.0, slewing=True),
        sample(ra=22.0, dec=87.0, alt=62.0, slewing=True),
        sample(ra=22.0, dec=85.4, alt=60.4, slewing=True),
        sample(ra=22.0, dec=85.4, alt=60.4),
    ])

    result = mover.move_toward_north_horizon(10.0)

    assert result.stop_confirmed
    assert result.pole_branch == "near"
    assert result.indi_direction == "MOTION_NORTH"
    assert result.measured_deg == pytest.approx(9.6, abs=0.01)
    assert result.final_altitude_deg == pytest.approx(60.4)
    assert transport.commands == [
        ("set", "TELESCOPE_SLEW_RATE", "9"),
        ("issues", "TELESCOPE_MOTION_NS", {
            "MOTION_NORTH": False, "MOTION_SOUTH": False,
        }),
        ("issues", "TELESCOPE_MOTION_NS", {
            "MOTION_NORTH": True, "MOTION_SOUTH": False,
        }),
    ]
    assert stops == ["stop"]


@patch("onstep_adapter.indi_terrestrial.time.sleep")
def test_toward_horizon_reverses_indi_direction_on_far_pole_branch(mocked_sleep):
    mover, transport, stops = controller([
        sample(ra=22.0, dec=85.0, alt=60.0),
        sample(ra=22.0, dec=84.4, alt=59.4, slewing=True),
        sample(ra=22.0, dec=84.4, alt=59.4),
    ])

    result = mover.move_toward_north_horizon(1.0)

    assert result.pole_branch == "far"
    assert result.indi_direction == "MOTION_SOUTH"
    assert result.measured_deg == pytest.approx(0.6, abs=0.01)
    assert transport.commands[-1] == (
        "issues", "TELESCOPE_MOTION_NS",
        {"MOTION_NORTH": False, "MOTION_SOUTH": True},
    )
    assert stops == ["stop"]


@pytest.mark.parametrize(
    "offset,ra_after,element",
    [(-1.0, 10.04, "MOTION_EAST"), (1.0, 9.96, "MOTION_WEST")],
)
@patch("onstep_adapter.indi_terrestrial.time.sleep")
def test_ra_axis_uses_opposite_fixed_directions_and_verified_stop(
    mocked_sleep, offset, ra_after, element,
):
    mover, transport, stops = controller([
        sample(ra=10.0, dec=43.0, alt=60.0),
        sample(ra=ra_after, dec=43.0, alt=60.0, slewing=True),
        sample(ra=ra_after, dec=43.0, alt=60.0),
    ])

    result = mover.move_ra_axis(offset)

    assert result.stop_confirmed
    assert result.indi_direction == element
    assert result.measured_deg == pytest.approx(0.6, abs=0.01)
    assert transport.commands == [
        ("set", "TELESCOPE_SLEW_RATE", "9"),
        ("issues", "TELESCOPE_MOTION_WE", {
            "MOTION_WEST": False, "MOTION_EAST": False,
        }),
        ("issues", "TELESCOPE_MOTION_WE", {
            "MOTION_WEST": element == "MOTION_WEST",
            "MOTION_EAST": element == "MOTION_EAST",
        }),
    ]
    assert stops == ["stop"]


@patch("onstep_adapter.indi_terrestrial.time.sleep")
def test_ra_axis_wrong_direction_stops_immediately(mocked_sleep):
    mover, transport, stops = controller([
        sample(ra=10.0, dec=43.0, alt=60.0),
        sample(ra=9.96, dec=43.0, alt=60.0, slewing=True),
    ])

    with pytest.raises(RuntimeError, match="opposite"):
        mover.move_ra_axis(-1.0)

    assert transport.commands[-1] == (
        "issues", "TELESCOPE_MOTION_WE",
        {"MOTION_WEST": False, "MOTION_EAST": True},
    )
    assert stops == ["stop"]


@patch("onstep_adapter.indi_terrestrial.time.sleep")
@patch("onstep_adapter.indi_terrestrial.time.monotonic")
def test_ra_axis_reports_onstep_under_pole_stall_without_waiting_for_timeout(
    mocked_monotonic, mocked_sleep,
):
    config = IndiRuntimeConfig(**{
        **CONFIG.__dict__, "terrestrial_stall_timeout_seconds": 1.0,
    })
    mover, transport, stops = controller([
        sample(ra=10.0, dec=43.0, alt=60.0, raw_status="nNpeEW280"),
        sample(
            ra=10.0, dec=43.0, alt=60.0, raw_status="nNpeEW286",
            blockers=("onstep_reported_error",),
        ),
    ], config=config)
    mocked_monotonic.side_effect = [0.0] * 7

    with pytest.raises(RuntimeError, match=r"under-pole limit \(code 6\)"):
        mover.move_ra_axis(-1.0, timeout_s=30.0)

    assert stops == ["stop"]
    assert transport.commands[-1] == (
        "issues", "TELESCOPE_MOTION_WE",
        {"MOTION_WEST": False, "MOTION_EAST": True},
    )


def test_finite_axis_jog_uses_retained_session_authority_and_updates_anchor():
    mover, transport, stops = controller([
        sample(ra=10.0, dec=20.0),
        sample(ra=9.5, dec=20.0),
    ])
    calls = []

    result = mover.run_finite_axis_jog(
        lambda: calls.append("finite-target") or "completed"
    )

    assert result == "completed"
    assert calls == ["finite-target"]
    assert mover._stationary_reference == (9.5, 20.0)
    assert transport.commands == []
    assert stops == []


def test_finite_axis_jog_refuses_without_retained_park_unpark_session():
    transport = Transport()
    mover = IndiTerrestrialController(
        transport, CONFIG, Mode(), observe=lambda: sample(),
        stop=lambda **kwargs: None,
    )
    called = []

    with pytest.raises(RuntimeError, match="PARK then UNPARK"):
        mover.run_finite_axis_jog(lambda: called.append(True))

    assert called == []


def test_park_reference_accepts_cached_coordinates_after_reconciled_park():
    mover = IndiTerrestrialController(
        Transport(), CONFIG, Mode(), observe=lambda: sample(),
        stop=lambda **kwargs: None,
    )

    mover.capture_park_reference(sample(
        parked=True, park_evidence_consistent=True, coordinates_live=False,
        ra=7.5, dec=48.9,
    ))

    assert mover._park_reference is not None
    assert mover._park_reference[:2] == (7.5, 48.9)


@pytest.mark.parametrize(
    "changes",
    [
        {"ra": None}, {"dec": None}, {"park_evidence_consistent": False},
        {"slewing": True}, {"tracking": True}, {"at_limit": True},
    ],
)
def test_park_reference_rejects_missing_or_unsafe_evidence(changes):
    mover = IndiTerrestrialController(
        Transport(), CONFIG, Mode(), observe=lambda: sample(),
        stop=lambda **kwargs: None,
    )

    with pytest.raises(RuntimeError, match="reconciled stationary PARKED"):
        values = {"parked": True, "park_evidence_consistent": True} | changes
        mover.capture_park_reference(sample(**values))


@pytest.mark.parametrize(
    "direction,element",
    [("east", "MOTION_EAST"), ("west", "MOTION_WEST")],
)
@patch("onstep_adapter.indi_terrestrial.time.sleep")
def test_timed_ra_recovery_allows_limit_fault_and_always_stops(
    mocked_sleep, direction, element,
):
    fault = ("onstep_limit_or_park_fault", "onstep_reported_error")
    mover, transport, stops = controller([
        sample(at_limit=True, blockers=fault),
        sample(at_limit=True, blockers=fault),
    ])

    result = mover.recover_ra_timed(direction, 1.0)

    mocked_sleep.assert_called_once_with(1.0)
    assert result.direction == direction
    assert result.stop_confirmed
    assert result.limit_or_fault_present
    assert transport.commands == [
        ("set", "TELESCOPE_SLEW_RATE", "9"),
        ("issues", "TELESCOPE_MOTION_WE", {
            "MOTION_WEST": False, "MOTION_EAST": False,
        }),
        ("issues", "TELESCOPE_MOTION_WE", {
            "MOTION_WEST": element == "MOTION_WEST",
            "MOTION_EAST": element == "MOTION_EAST",
        }),
    ]
    assert stops == ["stop", "stop"]


@pytest.mark.parametrize(
    "direction,element",
    [("north", "MOTION_NORTH"), ("south", "MOTION_SOUTH")],
)
@patch("onstep_adapter.indi_terrestrial.time.sleep")
def test_timed_dec_recovery_allows_limit_fault_and_always_stops(
    mocked_sleep, direction, element,
):
    fault = ("onstep_limit_or_park_fault", "onstep_reported_error")
    mover, transport, stops = controller([
        sample(at_limit=True, blockers=fault),
        sample(at_limit=True, blockers=fault),
    ])

    result = mover.recover_dec_timed(direction, 0.5)

    mocked_sleep.assert_called_once_with(0.5)
    assert result.axis == "dec"
    assert result.direction == direction
    assert result.stop_confirmed
    assert result.limit_or_fault_present
    assert transport.commands == [
        ("set", "TELESCOPE_SLEW_RATE", "9"),
        ("issues", "TELESCOPE_MOTION_NS", {
            "MOTION_NORTH": False, "MOTION_SOUTH": False,
        }),
        ("issues", "TELESCOPE_MOTION_NS", {
            "MOTION_NORTH": element == "MOTION_NORTH",
            "MOTION_SOUTH": element == "MOTION_SOUTH",
        }),
    ]
    assert stops == ["stop", "stop"]


@patch("onstep_adapter.indi_terrestrial.time.sleep")
def test_repeated_same_direction_ra_jog_is_rearmed_each_time(mocked_sleep):
    mover, transport, stops = controller([
        sample(ra=10.0, dec=43.0, alt=60.0),
        sample(ra=10.04, dec=43.0, alt=60.0, slewing=True),
        sample(ra=10.04, dec=43.0, alt=60.0),
        sample(ra=10.04, dec=43.0, alt=60.0),
        sample(ra=10.08, dec=43.0, alt=60.0, slewing=True),
        sample(ra=10.08, dec=43.0, alt=60.0),
    ])

    first = mover.move_ra_axis(-1.0)
    second = mover.move_ra_axis(-1.0)

    assert first.stop_confirmed and second.stop_confirmed
    starts = [command for command in transport.commands if command[0] == "issues"]
    assert starts == [
        ("issues", "TELESCOPE_MOTION_WE", {
            "MOTION_WEST": False, "MOTION_EAST": False,
        }),
        ("issues", "TELESCOPE_MOTION_WE", {
            "MOTION_WEST": False, "MOTION_EAST": True,
        }),
        ("issues", "TELESCOPE_MOTION_WE", {
            "MOTION_WEST": False, "MOTION_EAST": False,
        }),
        ("issues", "TELESCOPE_MOTION_WE", {
            "MOTION_WEST": False, "MOTION_EAST": True,
        }),
    ]
    assert stops == ["stop", "stop"]


@pytest.mark.parametrize(
    "axis,direction,property_name,element",
    [
        ("ra", "west", "TELESCOPE_MOTION_WE", "MOTION_WEST"),
        ("dec", "south", "TELESCOPE_MOTION_NS", "MOTION_SOUTH"),
    ],
)
@patch("onstep_adapter.indi_terrestrial.time.sleep")
def test_normal_timed_jog_requires_session_and_fault_free_status(
    mocked_sleep, axis, direction, property_name, element,
):
    mover, transport, stops = controller([sample(), sample()])

    result = getattr(mover, f"move_{axis}_timed")(direction, 0.5)

    assert not result.recovery_mode
    assert result.axis == axis
    assert transport.commands == [
        ("set", "TELESCOPE_SLEW_RATE", "9"),
        ("issues", property_name, {
            ("MOTION_WEST" if axis == "ra" else "MOTION_NORTH"): False,
            ("MOTION_EAST" if axis == "ra" else "MOTION_SOUTH"): False,
        }),
        ("issues", property_name, {
            ("MOTION_WEST" if axis == "ra" else "MOTION_NORTH"):
                element in {"MOTION_WEST", "MOTION_NORTH"},
            ("MOTION_EAST" if axis == "ra" else "MOTION_SOUTH"):
                element in {"MOTION_EAST", "MOTION_SOUTH"},
        }),
    ]
    assert stops == ["stop", "stop"]


def test_normal_timed_jog_does_not_escalate_to_recovery_on_fault():
    fault = ("onstep_limit_or_park_fault", "onstep_reported_error")
    mover, transport, stops = controller([
        sample(at_limit=True, blockers=fault),
    ])

    with pytest.raises(RuntimeError, match="fault-free"):
        mover.move_dec_timed("north", 0.5)

    assert transport.commands == []
    assert stops == ["stop"]


def test_normal_timed_jog_requires_retained_park_unpark_session():
    mover, transport, stops = controller([sample()])
    mover._park_reference = None

    with pytest.raises(RuntimeError, match="PARK then UNPARK"):
        mover.move_dec_timed("north", 0.5)

    assert transport.commands == []
    assert stops == ["stop"]


def test_timed_ra_recovery_rejects_parked_even_with_limit_fault():
    mover, transport, stops = controller([
        sample(parked=True, at_limit=True, blockers=("onstep_limit_or_park_fault",)),
    ])

    with pytest.raises(RuntimeError, match="unparked"):
        mover.recover_ra_timed("east", 0.5)

    assert transport.commands == []
    assert stops == ["stop"]


@pytest.mark.parametrize("duration", (0.0, 2.1, float("nan")))
def test_timed_ra_recovery_duration_is_bounded(duration):
    mover, transport, stops = controller([])
    with pytest.raises(ValueError, match="duration"):
        mover.recover_ra_timed("east", duration)
    assert transport.commands == []
    assert stops == []
