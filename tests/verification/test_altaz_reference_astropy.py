"""Dense independent verification of the spherical coordinate calculations.

These tests deliberately use Astropy's frame transformation as the oracle.
They do not prove which mechanical path a GEM firmware selects for a GoTo.
"""

import astropy.units as u
import numpy as np
from astropy.coordinates import AltAz, EarthLocation, HADec, SkyCoord
from astropy.time import Time

from onstep_adapter.indi_axis_motion import IndiAxisMover


LATITUDE_DEG = 50.336
LONGITUDE_DEG = 8.533
OBSTIME = Time("2026-10-20T20:00:00", scale="utc")
LOCATION = EarthLocation(
    lat=LATITUDE_DEG * u.deg, lon=LONGITUDE_DEG * u.deg, height=304 * u.m
)
ALTAZ = AltAz(obstime=OBSTIME, location=LOCATION, pressure=0 * u.hPa)
HADEC = HADec(obstime=OBSTIME, location=LOCATION, pressure=0 * u.hPa)


def mover() -> IndiAxisMover:
    return IndiAxisMover(None, "LX200 OnStep", LATITUDE_DEG, LONGITUDE_DEG, None, None)


def test_equatorial_to_horizontal_dense_grid_against_astropy() -> None:
    subject = mover()
    ha_values = []
    dec_values = []
    alt_values = []
    az_values = []
    for ha_deg in range(-180, 180, 2):
        for dec_deg in range(-88, 89, 2):
            alt, az = subject._horizontal_from_equatorial(ha_deg, dec_deg)
            ha_values.append(ha_deg)
            dec_values.append(dec_deg)
            alt_values.append(alt)
            az_values.append(az)
    reference = SkyCoord(
        ha=np.asarray(ha_values) * u.deg,
        dec=np.asarray(dec_values) * u.deg,
        frame=HADEC,
    ).transform_to(ALTAZ)
    actual = SkyCoord(
        alt=np.asarray(alt_values) * u.deg,
        az=np.asarray(az_values) * u.deg,
        frame=ALTAZ,
    )
    worst_arcsec = float(np.max(reference.separation(actual).arcsec))
    print(f"forward cases={len(actual)} worst_error_arcsec={worst_arcsec:.12g}")
    assert len(actual) == 16_020
    assert worst_arcsec < 0.001


def test_horizontal_to_equatorial_dense_grid_against_astropy() -> None:
    subject = mover()
    lst_hours = 8.0
    alt_values = []
    az_values = []
    ha_values = []
    dec_values = []
    for alt_deg in range(-88, 89, 2):
        for az_deg in range(0, 360, 2):
            ra_hours, dec_deg = subject._equatorial_from_horizontal(
                alt_deg, az_deg, lst_hours
            )
            ha_deg = ((lst_hours - ra_hours + 12.0) % 24.0 - 12.0) * 15.0
            alt_values.append(alt_deg)
            az_values.append(az_deg)
            ha_values.append(ha_deg)
            dec_values.append(dec_deg)
    reference = SkyCoord(
        alt=np.asarray(alt_values) * u.deg,
        az=np.asarray(az_values) * u.deg,
        frame=ALTAZ,
    ).transform_to(HADEC)
    actual = SkyCoord(
        ha=np.asarray(ha_values) * u.deg,
        dec=np.asarray(dec_values) * u.deg,
        frame=HADEC,
    )
    worst_arcsec = float(np.max(reference.separation(actual).arcsec))
    print(f"inverse cases={len(actual)} worst_error_arcsec={worst_arcsec:.12g}")
    assert len(actual) == 16_020
    assert worst_arcsec < 0.001


def test_all_supported_jog_offsets_land_on_the_requested_horizontal_point() -> None:
    subject = mover()
    lst_hours = 8.0
    target_alt_values = []
    target_az_values = []
    reached_ha_values = []
    reached_dec_values = []
    for initial_alt in range(-75, 91, 5):
        for initial_az in range(0, 360, 10):
            for axis in ("ra", "dec"):
                for offset in (-15.0, -5.0, -1.0, 1.0, 5.0, 15.0):
                    target_alt, target_az = initial_alt, initial_az
                    if axis == "dec":
                        target_alt, target_az = subject._fold_altitude_path(
                            target_alt + offset, target_az
                        )
                    else:
                        target_az = (target_az + offset) % 360.0
                    ra_hours, dec_deg = subject._equatorial_from_horizontal(
                        target_alt, target_az, lst_hours
                    )
                    ha_deg = (
                        (lst_hours - ra_hours + 12.0) % 24.0 - 12.0
                    ) * 15.0
                    target_alt_values.append(target_alt)
                    target_az_values.append(target_az)
                    reached_ha_values.append(ha_deg)
                    reached_dec_values.append(dec_deg)
    target = SkyCoord(
        alt=np.asarray(target_alt_values) * u.deg,
        az=np.asarray(target_az_values) * u.deg,
        frame=ALTAZ,
    )
    reached = SkyCoord(
        ha=np.asarray(reached_ha_values) * u.deg,
        dec=np.asarray(reached_dec_values) * u.deg,
        frame=HADEC,
    ).transform_to(ALTAZ)
    worst_arcsec = float(np.max(target.separation(reached).arcsec))
    print(f"jog cases={len(target)} worst_error_arcsec={worst_arcsec:.12g}")
    assert len(target) == 14_688
    assert worst_arcsec < 0.001
