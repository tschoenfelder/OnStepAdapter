from __future__ import annotations

import pytest

from onstep_adapter.tools.coordinate_axis_smoke import _coordinate_path
from smart_telescope.ports.mount import MountPosition


def test_coordinate_path_changes_one_axis_per_leg_and_returns() -> None:
    initial = MountPosition(ra=23.5, dec=20.0)

    path = _coordinate_path(initial, ra_delta_h=1.0, dec_delta_deg=10.0)

    assert path == (
        ("initial target", MountPosition(ra=23.5, dec=20.0)),
        ("increase RA", MountPosition(ra=0.5, dec=20.0)),
        ("increase DEC", MountPosition(ra=0.5, dec=30.0)),
        ("decrease RA", MountPosition(ra=23.5, dec=30.0)),
        ("decrease DEC", MountPosition(ra=23.5, dec=20.0)),
    )


def test_coordinate_path_rejects_dec_beyond_pole() -> None:
    with pytest.raises(ValueError):
        _coordinate_path(
            MountPosition(ra=4.0, dec=85.0),
            ra_delta_h=1.0,
            dec_delta_deg=10.0,
        )
