from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from onstep_adapter import OnStepMotionCalibration, OnStepSafetyError
from smart_telescope.adapters.onstep.mount import OnStepMount
from smart_telescope.adapters.onstep.safety import OnStepLimitError, OnStepSafetyConfig

from .fake_serial import FakeOnStepSerial


def _calibration() -> OnStepMotionCalibration:
    return OnStepMotionCalibration(
        guide_ra_east_arcsec_per_s=10.0,
        guide_ra_west_arcsec_per_s=11.0,
        guide_dec_north_arcsec_per_s=12.0,
        guide_dec_south_arcsec_per_s=13.0,
        center_ra_east_arcsec_per_s=100.0,
        center_ra_west_arcsec_per_s=110.0,
        center_dec_north_arcsec_per_s=120.0,
        center_dec_south_arcsec_per_s=130.0,
    )


def _mount(*, calibration: OnStepMotionCalibration | None = None) -> tuple[OnStepMount, FakeOnStepSerial]:
    config = OnStepSafetyConfig(
        observer_lat=50.0,
        observer_lon=8.0,
        min_alt_deg=-5.0,
        max_alt_deg=90.0,
        ha_east_limit_h=-5.5,
        ha_west_limit_h=5.0 / 15.0,
        require_home_confirmation=False,
        time_trust_source="user_confirmed",
    )
    fake = FakeOnStepSerial(
        initial_state="tracking",
        initial_ra=4.0,
        initial_dec=20.0,
        sidereal_hours=4.0,
    )
    mount = OnStepMount(
        "/dev/fake",
        safety_config=config,
        motion_calibration=calibration,
    )
    mount._serial = fake
    return mount, fake


def _safe_preflight() -> dict[str, object]:
    return {
        "motion_refused": False,
        "tracking": True,
        "at_home": False,
        "logical_position": {"ra": 4.0, "dec": 20.0},
        "hard_limit_reached": False,
        "tracking_stop_required": False,
    }


def test_center_ra_is_bounded_stops_direction_and_restores_guide_rate() -> None:
    mount, fake = _mount(calibration=_calibration())
    with (
        patch.object(mount, "motion_safety_preflight", return_value=_safe_preflight()),
        patch.object(mount, "validate_target", return_value={"allowed": True, "violation": None}),
    ):
        result = mount.move_ra_timed("east", 20, mode="center")

    commands = [command.decode() for command in fake.commands_received]
    assert result.ok is True
    assert result.axis == "ra"
    assert result.direction == "e"
    assert commands.index(":RC#") < commands.index(":Me#") < commands.index(":Qe#")
    assert commands.index(":Qe#") < commands.index(":RG#")
    assert result.tracking_before is True
    assert result.tracking_after is True


def test_guide_dec_uses_native_pulse_and_guaranteed_direction_stop() -> None:
    mount, fake = _mount(calibration=_calibration())
    with (
        patch.object(mount, "motion_safety_preflight", return_value=_safe_preflight()),
        patch.object(mount, "validate_target", return_value={"allowed": True, "violation": None}),
    ):
        result = mount.move_dec_timed("south", 20, mode="guide")

    commands = [command.decode() for command in fake.commands_received]
    assert ":RG#" in commands
    assert ":Mgs0020#" in commands
    assert ":Qs#" in commands
    assert result.mode == "guide"


@pytest.mark.parametrize(
    ("method", "offset", "expected_direction", "expected_ms"),
    [
        ("move_ra", 1.0, "e", 100),
        ("move_ra", -1.1, "w", 100),
        ("move_dec", 1.2, "n", 100),
        ("move_dec", -1.3, "s", 100),
    ],
)
def test_angular_offsets_use_direction_specific_calibration(
    method: str,
    offset: float,
    expected_direction: str,
    expected_ms: int,
) -> None:
    mount, _ = _mount(calibration=_calibration())
    with (
        patch.object(mount, "motion_safety_preflight", return_value=_safe_preflight()),
        patch.object(mount, "validate_target", return_value={"allowed": True, "violation": None}),
    ):
        result = getattr(mount, method)(offset, mode="guide")

    assert result.direction == expected_direction
    assert result.estimated_duration_ms == expected_ms
    assert result.requested_arcsec == offset
    assert result.verification_required is True


def test_angular_offset_requires_calibration() -> None:
    mount, fake = _mount(calibration=None)

    with pytest.raises(ValueError, match="motion calibration is required"):
        mount.move_ra(1.0)

    assert b":Me#" not in fake.commands_received


def test_guide_requires_tracking_without_sending_motion() -> None:
    mount, fake = _mount(calibration=_calibration())
    preflight = _safe_preflight()
    preflight["tracking"] = False
    with patch.object(mount, "motion_safety_preflight", return_value=preflight):
        with pytest.raises(OnStepSafetyError, match="guide_requires_tracking"):
            mount.move_ra_timed("east", 20, mode="guide")

    assert b":Mge0020#" not in fake.commands_received


def test_motion_refused_at_home_without_sending_motion() -> None:
    mount, fake = _mount(calibration=_calibration())
    preflight = _safe_preflight()
    preflight["at_home"] = True
    with patch.object(mount, "motion_safety_preflight", return_value=preflight):
        with pytest.raises(OnStepSafetyError, match="axis_motion_refused_at_home"):
            mount.move_dec_timed("north", 20)

    assert b":Mn#" not in fake.commands_received


def test_live_hard_limit_stops_tracking_and_raises_limit_error() -> None:
    mount, fake = _mount(calibration=_calibration())
    hard = _safe_preflight()
    hard["hard_limit_reached"] = True
    hard["tracking_stop_required"] = True
    with (
        patch.object(mount, "motion_safety_preflight", side_effect=[_safe_preflight(), hard]),
        patch.object(mount, "validate_target", return_value={"allowed": True, "violation": None}),
    ):
        with pytest.raises(OnStepLimitError, match="axis_motion_reached_hard_limit"):
            mount.move_ra_timed("east", 300, mode="center")

    commands = [command.decode() for command in fake.commands_received]
    assert ":Qe#" in commands
    assert ":Td#" in commands
    assert ":Q#" in commands


def test_cancellation_stops_and_returns_cancelled_result() -> None:
    mount, fake = _mount(calibration=_calibration())
    cancelled = threading.Event()
    cancelled.set()
    with (
        patch.object(mount, "motion_safety_preflight", return_value=_safe_preflight()),
        patch.object(mount, "validate_target", return_value={"allowed": True, "violation": None}),
    ):
        result = mount.move_dec_timed(
            "north",
            100,
            mode="center",
            cancel_check=cancelled.is_set,
        )

    assert result.ok is False
    assert result.cancelled is True
    assert b":Qn#" in fake.commands_received


def test_concurrent_axis_motion_is_rejected() -> None:
    mount, _ = _mount(calibration=_calibration())
    mount._axis_motion_lock.acquire()
    try:
        with pytest.raises(OnStepSafetyError, match="axis_motion_already_in_progress"):
            mount.move_ra_timed("east", 20)
    finally:
        mount._axis_motion_lock.release()


def test_guide_compatibility_wrapper_uses_new_engine() -> None:
    mount, fake = _mount(calibration=_calibration())
    with (
        patch.object(mount, "motion_safety_preflight", return_value=_safe_preflight()),
        patch.object(mount, "validate_target", return_value={"allowed": True, "violation": None}),
    ):
        assert mount.guide("e", 20) is True

    assert b":Mge0020#" in fake.commands_received
