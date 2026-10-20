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

