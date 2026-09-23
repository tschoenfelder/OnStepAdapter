from __future__ import annotations

import pytest

from onstep_adapter.location import haversine_distance_m, round_lx200_site_degrees


def test_haversine_distance_reports_zero_for_same_site() -> None:
    assert haversine_distance_m(50.336, 8.533, 50.336, 8.533) == pytest.approx(0.0)


def test_round_lx200_site_degrees_matches_arcminute_readback_precision() -> None:
    assert round_lx200_site_degrees(50.336) == pytest.approx(50 + 20 / 60)
    assert round_lx200_site_degrees(8.533) == pytest.approx(8 + 32 / 60)
    assert round_lx200_site_degrees(-8.533) == pytest.approx(-(8 + 32 / 60))


def test_rounded_location_compares_within_lx200_precision() -> None:
    lat = round_lx200_site_degrees(50.336)
    lon = round_lx200_site_degrees(8.533)

    assert haversine_distance_m(lat, lon, 50 + 20 / 60, 8 + 32 / 60) < 1.0


def test_round_lx200_site_degrees_rejects_non_finite_values() -> None:
    with pytest.raises(ValueError):
        round_lx200_site_degrees(float("nan"))
