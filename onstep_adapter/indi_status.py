"""Read-only, timestamped mount observations from the shared INDI device."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import time
from typing import Callable

from .indi_config import IndiRuntimeConfig
from .indi_transport import IndiProperty, IndiTransport
from .indi_gu import decode_gu, local_sidereal_hours


@dataclass(frozen=True)
class IndiMountSnapshot:
    raw_status: str | None
    motion_state: str
    pier_side: str | None
    ra_hours: float | None
    dec_deg: float | None
    ha_deg: float | None
    meridian_distance_deg: float | None
    tracking: bool
    slewing: bool
    parked: bool
    at_home: bool
    at_limit: bool
    status_age_ms: float | None
    coordinates_age_ms: float | None
    status_live: bool
    coordinates_live: bool
    time_site_authority: bool
    home_authority: bool
    mechanical_safe: bool
    motion_refused: bool
    blockers: tuple[str, ...]
    status_revision: int = -1
    coordinates_revision: int = -1


class IndiStatusReader:
    """Require post-subscription updates before calling cached data live."""

    def __init__(
        self,
        transport: IndiTransport,
        config: IndiRuntimeConfig,
        *,
        baseline_accepted: Callable[[], bool],
        home_authority: Callable[[], bool] | None = None,
        max_age_seconds: float = 3.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
            raise ValueError("Status age limit must be positive and finite")
        self.transport = transport
        self.config = config
        self.baseline_accepted = baseline_accepted
        self.home_authority = home_authority or (lambda: False)
        self.max_age_seconds = max_age_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._initial_revisions: dict[str, int] = {}

    def read(self) -> IndiMountSnapshot:
        device = self.config.device
        connection = self.transport.get_property(device, "CONNECTION")
        status = self.transport.get_property(device, "OnStep Status")
        coords = self.transport.get_property(device, "EQUATORIAL_EOD_COORD")
        pier = self.transport.get_property(device, "TELESCOPE_PIER_SIDE")
        blockers: list[str] = []
        if not self.transport.is_open or connection is None or connection.values.get("CONNECT") != "On":
            blockers.append("indi_device_disconnected")
        status_live, status_age = self._live("OnStep Status", status)
        coords_live, coords_age = self._live("EQUATORIAL_EOD_COORD", coords)
        pier_live, _ = self._live("TELESCOPE_PIER_SIDE", pier)
        if not status_live:
            blockers.append("onstep_status_not_fresh")
        if not coords_live:
            blockers.append("coordinates_not_fresh")

        raw = status.values.get(":GU# return") if status else None
        try:
            decoded = decode_gu(raw) if raw else {}
        except ValueError:
            decoded = {}
        if status is not None and status.state.lower() == "alert":
            blockers.append("onstep_status_alert")
        if not decoded:
            blockers.append("onstep_status_unavailable")
        if decoded.get("at_limit") or decoded.get("park_failed"):
            blockers.append("onstep_limit_or_park_fault")
        if status is not None and status.values.get("Error", "None") not in {"None", "", "0"}:
            blockers.append("onstep_reported_error")
        pier_side = decoded.get("pier_side")
        if pier_live and pier is not None and pier_side is not None:
            reported_side = (
                "east" if pier.values.get("PIER_EAST") == "On"
                else "west" if pier.values.get("PIER_WEST") == "On"
                else None
            )
            if reported_side is not None and reported_side != pier_side:
                blockers.append("pier_side_conflict")
        if pier_side is None:
            blockers.append("pier_side_unknown")

        ra_hours = self._number(coords, "RA", 0, 24)
        dec_deg = self._number(coords, "DEC", -90, 90)
        if ra_hours is None or dec_deg is None:
            blockers.append("coordinates_invalid")
        authority = self.baseline_accepted()
        if not authority:
            blockers.append("time_site_authority_unestablished")
        ha_deg = None
        if authority and coords_live and ra_hours is not None:
            now = self.clock()
            if now.tzinfo is not None and now.utcoffset() is not None:
                lst = local_sidereal_hours(self.config.observer_lon, now)
                ha_deg = ((lst - ra_hours + 12.0) % 24.0 - 12.0) * 15.0
        if ha_deg is None:
            blockers.append("hour_angle_unavailable")
        if decoded.get("parked") or decoded.get("at_home"):
            blockers.append("mechanical_terminal_state")
        authority_home = self.home_authority()
        if not authority_home:
            blockers.append("home_authority_unestablished")
        return IndiMountSnapshot(
            raw_status=raw,
            motion_state=str(decoded.get("motion_state", "unknown")),
            pier_side=pier_side if isinstance(pier_side, str) else None,
            ra_hours=ra_hours,
            dec_deg=dec_deg,
            ha_deg=ha_deg,
            meridian_distance_deg=abs(ha_deg) if ha_deg is not None else None,
            tracking=bool(decoded.get("tracking")),
            slewing=bool(decoded.get("slewing")),
            parked=bool(decoded.get("parked")),
            at_home=bool(decoded.get("at_home")),
            at_limit=bool(decoded.get("at_limit")),
            status_age_ms=status_age,
            coordinates_age_ms=coords_age,
            status_live=status_live,
            coordinates_live=coords_live,
            time_site_authority=authority,
            home_authority=authority_home,
            mechanical_safe=False,
            motion_refused=True,
            blockers=tuple(dict.fromkeys(blockers)),
            status_revision=status.revision if status is not None else -1,
            coordinates_revision=coords.revision if coords is not None else -1,
        )

    def _live(self, name: str, prop: IndiProperty | None) -> tuple[bool, float | None]:
        if prop is None:
            return False, None
        first = self._initial_revisions.setdefault(name, prop.revision)
        age = max(0.0, time.monotonic() - prop.received_at)
        return prop.revision > first and age <= self.max_age_seconds, age * 1000.0

    @staticmethod
    def _number(prop: IndiProperty | None, name: str, low: float, high: float) -> float | None:
        if prop is None:
            return None
        try:
            value = float(prop.values[name])
        except (KeyError, TypeError, ValueError):
            return None
        return value if math.isfinite(value) and low <= value <= high else None
