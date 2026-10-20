from unittest.mock import Mock

import pytest

from onstep_adapter.mount import (
    OnStepMount,
    _fold_horizontal_target,
    _format_altitude,
    _format_azimuth,
)


def _mount_with_replies(*replies: str) -> tuple[OnStepMount, Mock]:
    mount = object.__new__(OnStepMount)
    mount._at_mechanical_home = True
    sender = Mock(side_effect=replies)
    mount._send = sender
    return mount, sender


def test_horizontal_target_folds_across_zenith() -> None:
    assert _fold_horizontal_target(105.0, 0.0) == (75.0, 180.0)
    assert _fold_horizontal_target(120.0, 0.0) == (60.0, 180.0)
    assert _fold_horizontal_target(135.0, 0.0) == (45.0, 180.0)
    assert _fold_horizontal_target(-105.0, 20.0) == (-75.0, 200.0)


def test_native_altaz_goto_emits_only_sa_sz_ma() -> None:
    mount, sender = _mount_with_replies("1", "1", "0")

    assert mount.goto_altaz(105.0, 0.0) is True

    assert [call.args[0] for call in sender.call_args_list] == [
        f":Sa{_format_altitude(75.0)}#",
        f":Sz{_format_azimuth(180.0)}#",
        ":MA#",
    ]
    assert mount._at_mechanical_home is False


@pytest.mark.parametrize("axis", ["alt", "az"])
@pytest.mark.parametrize("offset", [-15.0, -5.0, -1.0, 1.0, 5.0, 15.0])
def test_finite_horizontal_move_uses_only_native_altaz_commands(
    axis: str, offset: float,
) -> None:
    mount, sender = _mount_with_replies("+80*00", "350*00", "1", "1", "0")

    assert mount.move_horizontal_deg(axis, offset) is True

    commands = [call.args[0] for call in sender.call_args_list]
    assert commands[:2] == [":GA#", ":GZ#"]
    assert commands[-1] == ":MA#"
    assert not any(command.startswith((":Sr", ":Sd")) for command in commands)
    assert ":MS#" not in commands
    expected_alt = 80.0 + offset if axis == "alt" else 80.0
    expected_az = 350.0 if axis == "alt" else (350.0 + offset) % 360.0
    expected_alt, expected_az = _fold_horizontal_target(expected_alt, expected_az)
    assert commands[2:4] == [
        f":Sa{_format_altitude(expected_alt)}#",
        f":Sz{_format_azimuth(expected_az)}#",
    ]


@pytest.mark.parametrize(
    ("axis", "offset"),
    [("ra", 1.0), ("dec", 1.0), ("alt", 0.0), ("az", 15.01),
     ("alt", float("nan")), ("az", float("inf"))],
)
def test_invalid_horizontal_move_never_touches_transport(
    axis: str, offset: float,
) -> None:
    mount, sender = _mount_with_replies()
    with pytest.raises(ValueError):
        mount.move_horizontal_deg(axis, offset)
    sender.assert_not_called()


@pytest.mark.parametrize(
    ("replies", "message"),
    [
        (("0",), "horizontal_altitude_target_rejected"),
        (("1", "0"), "horizontal_azimuth_target_rejected"),
        (("1", "1", "6"), "horizontal_goto_rejected"),
    ],
)
def test_native_altaz_goto_propagates_controller_rejection(
    replies: tuple[str, ...], message: str,
) -> None:
    mount, _ = _mount_with_replies(*replies)
    with pytest.raises(RuntimeError, match=message):
        mount.goto_altaz(45.0, 90.0)

