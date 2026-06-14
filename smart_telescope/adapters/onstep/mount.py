"""OnStep V4 mount adapter using the LX200 serial protocol."""

from __future__ import annotations

import logging
import json
import math
import os
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

try:
    import serial
except ModuleNotFoundError:
    class _MissingSerialModule:
        class SerialException(Exception):
            pass

        def Serial(self, *args: object, **kwargs: object) -> object:
            raise self.SerialException("pyserial is not installed")

    serial = _MissingSerialModule()  # type: ignore[assignment]

from ...ports.mount import MountPort, MountPosition, MountState
from .firmware_proof import load_firmware_proof, validate_firmware_proof
from .results import (
    AxisMotionResult,
    OnStepMotionCalibration,
    SetParkPositionResult,
    StoredParkPosition,
)
from .safety import (
    OnStepLimitError,
    OnStepLimits,
    OnStepSafetyConfig,
    OnStepSafetyError,
    SafetySeverity,
    SafetyViolation,
)
from .serial_bus import OnStepSerialBus
from .state_store import OnStepStateStore

_log = logging.getLogger(__name__)

_MAX_GVP_ATTEMPTS = 3
_GVP_RETRY_DELAY_S = 0.3
_TRUSTED_TIME_SOURCES = {"gps", "gps_runtime", "ntp", "rtc", "manual", "user_confirmed", "controller_confirmed"}
_ONSTEP_SITE_ARCMIN_READBACK_TOLERANCE_M = 1200.0


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _stored_park_to_dict(record: StoredParkPosition) -> dict[str, object]:
    return {
        "ra": record.ra,
        "dec": record.dec,
        "axis1_deg": record.axis1_deg,
        "axis2_deg": record.axis2_deg,
        "pier_side": record.pier_side,
        "captured_at_utc": record.captured_at_utc,
        "firmware_product": record.firmware_product,
        "firmware_version": record.firmware_version,
        "firmware_date": record.firmware_date,
        "home_authority_state": record.home_authority_state,
        "source": record.source,
        "controller_readback_supported": record.controller_readback_supported,
        "controller_match": record.controller_match,
        "trusted": record.trusted,
        "invalidation_reasons": list(record.invalidation_reasons),
    }
_ONSTEP_MERIDIAN_HALF_WIRE_MINUTE_H = 0.5 / 60.0


class _HorizonProfile:
    def __init__(self, points: list[tuple[float, float]]) -> None:
        self._pts = sorted(points)

    @classmethod
    def load(cls, path: str) -> "_HorizonProfile | None":
        p = Path(path)
        if not p.exists():
            return None
        pts: list[tuple[float, float]] = []
        with p.open() as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or not line[0].isdigit():
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    pts.append((float(parts[0]), float(parts[1])))
        return cls(pts)

    def min_alt_at(self, az_deg: float) -> float:
        if not self._pts:
            return 0.0
        az = az_deg % 360.0
        pts = self._pts
        for i in range(len(pts) - 1):
            az0, alt0 = pts[i]
            az1, alt1 = pts[i + 1]
            if az0 <= az <= az1:
                if az1 == az0:
                    return alt0
                return alt0 + (az - az0) / (az1 - az0) * (alt1 - alt0)
        az0, alt0 = pts[-1]
        az1, alt1 = pts[0][0] + 360.0, pts[0][1]
        az_w = az if az >= az0 else az + 360.0
        return alt0 + (az_w - az0) / (az1 - az0) * (alt1 - alt0)


def _format_ra(ra: float) -> str:
    ra = ra % 24
    h = int(ra)
    rem = (ra - h) * 60
    m = int(rem)
    s = int((rem - m) * 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _format_dec(dec: float) -> str:
    sign = "+" if dec >= 0 else "-"
    dec = abs(dec)
    d = int(dec)
    rem = (dec - d) * 60
    m = int(rem)
    s = int((rem - m) * 60)
    return f"{sign}{d:02d}*{m:02d}:{s:02d}"


def _parse_ra(s: str) -> float:
    parts = s.strip().split(":")
    return float(parts[0]) + float(parts[1]) / 60 + float(parts[2]) / 3600


def _parse_dec(s: str) -> float:
    s = s.strip()
    sign = -1 if s.startswith("-") else 1
    s = s.lstrip("+-").replace("*", ":").replace("'", ":")
    parts = s.split(":")
    return sign * (float(parts[0]) + float(parts[1]) / 60 + float(parts[2]) / 3600)


def _parse_degrees(s: str) -> float:
    s = s.strip().rstrip("#")
    sign = -1 if s.startswith("-") else 1
    s = s.lstrip("+-").replace("*", ":").replace("'", ":")
    parts = [p for p in s.split(":") if p]
    if not parts:
        raise ValueError("empty degree value")
    deg = float(parts[0])
    if len(parts) > 1:
        deg += float(parts[1]) / 60.0
    if len(parts) > 2:
        deg += float(parts[2]) / 3600.0
    return sign * deg


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_m = 6_371_000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius_m * math.asin(math.sqrt(a))


def _parse_onstep_local_datetime(date_reply: str, time_reply: str) -> datetime:
    month_s, day_s, year_s = date_reply.strip().rstrip("#").split("/")
    hour_s, minute_s, second_s = time_reply.strip().rstrip("#").split(":")
    year = int(year_s)
    year += 2000 if year < 70 else 1900
    return datetime(
        year,
        int(month_s),
        int(day_s),
        int(hour_s),
        int(minute_s),
        int(float(second_s)),
    )


def _format_limit_degrees(value: float, signed: bool) -> str:
    rounded = int(round(value))
    if signed:
        sign = "+" if rounded >= 0 else "-"
        return f"{sign}{abs(rounded):02d}"
    return f"{max(0, rounded):02d}"


def _format_site_degrees(value: float, width: int) -> str:
    sign = "+" if value >= 0 else "-"
    value = abs(value)
    deg = int(value)
    minutes = int(round((value - deg) * 60.0))
    if minutes >= 60:
        deg += 1
        minutes -= 60
    return f"{sign}{deg:0{width}d}*{minutes:02d}"


def _format_onstep_utc_offset(local_dt: datetime) -> str:
    """Format hours added to local time to obtain UTC, as required by :SG#."""
    offset = local_dt.utcoffset()
    if offset is None:
        raise ValueError("local datetime has no UTC offset")
    protocol_hours = -int(round(offset.total_seconds() / 3600.0))
    sign = "+" if protocol_hours >= 0 else "-"
    return f"{sign}{abs(protocol_hours):02d}"


_GU_CORE_FLAG_LABELS = {
    "N": "not_slewing",
    "H": "at_home",
    "P": "parked",
    "p": "not_parked",
    "F": "park_failed",
    "I": "park_in_progress",
    "R": "pec_recorded",
    "G": "guiding",
    "S": "gps_pps_synced",
}


def _decode_onstep_status(status: str) -> dict[str, object]:
    raw = status.strip().rstrip("#")
    pipe_format = "|" in raw
    tokens = [part for part in raw.split("|") if part] if pipe_format else []
    chars = [ch for ch in raw if ch != "|"]
    flags = set(chars)
    token_flags = set(tokens)
    core = {flag: _GU_CORE_FLAG_LABELS[flag] for flag in flags if flag in _GU_CORE_FLAG_LABELS}
    unknown = sorted(flag for flag in flags if flag not in _GU_CORE_FLAG_LABELS and not flag.isdigit())
    parked = "P" in flags
    park_in_progress = "I" in flags
    park_failed = "F" in flags
    if pipe_format:
        # Legacy/test token format used T/S as tracking/slewing tokens.
        tracking = "T" in token_flags
        goto_active = "S" in token_flags
        pier_side = (
            "east"
            if "E" in token_flags and "W" not in token_flags
            else "west"
            if "W" in token_flags and "E" not in token_flags
            else None
        )
    else:
        # Official compact :GU# format:
        # n = not tracking, N = no goto, E = GEM, T/W = east/west pier.
        tracking = "n" not in flags
        goto_active = "N" not in flags
        pier_side = "east" if "T" in flags else "west" if "W" in flags else None
    not_slewing = not goto_active
    slewing = goto_active
    at_home = "H" in flags
    at_limit = "l" in flags or "L" in flags
    if parked:
        motion_state = "parked"
    elif park_in_progress:
        motion_state = "parking"
    elif park_failed:
        motion_state = "park_failed"
    elif at_limit:
        motion_state = "at_limit"
    elif slewing:
        motion_state = "slewing"
    elif tracking:
        motion_state = "tracking"
    elif at_home:
        motion_state = "home"
    elif "p" in flags or "n" in flags:
        motion_state = "unparked"
    else:
        motion_state = "unknown"
    return {
        "raw": raw,
        "tokens": tokens,
        "core_flags": core,
        "unknown_flags": unknown,
        "motion_state": motion_state,
        "parking_state": "failed" if park_failed else "parking" if park_in_progress else "parked" if parked else "not_parked" if ("p" in flags or "n" in flags) else "unknown",
        "parked": parked,
        "not_parked": "p" in flags or ("n" in flags and not parked),
        "park_in_progress": park_in_progress,
        "park_failed": park_failed,
        "at_home": at_home,
        "not_slewing": not_slewing,
        "slewing": slewing,
        "tracking": tracking,
        "goto_active": goto_active,
        "at_limit": at_limit,
        "pec_recorded": "R" in flags,
        "guiding": "G" in flags,
        "gps_pps_synced": "S" in flags if not pipe_format else False,
        "auto_meridian_flip": "a" in flags,
        "mount_type": (
            "gem"
            if "E" in flags and not pipe_format
            else "fork"
            if "K" in flags and not pipe_format
            else "altaz"
            if "A" in flags and not pipe_format
            else "altalt"
            if "L" in flags and not pipe_format
            else None
        ),
        "pier_side": pier_side,
        "pier_east": pier_side == "east",
        "pier_west": pier_side == "west",
        "flags": sorted(flags),
    }


def _instrument_to_mount_axes(
    axis1_deg: float,
    axis2_deg: float,
    observer_lat: float,
) -> dict[str, object]:
    """Apply the OnStepX GEM instrumentToMount pier-side transform."""
    mount_axis1 = float(axis1_deg)
    mount_axis2 = float(axis2_deg)
    if observer_lat >= 0.0:
        if mount_axis2 > 90.0:
            pier_side = "west"
            mount_axis1 -= 180.0
            mount_axis2 = 180.0 - mount_axis2
        else:
            pier_side = "east"
    elif mount_axis2 < -90.0:
        pier_side = "west"
        mount_axis1 -= 180.0
        mount_axis2 = -180.0 - mount_axis2
    else:
        pier_side = "east"
    return {
        "axis1_deg": mount_axis1,
        "axis2_deg": mount_axis2,
        "pier_side": pier_side,
    }


def _counterweight_safety_state(
    *,
    ha_hours: float | None,
    pier_side: str | None,
    east_limit_h: float,
    west_limit_h: float,
    warning_margin_deg: float,
    preflip_pier_side: str | None = None,
    terminal_state: bool = False,
) -> dict[str, object]:
    """Classify wrong-side meridian travel without treating coordinates as sensors."""
    if terminal_state:
        return {
            "counterweight_state": "normal",
            "counterweight_up": False,
            "operational_limit_margin_deg": None,
            "limit_warning": False,
            "hard_limit_reached": False,
            "applicable": False,
        }
    if ha_hours is None or pier_side not in {"east", "west"}:
        return {
            "counterweight_state": "unknown",
            "counterweight_up": None,
            "operational_limit_margin_deg": None,
            "limit_warning": False,
            "hard_limit_reached": False,
            "applicable": True,
        }

    # On this GEM workflow the pre-flip side is west for positive HA and east
    # for negative HA. Continuing on that side is the counterweight-up case.
    positive_ha_preflip_side = (
        preflip_pier_side
        if preflip_pier_side in {"east", "west"}
        else "west"
    )
    if pier_side == positive_ha_preflip_side and ha_hours >= 0.0:
        margin_deg = (west_limit_h - ha_hours) * 15.0
        warning_boundary_h = west_limit_h - warning_margin_deg / 15.0
        hard = ha_hours >= west_limit_h
        warning = not hard and ha_hours >= warning_boundary_h
        state = "hard_limit_reached" if hard else "approaching_limit" if warning else "counterweight_up_allowed"
        return {
            "counterweight_state": state,
            "counterweight_up": True,
            "operational_limit_margin_deg": round(margin_deg, 4),
            "limit_warning": warning,
            "hard_limit_reached": hard,
            "applicable": True,
        }
    if preflip_pier_side is None and pier_side == "east" and ha_hours <= 0.0:
        margin_deg = (ha_hours - east_limit_h) * 15.0
        warning_boundary_h = east_limit_h + warning_margin_deg / 15.0
        hard = ha_hours <= east_limit_h
        warning = not hard and ha_hours <= warning_boundary_h
        state = "hard_limit_reached" if hard else "approaching_limit" if warning else "counterweight_up_allowed"
        return {
            "counterweight_state": state,
            "counterweight_up": True,
            "operational_limit_margin_deg": round(margin_deg, 4),
            "limit_warning": warning,
            "hard_limit_reached": hard,
            "applicable": True,
        }
    return {
        "counterweight_state": "normal",
        "counterweight_up": False,
        "operational_limit_margin_deg": None,
        "limit_warning": False,
        "hard_limit_reached": False,
        "applicable": True,
    }


def _evaluate_onstep_operational_protection(
    *,
    state: str,
    at_home: bool,
    pier_side: str | None,
    ha_hours: float | None,
    limits: dict[str, object],
    flip_boundary_h: float,
    west_stop_h: float,
) -> dict[str, object]:
    """Select the OnStep limit branch relevant to current live motion."""
    moving = state in {"tracking", "slewing"}
    if state == "parked" or at_home:
        return {
            "applicable": False,
            "status": "not_applicable_mechanical_terminal_state",
            "operation": state,
            "active_branch": None,
            "protected_to_requested_boundary": None,
            "reason": "HOME/PARK safety is established by mechanical status and route completion.",
        }
    if not moving:
        return {
            "applicable": False,
            "status": "not_applicable_not_moving",
            "operation": state,
            "active_branch": None,
            "protected_to_requested_boundary": None,
            "reason": "Firmware motion limits become operationally relevant during slew or tracking.",
        }
    if pier_side not in {"east", "west"} or ha_hours is None:
        return {
            "applicable": True,
            "status": "unknown",
            "operation": state,
            "active_branch": None,
            "protected_to_requested_boundary": False,
            "reason": "Pier side and current hour angle are required to select the active OnStep branch.",
        }

    east = limits.get("meridian_east") if isinstance(limits, dict) else None
    west = limits.get("meridian_west") if isinstance(limits, dict) else None
    east_h = east.get("hours") if isinstance(east, dict) else None
    west_h = west.get("hours") if isinstance(west, dict) else None
    axis1_max_deg = limits.get("axis1_max_deg") if isinstance(limits, dict) else None

    dual_pier = limits.get("dual_pier_west_ha_stop") if isinstance(limits, dict) else None
    dual_pier_enabled = bool(
        isinstance(dual_pier, dict) and dual_pier.get("enabled") is True
    )

    if pier_side == "west" and ha_hours >= 0.0:
        active_branch = "preflip_pier_west_west_meridian"
        threshold_h = float(west_h) if isinstance(west_h, (int, float)) else None
        protected = bool(
            threshold_h is not None
            and flip_boundary_h - _ONSTEP_MERIDIAN_HALF_WIRE_MINUTE_H
            <= threshold_h
            <= west_stop_h + _ONSTEP_MERIDIAN_HALF_WIRE_MINUTE_H
        )
        reason = (
            "The west-past-meridian branch is compatible with the requested pre-flip interval."
            if protected
            else "The west-past-meridian threshold is missing or outside the requested pre-flip interval."
        )
    elif pier_side == "east" and ha_hours < 0.0:
        active_branch = "pier_east_east_meridian"
        threshold_h = -float(east_h) if isinstance(east_h, (int, float)) else None
        protected = threshold_h is not None
        reason = (
            "The east-past-meridian branch is readable for the negative-HA pier-east path."
            if protected
            else "The east-past-meridian threshold is unavailable."
        )
    elif pier_side == "east" and ha_hours >= 0.0 and dual_pier_enabled:
        active_branch = "postflip_pier_east_dual_pier_west_ha_stop"
        threshold_h = float(west_h) if isinstance(west_h, (int, float)) else None
        protected = bool(
            threshold_h is not None
            and flip_boundary_h - _ONSTEP_MERIDIAN_HALF_WIRE_MINUTE_H
            <= threshold_h
            <= west_stop_h + _ONSTEP_MERIDIAN_HALF_WIRE_MINUTE_H
        )
        reason = (
            "The SmartTScope dual-pier firmware extension applies the west HA stop on pier east."
            if protected
            else "The dual-pier extension is enabled but its west HA threshold does not match policy."
        )
    else:
        active_branch = "pier_side_axis1_max"
        threshold_h = float(axis1_max_deg) / 15.0 if isinstance(axis1_max_deg, (int, float)) else None
        protected = bool(threshold_h is not None and threshold_h <= west_stop_h)
        reason = (
            "Axis 1 maximum protects the requested west boundary."
            if protected
            else "Axis 1 maximum is beyond the requested west boundary."
        )

    return {
        "applicable": True,
        "status": "protected" if protected else "unprotected",
        "operation": state,
        "pier_side": pier_side,
        "ha_hours": round(ha_hours, 4),
        "active_branch": active_branch,
        "active_threshold_h": threshold_h,
        "comparison_tolerance_h": _ONSTEP_MERIDIAN_HALF_WIRE_MINUTE_H,
        "flip_boundary_h": flip_boundary_h,
        "requested_west_stop_h": west_stop_h,
        "protected_to_requested_boundary": protected,
        "reason": reason,
    }


def _evaluate_onstep_meridian_path_coverage(
    *,
    limits: dict[str, object],
    flip_boundary_h: float,
    west_stop_h: float,
) -> dict[str, object]:
    """Evaluate firmware coverage for the intended pre-flip/flip/post-flip path."""
    preflip_sample_h = max(0.0, flip_boundary_h)
    preflip = _evaluate_onstep_operational_protection(
        state="tracking",
        at_home=False,
        pier_side="west",
        ha_hours=preflip_sample_h,
        limits=limits,
        flip_boundary_h=flip_boundary_h,
        west_stop_h=west_stop_h,
    )
    postflip = _evaluate_onstep_operational_protection(
        state="tracking",
        at_home=False,
        pier_side="east",
        ha_hours=flip_boundary_h,
        limits=limits,
        flip_boundary_h=flip_boundary_h,
        west_stop_h=west_stop_h,
    )
    preflip_protected = preflip.get("protected_to_requested_boundary") is True
    postflip_protected = postflip.get("protected_to_requested_boundary") is True
    app_policy_valid = 0.0 <= flip_boundary_h < west_stop_h
    blockers: list[str] = []
    if not app_policy_valid:
        blockers.append("invalid_flip_stop_order")
    if not preflip_protected:
        blockers.append("preflip_pier_west_meridian_limit_not_compatible")
    if not postflip_protected:
        blockers.append("postflip_pier_east_dual_pier_stop_not_available")
    return {
        "path": [
            {
                "segment": "preflip_pier_west_tracking",
                "range_h": [0.0, flip_boundary_h],
                "adapter_action_at_end": "request_meridian_flip",
                "firmware": preflip,
            },
            {
                "segment": "meridian_flip_handoff",
                "at_h": flip_boundary_h,
                "required_transition": "pier_west_to_pier_east",
                "firmware_protection_during_failed_handoff": preflip_protected,
            },
            {
                "segment": "postflip_pier_east_tracking",
                "range_h": [flip_boundary_h, west_stop_h],
                "adapter_action_at_end": "stop_tracking",
                "firmware": postflip,
            },
        ],
        "app_policy_valid": app_policy_valid,
        "preflip_firmware_protected": preflip_protected,
        "postflip_firmware_protected": postflip_protected,
        "full_path_firmware_protected": bool(
            app_policy_valid and preflip_protected and postflip_protected
        ),
        "unattended_tracking_allowed": False,
        "blockers": blockers,
    }


def _default_safety_config() -> OnStepSafetyConfig:
    try:
        from ... import config

        factory = getattr(config, "build_onstep_safety_config", None)
        if callable(factory):
            return factory()
    except (ImportError, AttributeError):
        pass
    return OnStepSafetyConfig(
        observer_lat=0.0,
        observer_lon=0.0,
        min_alt_deg=0.0,
        max_alt_deg=90.0,
        ha_east_limit_h=-6.0,
        ha_west_limit_h=5.0 / 15.0,
        require_home_confirmation=True,
        time_trust_source="unconfigured",
        require_onstep_limits=True,
    )


def _julian_date(now: datetime) -> float:
    unix_days = now.timestamp() / 86400.0
    return 2440587.5 + unix_days


def _lst_hours(observer_lon: float, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    jd = _julian_date(now)
    d = jd - 2451545.0
    gmst = 18.697374558 + 24.06570982441908 * d
    return (gmst + observer_lon / 15.0) % 24.0


def _compute_altaz_stdlib(
    ra_hours: float,
    dec_deg: float,
    observer_lat: float,
    observer_lon: float,
) -> tuple[float, float]:
    return _compute_altaz_stdlib_at(
        ra_hours,
        dec_deg,
        observer_lat,
        observer_lon,
        datetime.now(timezone.utc),
    )


def _compute_altaz_stdlib_at(
    ra_hours: float,
    dec_deg: float,
    observer_lat: float,
    observer_lon: float,
    now: datetime,
) -> tuple[float, float]:
    ha_hours = ((_lst_hours(observer_lon, now) - ra_hours + 12.0) % 24.0) - 12.0
    lat = math.radians(observer_lat)
    dec = math.radians(dec_deg)
    ha = math.radians(ha_hours * 15.0)
    sin_alt = math.sin(lat) * math.sin(dec) + math.cos(lat) * math.cos(dec) * math.cos(ha)
    alt = math.asin(max(-1.0, min(1.0, sin_alt)))
    cos_alt = max(1e-9, math.cos(alt))
    sin_az = -math.sin(ha) * math.cos(dec) / cos_alt
    cos_az = (math.sin(dec) - math.sin(alt) * math.sin(lat)) / (cos_alt * math.cos(lat))
    az = math.atan2(sin_az, cos_az)
    return math.degrees(alt), (math.degrees(az) + 360.0) % 360.0


class OnStepMount(MountPort):
    def __init__(
        self,
        port: str,
        baud_rate: int = 9600,
        timeout: float = 2.0,
        safety_config: OnStepSafetyConfig | None = None,
        serial_bus: OnStepSerialBus | None = None,
        motion_calibration: OnStepMotionCalibration | None = None,
    ) -> None:
        self._port = port
        self._baud_rate = baud_rate
        self._timeout = timeout
        self._bus = serial_bus or OnStepSerialBus()
        self._safety_config = safety_config or _default_safety_config()
        self._motion_calibration = motion_calibration
        self._axis_motion_lock = threading.Lock()
        self._horizon: _HorizonProfile | None = None
        self._onstep_limits = OnStepLimits()
        self._onstep_extended_limits: dict[str, object] = {}
        self._safety_lock: SafetyViolation | None = None
        self._home_confirmed = not self._safety_config.require_home_confirmation
        self._park_pose_confirmed = False
        self._mechanical_trust_invalidations: list[str] = []
        self._last_status = ""
        self._last_decoded_status: dict[str, object] = {}
        self._last_clock_check: dict[str, object] | None = None
        self._last_sidereal_check: dict[str, object] | None = None
        self._last_system_clock_check: dict[str, object] | None = None
        self._last_ra: float | None = None
        self._last_dec: float | None = None
        self._at_mechanical_home = False
        self._meridian_initial_pier_side: str | None = None
        self._meridian_postflip_pier_side: str | None = None
        self._meridian_flip_completed = False
        self._mechanical_axis_position: dict[str, float] | None = None
        self._mechanical_calibration = self._load_mechanical_calibration()
        state_path = self._safety_config.state_file
        if not state_path:
            state_path = str(Path.home() / ".SmartTScope" / "onstep_last_state.json")
        self._state_store = OnStepStateStore(
            state_path,
            min_write_interval_s=self._safety_config.state_write_interval_s,
        )
        self._persisted_state_at_startup = self._state_store.load()
        self._inspect_persisted_startup_state()
        if self._home_confirmed:
            self._mechanical_trust_invalidations = [
                reason
                for reason in self._mechanical_trust_invalidations
                if reason != "no_persisted_startup_state"
            ]
        self._restore_mechanical_pose_from_calibration()
        if self._safety_config.horizon_path:
            try:
                self._horizon = _HorizonProfile.load(self._safety_config.horizon_path)
            except Exception as exc:
                _log.warning("OnStepMount: horizon profile could not be loaded: %s", exc)

    @property
    def _serial(self) -> serial.Serial | None:  # type: ignore[override]
        return self._bus._serial

    @_serial.setter
    def _serial(self, value: serial.Serial | None) -> None:
        self._bus._serial = value

    @property
    def serial_bus(self) -> OnStepSerialBus:
        return self._bus

    @property
    def safety_config(self) -> OnStepSafetyConfig:
        return self._safety_config

    @property
    def safety_lock(self) -> SafetyViolation | None:
        return self._safety_lock

    @property
    def onstep_limits(self) -> OnStepLimits:
        return self._onstep_limits

    @property
    def last_decoded_status(self) -> dict[str, object]:
        return dict(self._last_decoded_status)

    def safety_snapshot(self) -> dict[str, object]:
        lock = self._safety_lock.to_dict() if self._safety_lock is not None else None
        cfg = self._safety_config
        time_readiness = self._time_readiness()
        location_readiness = self._location_readiness()
        limit_readiness = self._limit_readiness()
        mechanical_limits = self._mechanical_limits_readiness()
        mechanical_position_authority = self._mechanical_position_authority()
        mechanical_readiness = self._mechanical_readiness(mechanical_limits, mechanical_position_authority)
        firmware_protection = self._onstep_firmware_protection()
        motion_authority = self._motion_authority(
            time_readiness,
            location_readiness,
            limit_readiness,
            mechanical_readiness,
        )
        return {
            "adapter": "onstep",
            "adapter_contract_ok": True,
            "time_location_ready": bool(time_readiness["ready"] and location_readiness["ready"]),
            "limits_ready": bool(limit_readiness["ready"]),
            "mechanical_ready": bool(mechanical_readiness["ready"]),
            "recovery_ready": bool(motion_authority["recovery_pulse"]),
            "astronomical_motion_ready": bool(motion_authority["goto"] and motion_authority["tracking"]),
            "astronomical_ready": bool(motion_authority["goto"] and motion_authority["tracking"]),
            "mechanical_origin": "home",
            "movement_reference_confirmed": self._home_confirmed,
            "home_confirmed": self._home_confirmed,
            "home_reference_confirmed": self._home_confirmed,
            "park_pose_confirmed": self._park_pose_confirmed,
            "mechanical_position_authority": mechanical_position_authority,
            "position_authority": mechanical_position_authority,
            "mechanical_axis_position": self._mechanical_axis_position or "unknown",
            "mechanical_limits": mechanical_limits,
            "mechanical_readiness": mechanical_readiness,
            "safety_locked": lock is not None,
            "safety_lock": lock,
            "last_onstep_status": self._last_status or None,
            "decoded_onstep_status": self._last_decoded_status or None,
            "system_clock": self._last_system_clock_check,
            "onstep_clock": self._last_clock_check,
            "onstep_sidereal_time": self._last_sidereal_check,
            "observer": {
                "lat": cfg.observer_lat,
                "lon": cfg.observer_lon,
                "alt_m": cfg.observer_alt_m,
                "time_offset_s": round(cfg.time_offset_s, 3),
            },
            "persisted_state_file": str(self._state_store.path),
            "persisted_state_at_startup": self._persisted_state_at_startup,
            "onstep_limits": {
                "horizon_deg": self._onstep_limits.horizon_deg,
                "overhead_deg": self._onstep_limits.overhead_deg,
            },
            "configured_limits": {
                "min_alt_deg": cfg.min_alt_deg,
                "max_alt_deg": cfg.max_alt_deg,
                "safe_overhead_corridors": [
                    {
                        "az_center_deg": corridor.az_center_deg,
                        "az_half_width_deg": corridor.az_half_width_deg,
                        "max_alt_deg": corridor.max_alt_deg,
                    }
                    for corridor in cfg.safe_overhead_corridors
                ],
                "ha_east_limit_h": cfg.ha_east_limit_h,
                "ha_west_limit_h": cfg.ha_west_limit_h,
                "dec_min_deg": cfg.dec_min_deg,
                "dec_max_deg": cfg.dec_max_deg,
                "horizon_path": cfg.horizon_path or None,
                "require_home_confirmation": cfg.require_home_confirmation,
                "meridian_margin_deg": cfg.meridian_margin_deg,
                "sync_limits_to_onstep": cfg.sync_limits_to_onstep,
                "configured_horizon_limit_deg": cfg.configured_horizon_limit_deg,
                "configured_overhead_limit_deg": cfg.configured_overhead_limit_deg,
                "clock_warning_threshold_s": cfg.clock_warning_threshold_s,
            },
            "effective_limits": {
                "min_alt_base_deg": max(
                    value for value in (
                        cfg.min_alt_deg,
                        self._onstep_limits.horizon_deg,
                    )
                    if value is not None
                ),
                "max_alt_deg": self._effective_max_altitude(),
                "max_alt_policy": "az_corridor_aware",
            },
            "horizon_profile_loaded": self._horizon is not None,
            "time_readiness": time_readiness,
            "location_readiness": location_readiness,
            "limit_readiness": limit_readiness,
            "tracking_runtime_safety": self.current_tracking_safety(margin_deg=0.0),
            "onstep_firmware_protection": firmware_protection,
            "unattended_tracking_allowed": bool(firmware_protection["unattended_tracking_allowed"]),
            "supervised_tracking_allowed": bool(motion_authority["tracking"]),
            "motion_authority": motion_authority,
        }

    def _inspect_persisted_startup_state(self) -> None:
        state = self._persisted_state_at_startup
        if not isinstance(state, dict):
            self._mechanical_trust_invalidations.append("no_persisted_startup_state")
            return
        unsafe_flags = []
        for key in ("tracking", "slewing", "at_limit", "safety_locked"):
            if state.get(key):
                unsafe_flags.append(f"persisted_{key}")
        if state.get("parked") is False:
            unsafe_flags.append("persisted_unparked")
        if state.get("safety_reason") and state.get("safety_reason") not in {"system_clock_invalid", "onstep_clock_invalid"}:
            unsafe_flags.append(f"persisted_safety_reason:{state.get('safety_reason')}")
        self._mechanical_trust_invalidations.extend(unsafe_flags)

    def _invalidate_mechanical_trust(self, reason: str) -> None:
        if reason not in self._mechanical_trust_invalidations:
            self._mechanical_trust_invalidations.append(reason)
        self._home_confirmed = False
        self._park_pose_confirmed = False

    def _position_authority(self) -> dict[str, object]:
        return self._mechanical_position_authority()

    def _mechanical_position_authority(self) -> dict[str, object]:
        reasons = list(self._mechanical_trust_invalidations)
        if self._safety_lock is not None:
            if self._safety_lock.reason not in {"system_clock_invalid", "onstep_clock_invalid"}:
                reasons.append(f"safety_locked:{self._safety_lock.reason}")
        decoded = self._last_decoded_status or {}
        if decoded.get("park_failed"):
            reasons.append("onstep_park_failed")
        if decoded.get("at_limit"):
            reasons.append("onstep_at_limit")
        if not self._home_confirmed:
            reasons.append("home_reference_not_confirmed")
        if decoded.get("parked") and not self._park_pose_confirmed:
            reasons.append("park_pose_not_confirmed")
        if any(reason in {"onstep_park_failed", "onstep_at_limit"} or reason.startswith("safety_locked:") for reason in reasons):
            state = "fault"
        elif not reasons:
            state = "trusted"
        elif self._park_pose_confirmed and not self._home_confirmed:
            state = "park_confirmed"
        else:
            state = "unknown"
        return {
            "state": state,
            "home_reference_confirmed": self._home_confirmed,
            "park_pose_confirmed": self._park_pose_confirmed,
            "mechanical_origin": "home",
            "reasons": reasons,
            "description": "home=mechanical reference; park=mirror-safe upward storage pose",
        }

    def _mechanical_limits_readiness(self) -> dict[str, object]:
        cfg = self._safety_config
        configured = {
            "axis1_min_deg": cfg.mechanical_axis1_min_deg,
            "axis1_max_deg": cfg.mechanical_axis1_max_deg,
            "axis2_min_deg": cfg.mechanical_axis2_min_deg,
            "axis2_max_deg": cfg.mechanical_axis2_max_deg,
        }
        missing = [name for name, value in configured.items() if value is None]
        reasons = ["mechanical_axis_limits_missing"] if missing else []
        return {
            "ready": not reasons,
            "origin": "home",
            "configured": configured,
            "missing": missing,
            "reasons": reasons,
            "required_action": None if not reasons else "Configure HOME-relative mechanical axis limits before mechanical/manual movement readiness.",
        }

    def _mechanical_readiness(
        self,
        mechanical_limits: dict[str, object],
        position_authority: dict[str, object],
    ) -> dict[str, object]:
        decoded = self._last_decoded_status or {}
        status_usable = bool(decoded.get("raw") or self._last_status)
        reasons: list[str] = []
        if not status_usable:
            reasons.append("onstep_status_unavailable")
        if position_authority.get("state") == "fault":
            reasons.append("mechanical_position_fault")
        if not (self._home_confirmed or self._park_pose_confirmed):
            reasons.append("home_or_park_not_confirmed")
        if not mechanical_limits.get("ready"):
            reasons.extend(str(reason) for reason in mechanical_limits.get("reasons") or [])
        ready = not reasons
        return {
            "ready": ready,
            "status_usable": status_usable,
            "origin": "home",
            "position_authority": position_authority,
            "limits_ready": bool(mechanical_limits.get("ready")),
            "reasons": reasons,
            "required_action": None if ready else "Confirm HOME/PARK and configure HOME-relative mechanical limits.",
        }

    def _load_mechanical_calibration(self) -> dict[str, object] | None:
        path = self._mechanical_calibration_path()
        if path is None or not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except Exception as exc:
            _log.warning("OnStepMount: mechanical calibration read failed: %s", exc)
            return None

    def _mechanical_calibration_path(self) -> Path | None:
        raw = self._safety_config.mechanical_calibration_file
        if not raw:
            return None
        return Path(raw).expanduser()

    def _store_mechanical_calibration(self, payload: dict[str, object]) -> None:
        path = self._mechanical_calibration_path()
        if path is None:
            raise RuntimeError("mechanical calibration file is not configured")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        self._mechanical_calibration = payload

    def _restore_mechanical_pose_from_calibration(self) -> None:
        data = self._mechanical_calibration
        if not isinstance(data, dict):
            return
        park = data.get("park_pose")
        if isinstance(park, dict) and self._park_pose_confirmed:
            self._mechanical_axis_position = {
                "axis1_deg": float(park.get("axis1_deg", 0.0)),
                "axis2_deg": float(park.get("axis2_deg", 0.0)),
            }

    def _time_readiness(self) -> dict[str, object]:
        system_clock = self._last_system_clock_check or self.read_system_clock_sanity()
        onstep_clock = self._last_clock_check or self.read_onstep_clock()
        sidereal = self._last_sidereal_check or self.read_onstep_sidereal_consistency()
        cfg = self._safety_config
        trust_source = (cfg.time_trust_source or "raspberry_plausible").lower()
        trusted = trust_source in _TRUSTED_TIME_SOURCES or abs(cfg.time_offset_s) > 0.001
        system_valid = bool(system_clock.get("valid"))
        if not system_valid:
            raspberry_state = "invalid"
        elif trusted:
            raspberry_state = "trusted"
        else:
            raspberry_state = "plausible_not_trusted"
        onstep_warning = bool(onstep_clock.get("warning")) or not bool(onstep_clock.get("available", True))
        date_reply = str(onstep_clock.get("onstep_date_reply") or "")
        fresh_boot = date_reply in {"01/01/-12", "01/01/00", "01/01/01"} or date_reply.endswith("/-12")
        reasons: list[str] = []
        if raspberry_state == "invalid":
            reasons.append("raspberry_time_invalid")
        elif raspberry_state == "plausible_not_trusted":
            reasons.append("raspberry_time_plausible_not_trusted")
        if fresh_boot:
            reasons.append("onstep_fresh_boot_clock")
        if onstep_warning:
            reasons.append("onstep_clock_invalid")
        if not sidereal.get("ok"):
            reasons.append(str(sidereal.get("reason") or "onstep_sidereal_time_mismatch"))
        ready = system_valid and trusted and not onstep_warning and bool(sidereal.get("ok"))
        return {
            "ready": ready,
            "raspberry": {
                "state": raspberry_state,
                "trust_source": trust_source,
                "valid": system_valid,
                "detail": system_clock,
            },
            "onstep": {
                "state": "invalid" if onstep_warning else "valid",
                "fresh_boot_indicator": fresh_boot,
                "detail": onstep_clock,
            },
            "sidereal": sidereal,
            "trusted_for_astronomy": ready,
            "reasons": reasons,
            "required_action": None if ready else "Confirm Raspberry time via GPS/NTP/RTC/user, then sync/check OnStep time before astronomical motion.",
        }

    def _location_readiness(self) -> dict[str, object]:
        cfg = self._safety_config
        site = self.read_onstep_site()
        reasons: list[str] = []
        ready = True
        delta_location_m = None
        delta_alt_m = None
        onstep_lat = site.get("lat")
        onstep_lon = site.get("lon")
        if not site.get("available"):
            ready = False
            reasons.append("onstep_location_unavailable")
        elif isinstance(onstep_lat, (int, float)) and isinstance(onstep_lon, (int, float)):
            delta_location_m = _distance_m(cfg.observer_lat, cfg.observer_lon, float(onstep_lat), float(onstep_lon))
            effective_location_threshold_m = max(
                cfg.location_warning_threshold_m,
                _ONSTEP_SITE_ARCMIN_READBACK_TOLERANCE_M,
            )
            if delta_location_m > effective_location_threshold_m:
                ready = False
                reasons.append("onstep_location_mismatch")
            elif delta_location_m > cfg.location_warning_threshold_m:
                reasons.append("onstep_location_readback_precision_limited")
            if (cfg.observer_lat >= 0) != (float(onstep_lat) >= 0):
                ready = False
                reasons.append("wrong_hemisphere_warning")
        else:
            ready = False
            reasons.append("onstep_location_not_set")
        if site.get("alt_m") is not None:
            delta_alt_m = abs(float(site["alt_m"]) - cfg.observer_alt_m)
            if delta_alt_m > cfg.altitude_warning_threshold_m:
                reasons.append("onstep_altitude_mismatch")
        return {
            "ready": ready,
            "active_observer": {
                "lat": cfg.observer_lat,
                "lon": cfg.observer_lon,
                "alt_m": cfg.observer_alt_m,
                "time_offset_s": round(cfg.time_offset_s, 3),
            },
            "onstep_site": site,
            "deltas": {
                "location_m": round(delta_location_m, 1) if delta_location_m is not None else None,
                "altitude_m": round(delta_alt_m, 1) if delta_alt_m is not None else None,
            },
            "thresholds": {
                "configured_location_m": cfg.location_warning_threshold_m,
                "effective_onstep_readback_location_m": _ONSTEP_SITE_ARCMIN_READBACK_TOLERANCE_M,
                "altitude_m": cfg.altitude_warning_threshold_m,
            },
            "reasons": reasons,
            "required_action": None if ready else "Confirm/apply site location to OnStep before astronomical motion.",
        }

    def _limit_readiness(self) -> dict[str, object]:
        cfg = self._safety_config
        reasons: list[str] = []
        horizon = self._onstep_limits.horizon_deg
        overhead = self._onstep_limits.overhead_deg
        if horizon is None:
            reasons.append("onstep_horizon_limit_missing")
        if overhead is None:
            reasons.append("onstep_overhead_limit_missing")
        broad = (
            horizon is not None
            and overhead is not None
            and horizon <= -5.0
            and overhead >= 89.5
        )
        if broad and not cfg.allow_broad_onstep_limits:
            reasons.append("firmware_limits_broad")
        if cfg.require_onstep_limits and (horizon is None or overhead is None):
            reasons.append("required_onstep_limits_unavailable")
        if cfg.horizon_path and self._horizon is None:
            reasons.append("horizon_profile_missing")
        configured_complete = all(
            value is not None
            for value in (
                cfg.min_alt_deg,
                cfg.max_alt_deg,
                cfg.ha_east_limit_h,
                cfg.ha_west_limit_h,
                cfg.dec_min_deg,
                cfg.dec_max_deg,
            )
        )
        if not configured_complete:
            reasons.append("configured_limits_missing")
        ready = configured_complete and not reasons
        return {
            "ready": ready,
            "onstep": {
                "horizon_deg": horizon,
                "overhead_deg": overhead,
                "readback_available": horizon is not None and overhead is not None,
                "broad": broad,
            },
            "configured": {
                "min_alt_deg": cfg.min_alt_deg,
                "max_alt_deg": cfg.max_alt_deg,
                "ha_east_limit_h": cfg.ha_east_limit_h,
                "ha_west_limit_h": cfg.ha_west_limit_h,
                "dec_min_deg": cfg.dec_min_deg,
                "dec_max_deg": cfg.dec_max_deg,
                "require_onstep_limits": cfg.require_onstep_limits,
                "allow_broad_onstep_limits": cfg.allow_broad_onstep_limits,
            },
            "horizon_profile": {
                "path": cfg.horizon_path or None,
                "loaded": self._horizon is not None,
            },
            "reasons": reasons,
            "required_action": None if ready else "Review/configure OnStep firmware limits and SmartTScope rig limits.",
        }

    def _onstep_firmware_protection(self) -> dict[str, object]:
        reasons: list[str] = []
        proven: list[str] = []
        unproven: list[str] = []
        if self._onstep_limits.horizon_deg is None:
            reasons.append("onstep_horizon_unreadable")
            unproven.append("horizon")
        else:
            proven.append("horizon")
        if self._onstep_limits.overhead_deg is None:
            reasons.append("onstep_overhead_unreadable")
            unproven.append("overhead")
        else:
            proven.append("overhead")
        extended = self._onstep_extended_limits or self.read_onstep_extended_limits()
        west = extended.get("meridian_west") if isinstance(extended, dict) else None
        west_h = west.get("hours") if isinstance(west, dict) else None
        axis_values = [
            extended.get("axis1_min_deg"),
            extended.get("axis1_max_deg"),
            extended.get("axis2_min_deg"),
            extended.get("axis2_max_deg"),
        ] if isinstance(extended, dict) else []
        if west_h is None:
            unproven.append("hour_angle_or_meridian")
            reasons.append("firmware_meridian_limit_unreadable")
        else:
            proven.append("meridian_limit_readback")
        if not axis_values or any(value is None for value in axis_values):
            unproven.append("home_relative_axis_limits")
        else:
            proven.append("axis_limit_readback")
        if not reasons:
            reasons.append("firmware_limits_readable_but_physical_stop_not_proven")
        cfg = self._safety_config
        path_coverage = _evaluate_onstep_meridian_path_coverage(
            limits=extended,
            flip_boundary_h=cfg.ha_west_limit_h - (cfg.meridian_margin_deg / 15.0),
            west_stop_h=cfg.ha_west_limit_h,
        )
        auto_flip = self.read_onstep_auto_meridian_flip()
        firmware_identity = self.read_onstep_firmware_identity()
        west_limit = extended.get("meridian_west") if isinstance(extended, dict) else None
        west_limit_minutes = (
            west_limit.get("minutes") if isinstance(west_limit, dict) else None
        )
        dual_pier = (
            extended.get("dual_pier_west_ha_stop")
            if isinstance(extended, dict)
            else None
        )
        proof_path = self._safety_config.firmware_proof_file
        proof_validation = validate_firmware_proof(
            load_firmware_proof(proof_path) if proof_path else None,
            firmware_identity=firmware_identity,
            dual_pier_enabled=bool(
                isinstance(dual_pier, dict) and dual_pier.get("enabled") is True
            ),
            west_limit_minutes=(
                float(west_limit_minutes)
                if isinstance(west_limit_minutes, (int, float))
                else None
            ),
            requested_west_stop_h=cfg.ha_west_limit_h,
            axis_limits={
                key: (
                    float(extended[key])
                    if isinstance(extended.get(key), (int, float))
                    else None
                )
                for key in (
                    "axis1_min_deg",
                    "axis1_max_deg",
                    "axis2_min_deg",
                    "axis2_max_deg",
                )
            },
            observer={
                "lat": round(float(cfg.observer_lat), 6),
                "lon": round(float(cfg.observer_lon), 6),
            },
            auto_meridian_flip_enabled=(
                bool(auto_flip.get("enabled"))
                if auto_flip.get("enabled") is not None
                else None
            ),
        )
        proof_mode = proof_validation.get("proof_mode")
        stock_axis1_proven = bool(
            proof_validation.get("valid") and proof_mode == "axis1_fallback"
        )
        dual_pier_proven = bool(
            proof_validation.get("valid") and proof_mode == "dual_pier"
        )
        reasons.extend(
            reason
            for reason in path_coverage["blockers"]
            if not (
                stock_axis1_proven
                and reason == "postflip_pier_east_dual_pier_stop_not_available"
            )
            if reason not in reasons
        )
        reasons.extend(
            reason
            for reason in proof_validation["reasons"]
            if reason not in reasons
        )
        if proof_validation.get("valid"):
            reasons = [
                reason
                for reason in reasons
                if reason != "firmware_limits_readable_but_physical_stop_not_proven"
            ]
        full_path_configured = bool(path_coverage["full_path_firmware_protected"])
        preflip_configured = bool(path_coverage["preflip_firmware_protected"])
        unattended_allowed = bool(
            (full_path_configured and dual_pier_proven)
            or (
                path_coverage.get("app_policy_valid")
                and preflip_configured
                and stock_axis1_proven
            )
        )
        operational_stop_deg = round(cfg.ha_west_limit_h * 15.0, 4)
        axis1_max_deg = extended.get("axis1_max_deg") if isinstance(extended, dict) else None
        status = (
            "proven"
            if unattended_allowed
            else "unknown"
            if "horizon" in unproven or "overhead" in unproven
            else "partial"
        )
        return {
            "status": status,
            "unattended_tracking_allowed": unattended_allowed,
            "supervised_tracking_allowed": True,
            "primary_safety_layer": "onstep_firmware",
            "adapter_watchdog_role": "secondary_supervised_backup",
            "proven": proven,
            "unproven": unproven,
            "reasons": reasons,
            "onstep_limits": {
                "horizon_deg": self._onstep_limits.horizon_deg,
                "overhead_deg": self._onstep_limits.overhead_deg,
                "extended": extended,
            },
            "meridian_path_coverage": path_coverage,
            "auto_meridian_flip": auto_flip,
            "firmware_identity": firmware_identity,
            "firmware_safeguard_proof": proof_validation,
            "operational_stop_deg": operational_stop_deg,
            "firmware_fallback_type": "axis1_max",
            "firmware_fallback_deg": axis1_max_deg,
            "firmware_fallback_proven": stock_axis1_proven,
            "firmware_fallback_physically_safe": stock_axis1_proven,
            "required_action": (
                None
                if unattended_allowed
                else (
                    "Pass the staged watched stock Axis-1 fallback proof, or install and "
                    "prove the dual-pier firmware stop."
                )
            ),
        }

    def _motion_authority(
        self,
        time_readiness: dict[str, object],
        location_readiness: dict[str, object],
        limit_readiness: dict[str, object],
        mechanical_readiness: dict[str, object],
    ) -> dict[str, bool]:
        lock_reason = self._safety_lock.reason if self._safety_lock else None
        mechanical_blocked = self._safety_lock is not None and lock_reason not in {"system_clock_invalid", "onstep_clock_invalid"}
        anchor_ready = (self._home_confirmed or self._park_pose_confirmed) and not mechanical_blocked
        mechanical_ready = bool(mechanical_readiness.get("ready")) and not mechanical_blocked
        astronomy_ready = (
            mechanical_ready
            and self._safety_lock is None
            and bool(time_readiness.get("ready"))
            and bool(location_readiness.get("ready"))
            and bool(limit_readiness.get("ready"))
        )
        return {
            "emergency_stop": True,
            "recovery_pulse": anchor_ready,
            "terrestrial_low_rate_manual": mechanical_ready,
            "mechanical_manual": mechanical_ready,
            "tracking": astronomy_ready,
            "goto": astronomy_ready,
            "guide": astronomy_ready,
            "park": anchor_ready,
            "unpark": anchor_ready,
            "unpark_stop_tracking": anchor_ready,
        }

    def connect(self) -> bool:
        s = self._bus._serial
        if s is not None and s.is_open:
            _log.info("OnStepMount.connect(): already open on %s", self._port)
            return True
        _log.info(
            "OnStepMount.connect(): opening %s @ %d baud timeout=%.1fs",
            self._port, self._baud_rate, self._timeout,
        )
        try:
            s = serial.Serial(self._port, self._baud_rate, timeout=self._timeout)
        except (serial.SerialException, OSError) as exc:
            _log.error("OnStepMount.connect(): failed to open %s: %s", self._port, exc)
            if self._safety_config.indi_fallback_enabled:
                _log.error(
                    "OnStep INDI fallback is configured (%s:%d/%s) but no INDI mount adapter exists yet.",
                    self._safety_config.indi_host,
                    self._safety_config.indi_port,
                    self._safety_config.indi_device,
                )
            return False
        self._bus._serial = s

        product = ""
        for attempt in range(_MAX_GVP_ATTEMPTS):
            if attempt:
                time.sleep(_GVP_RETRY_DELAY_S)
            s.reset_input_buffer()
            s.write(b":GVP#")
            raw = s.read(32)
            if not isinstance(raw, bytes):
                _log.warning("OnStepMount.connect(): :GVP# returned non-bytes %r (mock?)", raw)
                break
            product = raw.decode(errors="replace").rstrip("#\r\n").strip()
            _log.info("OnStepMount.connect(): :GVP# attempt=%d response=%r", attempt + 1, product)
            if not product or ("on" in product.lower() and "step" in product.lower()):
                break
            _log.warning(
                "OnStepMount.connect(): :GVP# unexpected %r on attempt %d; retrying",
                product, attempt + 1,
            )
        else:
            _log.error("OnStepMount.connect(): not OnStep after %d attempts; closing", _MAX_GVP_ATTEMPTS)
            s.close()
            self._bus._serial = None
            return False

        self.disable_tracking()
        try:
            self.refresh_safety_state()
        except Exception as exc:
            _log.warning("OnStepMount.connect(): initial safety readback failed: %s", exc)
        _log.info("OnStepMount.connect(): connected product=%r port=%s", product, self._port)
        return True

    def disconnect(self) -> None:
        self._invalidate_mechanical_trust("controller_disconnected")
        self._bus.close()

    def _raw_send(self, cmd: str) -> bytes:
        return self._bus.raw_send(cmd)

    def _send(self, cmd: str) -> str:
        return self._bus.send(cmd)

    def confirm_home_position(self) -> None:
        self._home_confirmed = True
        self._mechanical_axis_position = {"axis1_deg": 0.0, "axis2_deg": 0.0}
        self._mechanical_trust_invalidations = [
            reason for reason in self._mechanical_trust_invalidations
            if reason in {"onstep_park_failed", "onstep_at_limit"}
        ]
        if self._safety_lock and self._safety_lock.reason == "home_not_confirmed":
            self._safety_lock = None
        self._persist_last_state(last_command="confirm_home_position", force=True)

    def invalidate_home_authority(self, reason: str = "manual_reposition_reported") -> None:
        """Invalidate logical mechanical coordinates after a reported physical change."""
        self._invalidate_mechanical_trust(reason)
        self._persist_last_state(last_command="invalidate_home_authority", force=True)

    def confirm_park_pose(self) -> dict[str, object]:
        state = self.get_state()
        if state != MountState.PARKED:
            raise OnStepSafetyError(SafetyViolation(
                reason="park_pose_confirmation_requires_parked",
                command="confirm_park_pose",
                severity=SafetySeverity.BLOCKED,
                recovery_hint="Park the mount and visually confirm the mirror-safe upward pose.",
            ))
        self._park_pose_confirmed = True
        park = (self._mechanical_calibration or {}).get("park_pose")
        if isinstance(park, dict):
            self._mechanical_axis_position = {
                "axis1_deg": float(park.get("axis1_deg", 0.0)),
                "axis2_deg": float(park.get("axis2_deg", 0.0)),
            }
        self._mechanical_trust_invalidations = [
            reason for reason in self._mechanical_trust_invalidations
            if (
                not reason.startswith("persisted_")
                and reason not in {"park_pose_not_confirmed", "no_persisted_startup_state"}
            )
        ]
        self._persist_last_state(last_command="confirm_park_pose", force=True)
        return {
            "ok": True,
            "state": state.name.lower(),
            "park_pose_confirmed": self._park_pose_confirmed,
            "position_authority": self._position_authority(),
        }

    def store_park_pose(
        self,
        *,
        axis1_deg: float = 0.0,
        axis2_deg: float = 0.0,
        confirmed_by_user: bool = False,
    ) -> dict[str, object]:
        _log.warning(
            "store_park_pose(axis1_deg=..., axis2_deg=...) is deprecated; "
            "the adapter now captures the current logical axes itself"
        )
        result = self.set_park_position_from_current(
            confirmed_safe=confirmed_by_user,
            allow_at_home=False,
        )
        return {
            "ok": result.ok,
            "onstep_reply": result.onstep_reply,
            "controller_updated": result.controller_updated,
            "local_record_persisted": result.local_record_persisted,
            "calibration_file": str(self._mechanical_calibration_path()),
            "park_pose_confirmed": self._park_pose_confirmed,
            "mechanical_axis_position": self._mechanical_axis_position,
            "record": result.record,
            "error": result.error,
        }

    def _park_record_from_payload(self, payload: dict[str, object]) -> StoredParkPosition | None:
        record = payload.get("stored_park_position")
        if isinstance(record, dict):
            try:
                invalidations = tuple(str(value) for value in record.get("invalidation_reasons", ()))
                return StoredParkPosition(
                    ra=float(record["ra"]),
                    dec=float(record["dec"]),
                    axis1_deg=_optional_float(record.get("axis1_deg")),
                    axis2_deg=_optional_float(record.get("axis2_deg")),
                    pier_side=_optional_str(record.get("pier_side")),
                    captured_at_utc=str(record["captured_at_utc"]),
                    firmware_product=_optional_str(record.get("firmware_product")),
                    firmware_version=_optional_str(record.get("firmware_version")),
                    firmware_date=_optional_str(record.get("firmware_date")),
                    home_authority_state=str(record.get("home_authority_state", "unknown")),
                    trusted=bool(record.get("trusted", False)),
                    invalidation_reasons=invalidations,
                )
            except (KeyError, TypeError, ValueError):
                return None

        # Migrate the original mechanical-calibration record as a logical-axis-only
        # PARK record. It cannot provide astronomical coordinates.
        legacy = payload.get("park_pose")
        if isinstance(legacy, dict):
            return None
        return None

    def get_stored_park_position(self) -> StoredParkPosition | None:
        payload = self._mechanical_calibration
        if not isinstance(payload, dict):
            return None
        record = self._park_record_from_payload(payload)
        if record is None:
            return None

        reasons = list(record.invalidation_reasons)
        reasons.extend(self._mechanical_trust_invalidations)
        if not self._home_confirmed:
            reasons.append("home_authority_untrusted")
        configured_observer = payload.get("observer")
        if isinstance(configured_observer, dict):
            if (
                abs(float(configured_observer.get("lat", self._safety_config.observer_lat))
                    - self._safety_config.observer_lat) > 1e-9
                or abs(float(configured_observer.get("lon", self._safety_config.observer_lon))
                       - self._safety_config.observer_lon) > 1e-9
            ):
                reasons.append("observer_configuration_changed")
        stored_configuration = payload.get("mount_configuration")
        current_configuration = {
            "ha_east_limit_h": self._safety_config.ha_east_limit_h,
            "ha_west_limit_h": self._safety_config.ha_west_limit_h,
            "mechanical_axis1_min_deg": self._safety_config.mechanical_axis1_min_deg,
            "mechanical_axis1_max_deg": self._safety_config.mechanical_axis1_max_deg,
            "mechanical_axis2_min_deg": self._safety_config.mechanical_axis2_min_deg,
            "mechanical_axis2_max_deg": self._safety_config.mechanical_axis2_max_deg,
        }
        if isinstance(stored_configuration, dict) and stored_configuration != current_configuration:
            reasons.append("mount_configuration_changed")
        if self._bus.is_open:
            try:
                current_firmware = self.read_onstep_firmware_identity()
                if (
                    record.firmware_product != _optional_str(current_firmware.get("product"))
                    or record.firmware_version != _optional_str(current_firmware.get("version"))
                    or record.firmware_date != _optional_str(current_firmware.get("date"))
                ):
                    reasons.append("firmware_identity_changed")
            except Exception:
                reasons.append("firmware_identity_unavailable")
        reasons = list(dict.fromkeys(reasons))
        return replace(record, trusted=not reasons, invalidation_reasons=tuple(reasons))

    def _prepare_park_record_file(self, payload: dict[str, object]) -> tuple[Path, Path]:
        path = self._mechanical_calibration_path()
        if path is None:
            raise OnStepSafetyError(SafetyViolation(
                reason="mechanical_calibration_file_required",
                command="set_park_position_from_current",
                severity=SafetySeverity.UNKNOWN,
                recovery_hint="Configure mechanical_calibration_file before changing OnStep PARK.",
            ))
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_suffix(path.suffix + ".park-pending")
        with pending.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        return path, pending

    def set_park_position_from_current(
        self,
        *,
        confirmed_safe: bool,
        allow_at_home: bool = False,
    ) -> SetParkPositionResult:
        command = "set_park_position_from_current"
        if not confirmed_safe:
            raise OnStepSafetyError(SafetyViolation(
                reason="park_position_confirmation_required",
                command=command,
                severity=SafetySeverity.UNKNOWN,
                recovery_hint="The calling application must obtain physical PARK confirmation first.",
            ))
        if not self._home_confirmed:
            raise OnStepSafetyError(SafetyViolation(
                reason="home_reference_required_before_set_park",
                command=command,
                severity=SafetySeverity.UNKNOWN,
                recovery_hint="Establish trusted HOME authority before changing PARK.",
            ))
        authority_reasons = [
            reason for reason in self._mechanical_trust_invalidations
            if reason not in {"park_pose_not_confirmed"}
        ]
        if authority_reasons or self._safety_lock is not None:
            raise OnStepSafetyError(SafetyViolation(
                reason="home_authority_not_trusted_for_set_park",
                command=command,
                severity=SafetySeverity.UNKNOWN,
                recovery_hint="Re-establish HOME authority and clear controller faults before changing PARK.",
            ))

        state = self.get_state()
        decoded = dict(self._last_decoded_status or {})
        if (decoded.get("at_home") or self._at_mechanical_home) and not allow_at_home:
            raise OnStepSafetyError(SafetyViolation(
                reason="set_park_at_home_refused",
                command=command,
                severity=SafetySeverity.BLOCKED,
                recovery_hint="Move to the intended PARK pose or explicitly set allow_at_home=True.",
            ))
        if state == MountState.SLEWING or decoded.get("slewing"):
            raise OnStepSafetyError(SafetyViolation(
                reason="set_park_requires_stationary_mount",
                command=command,
                severity=SafetySeverity.BLOCKED,
            ))
        if state == MountState.TRACKING or decoded.get("tracking"):
            raise OnStepSafetyError(SafetyViolation(
                reason="set_park_requires_tracking_off",
                command=command,
                severity=SafetySeverity.BLOCKED,
            ))
        if decoded.get("at_limit") or decoded.get("park_failed"):
            raise OnStepSafetyError(SafetyViolation(
                reason="set_park_blocked_by_onstep_fault",
                command=command,
                severity=SafetySeverity.LIMIT_HIT,
            ))

        position = self.get_position()
        axes = self.read_onstep_axis_position()
        pier = self.read_pier_side()
        firmware = self.read_onstep_firmware_identity()
        captured_at = datetime.now(timezone.utc).isoformat()
        authority = self._mechanical_position_authority()
        record = StoredParkPosition(
            ra=position.ra,
            dec=position.dec,
            axis1_deg=_optional_float(axes.get("axis1_deg")),
            axis2_deg=_optional_float(axes.get("axis2_deg")),
            pier_side=_optional_str(pier.get("value")),
            captured_at_utc=captured_at,
            firmware_product=_optional_str(firmware.get("product")),
            firmware_version=_optional_str(firmware.get("version")),
            firmware_date=_optional_str(firmware.get("date")),
            home_authority_state=str(authority.get("state", "unknown")),
            trusted=True,
        )
        payload = {
            "schema": "onstep-mechanical-calibration-v2",
            "updated_at_utc": captured_at,
            "home": {"axis1_deg": 0.0, "axis2_deg": 0.0},
            "park_pose": {
                "axis1_deg": record.axis1_deg,
                "axis2_deg": record.axis2_deg,
            },
            "stored_park_position": _stored_park_to_dict(record),
            "observer": {
                "lat": self._safety_config.observer_lat,
                "lon": self._safety_config.observer_lon,
            },
            "mount_configuration": {
                "ha_east_limit_h": self._safety_config.ha_east_limit_h,
                "ha_west_limit_h": self._safety_config.ha_west_limit_h,
                "mechanical_axis1_min_deg": self._safety_config.mechanical_axis1_min_deg,
                "mechanical_axis1_max_deg": self._safety_config.mechanical_axis1_max_deg,
                "mechanical_axis2_min_deg": self._safety_config.mechanical_axis2_min_deg,
                "mechanical_axis2_max_deg": self._safety_config.mechanical_axis2_max_deg,
            },
            "onstep_set_park_reply": None,
        }
        path, pending = self._prepare_park_record_file(payload)
        reply = ""
        try:
            reply = self._bus.send_fixed(":hQ#", size=1, timeout=2.0)
            if reply != "1":
                pending.unlink(missing_ok=True)
                return SetParkPositionResult(
                    ok=False,
                    controller_updated=False,
                    local_record_persisted=False,
                    onstep_reply=reply,
                    record=None,
                    error="onstep_rejected_set_park",
                )
            payload["onstep_set_park_reply"] = reply
            with pending.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.write("\n")
            try:
                os.replace(pending, path)
            except Exception as exc:
                return SetParkPositionResult(
                    ok=False,
                    controller_updated=True,
                    local_record_persisted=False,
                    onstep_reply=reply,
                    record=replace(
                        record,
                        trusted=False,
                        invalidation_reasons=("local_record_persistence_failed",),
                    ),
                    error=f"local_record_persistence_failed:{exc}",
                )
        finally:
            if reply != "1":
                pending.unlink(missing_ok=True)

        self._mechanical_calibration = payload
        self._park_pose_confirmed = True
        self._mechanical_axis_position = {
            "axis1_deg": float(record.axis1_deg or 0.0),
            "axis2_deg": float(record.axis2_deg or 0.0),
        }
        self._mechanical_trust_invalidations = [
            reason for reason in self._mechanical_trust_invalidations
            if (
                not reason.startswith("persisted_")
                and reason not in {"park_pose_not_confirmed", "no_persisted_startup_state"}
            )
        ]
        self._persist_last_state(last_command=command, force=True)
        return SetParkPositionResult(
            ok=True,
            controller_updated=True,
            local_record_persisted=True,
            onstep_reply=reply,
            record=self.get_stored_park_position(),
        )

    def clear_mechanical_confirmations(self, reason: str = "operator_clear") -> None:
        self._home_confirmed = not self._safety_config.require_home_confirmation
        self._invalidate_mechanical_trust(reason)

    def note_external_motion(self, reason: str = "external_motion") -> None:
        """Invalidate HOME-relative position after motion outside adapter control."""
        self._at_mechanical_home = False
        self._mechanical_axis_position = None
        self._invalidate_mechanical_trust(reason)

    def clear_safety_lock(self) -> None:
        self._safety_lock = None

    def refresh_safety_state(self) -> None:
        self._onstep_limits = self.read_onstep_limits()
        self._onstep_extended_limits = self.read_onstep_extended_limits()
        if self._safety_config.sync_limits_to_onstep:
            self._sync_configured_limits_to_onstep()
            self._onstep_limits = self.read_onstep_limits()
        status = self._send(":GU#")
        if status:
            self._inspect_status(status)
        self._last_system_clock_check = self.read_system_clock_sanity()
        self._last_clock_check = self.read_onstep_clock()
        self._last_sidereal_check = self.read_onstep_sidereal_consistency()
        self._apply_clock_safety_lock()

    def read_system_clock_sanity(self) -> dict[str, object]:
        system_local = datetime.now().astimezone()
        application_path = Path(__file__).resolve().parents[2] / "app.py"
        reference_path = (
            application_path
            if application_path.is_file()
            else Path(__file__).resolve()
        )
        reference_source = (
            "smart_telescope_application"
            if reference_path == application_path
            else "installed_onstep_package"
        )
        reference_mtime = None
        delta_s = None
        valid = system_local.year >= 2024
        message = None
        try:
            reference_mtime = datetime.fromtimestamp(reference_path.stat().st_mtime).astimezone()
            delta_s = (system_local - reference_mtime).total_seconds()
            valid = valid and delta_s >= 0
        except OSError as exc:
            message = f"Could not verify Raspberry time against package file timestamp: {exc}"
        if message is None and not valid:
            if system_local.year < 2024:
                message = "Raspberry clock is before 2024; wait for GPS/NTP or set time manually."
            else:
                message = "Raspberry clock is older than the installed package file timestamp."
        if message is None:
            message = "Raspberry clock passed package timestamp sanity check."
        trust_source = (self._safety_config.time_trust_source or "raspberry_plausible").lower()
        trusted = trust_source in _TRUSTED_TIME_SOURCES or abs(self._safety_config.time_offset_s) > 0.001
        confidence = "invalid" if not valid else ("trusted" if trusted else "plausible_not_trusted")
        return {
            "valid": valid,
            "warning": not valid,
            "confidence": confidence,
            "trust_source": trust_source,
            "trusted_for_astronomy": bool(valid and trusted),
            "system_local": system_local.isoformat(timespec="seconds"),
            "reference_file": str(reference_path),
            "reference_source": reference_source,
            "reference_mtime_local": (
                reference_mtime.isoformat(timespec="seconds")
                if reference_mtime is not None
                else None
            ),
            "delta_s": round(delta_s, 1) if delta_s is not None else None,
            "message": message,
        }

    def read_onstep_limits(self) -> OnStepLimits:
        return OnStepLimits(
            horizon_deg=self._read_limit(":Gh#"),
            overhead_deg=self._read_limit(":Go#"),
        )

    def read_onstep_extended_limits(self) -> dict[str, object]:
        commands = {
            "meridian_east": ":GXE9#",
            "meridian_west": ":GXEA#",
            "axis1_min_deg": ":GXEe#",
            "axis1_max_deg": ":GXEw#",
            "axis1_max_h": ":GXEB#",
            "axis2_min_deg": ":GXEC#",
            "axis2_max_deg": ":GXED#",
        }
        raw: dict[str, str] = {}
        values: dict[str, float | None] = {}
        for name, command in commands.items():
            try:
                reply = self._send(command).strip()
                raw[name] = reply
                values[name] = float(reply)
            except (TypeError, ValueError, RuntimeError):
                raw[name] = ""
                values[name] = None
        east_minutes = values["meridian_east"]
        west_minutes = values["meridian_west"]
        result: dict[str, object] = {
            "available": east_minutes is not None or west_minutes is not None,
            "raw": raw,
            "meridian_east": {
                "minutes": east_minutes,
                "degrees": east_minutes / 4.0 if east_minutes is not None else None,
                "hours": east_minutes / 60.0 if east_minutes is not None else None,
            },
            "meridian_west": {
                "minutes": west_minutes,
                "degrees": west_minutes / 4.0 if west_minutes is not None else None,
                "hours": west_minutes / 60.0 if west_minutes is not None else None,
            },
            "axis1_min_deg": values["axis1_min_deg"],
            "axis1_max_deg": values["axis1_max_deg"],
            "axis1_max_h": values["axis1_max_h"],
            "axis2_min_deg": values["axis2_min_deg"],
            "axis2_max_deg": values["axis2_max_deg"],
        }
        try:
            dual_pier_raw = self._send(":GXEG#").strip()
        except RuntimeError:
            dual_pier_raw = ""
        result["dual_pier_west_ha_stop"] = {
            "available": dual_pier_raw in {"0", "1"},
            "enabled": dual_pier_raw == "1",
            "raw": dual_pier_raw,
            "command": ":GXEG#",
        }
        self._onstep_extended_limits = result
        return result

    def read_onstep_firmware_identity(self) -> dict[str, object]:
        return {
            "product": self._send(":GVP#").strip(),
            "version": self._send(":GVN#").strip(),
            "date": self._send(":GVD#").strip(),
        }

    def read_onstep_auto_meridian_flip(self) -> dict[str, object]:
        reply = self._send(":GX95#")
        enabled = reply.strip() == "1" if reply else None
        return {
            "available": reply != "",
            "enabled": enabled,
            "raw": reply,
            "trigger_model": (
                "Stock OnStepX uses the west-past-meridian threshold on pier west "
                "at positive HA. Pier east at positive HA needs the SmartTScope "
                "dual-pier extension or falls back to Axis 1 maximum."
            ),
        }

    def read_onstep_preferred_pier_side(self) -> dict[str, object]:
        """Read OnStepX preferred pier-side policy using ``:GX96#``."""
        reply = self._send(":GX96#").strip().upper()
        value = {
            "E": "east",
            "W": "west",
            "B": "best",
            "A": "automatic",
        }.get(reply)
        return {
            "available": value is not None,
            "value": value,
            "raw": reply,
            "command": ":GX96#",
        }

    def read_pier_side(self) -> dict[str, object]:
        """Read the authoritative GEM pier side from ``:Gm#``."""
        reply = self._send(":Gm#").strip().upper()
        value = {"E": "east", "W": "west", "N": None}.get(reply)
        return {
            "available": reply in {"E", "W"},
            "value": value,
            "raw": reply,
            "command": ":Gm#",
        }

    def begin_meridian_tracking_session(self) -> dict[str, object]:
        """Capture the pier side selected by OnStep's BEST goto policy."""
        pier = self.read_pier_side()
        value = pier.get("value")
        if value not in {"east", "west"}:
            return {
                "ok": False,
                "reason": "pier_side_unavailable",
                "pier_side": pier,
            }
        self._meridian_initial_pier_side = str(value)
        self._meridian_postflip_pier_side = None
        self._meridian_flip_completed = False
        return {
            "ok": True,
            "initial_pier_side": self._meridian_initial_pier_side,
            "expected_opposite_side": (
                "west" if self._meridian_initial_pier_side == "east" else "east"
            ),
            "pier_side": pier,
            "selection_policy": "onstep_best_observed",
        }

    def configure_onstep_west_meridian_limit(
        self,
        *,
        minutes: int,
        confirmed_by_user: bool = False,
    ) -> dict[str, object]:
        """Persist the OnStep pier-west meridian threshold with readback."""
        if not confirmed_by_user:
            raise OnStepSafetyError(SafetyViolation(
                reason="meridian_limit_sync_confirmation_required",
                command="configure_onstep_west_meridian_limit",
                severity=SafetySeverity.UNKNOWN,
                recovery_hint="Explicitly confirm the OnStep west-meridian policy update.",
            ))
        if not -1440 <= int(minutes) <= 1440:
            raise ValueError("OnStep meridian limit minutes must be in [-1440, 1440]")
        state = self.get_state()
        if state != MountState.PARKED:
            raise OnStepSafetyError(SafetyViolation(
                reason="park_required_for_meridian_limit_sync",
                command="configure_onstep_west_meridian_limit",
                severity=SafetySeverity.BLOCKED,
                recovery_hint="Park the mount before changing persistent OnStep meridian policy.",
            ))
        before = self.read_onstep_extended_limits()
        reply = self._bus.send_fixed(f":SXEA,{int(minutes)}#", size=1, timeout=2.0)
        after = self.read_onstep_extended_limits()
        west = after.get("meridian_west") if isinstance(after, dict) else None
        readback_minutes = west.get("minutes") if isinstance(west, dict) else None
        verified = (
            reply == "1"
            and isinstance(readback_minutes, (int, float))
            and abs(float(readback_minutes) - int(minutes)) < 0.5
        )
        self._persist_last_state(last_command="configure_onstep_west_meridian_limit", force=True)
        return {
            "ok": verified,
            "command": f":SXEA,{int(minutes)}#",
            "reply": reply,
            "requested_minutes": int(minutes),
            "readback_minutes": readback_minutes,
            "state": state.name.lower(),
            "before": before,
            "after": after,
        }

    def read_onstep_axis_position(self) -> dict[str, object]:
        raw: dict[str, str] = {}
        values: dict[str, float | None] = {}
        for name, command in {
            "axis1_deg": ":GX42#",
            "axis2_deg": ":GX43#",
        }.items():
            try:
                reply = self._send(command).strip()
                raw[name] = reply
                values[name] = float(reply)
            except (TypeError, ValueError, RuntimeError):
                raw[name] = ""
                values[name] = None
        return {
            "available": all(value is not None for value in values.values()),
            "raw": raw,
            **values,
        }

    def set_onstep_limits(
        self,
        *,
        horizon_deg: float | None = None,
        overhead_deg: float | None = None,
    ) -> OnStepLimits:
        if horizon_deg is not None:
            value = _format_limit_degrees(horizon_deg, signed=True)
            if self._send(f":Sh{value}#") != "1":
                raise RuntimeError(f"OnStep rejected horizon limit update :Sh{value}#")
        if overhead_deg is not None:
            value = _format_limit_degrees(overhead_deg, signed=False)
            if self._send(f":So{value}#") != "1":
                raise RuntimeError(f"OnStep rejected overhead limit update :So{value}#")
        self._onstep_limits = self.read_onstep_limits()
        self._persist_last_state(force=True)
        return self._onstep_limits

    def read_onstep_clock(self) -> dict[str, object]:
        system_local = self._active_now_utc().astimezone().replace(tzinfo=None)
        date_reply = ""
        time_reply = ""
        try:
            date_reply = self._send(":GC#")
            time_reply = self._send(":GL#")
            onstep_local = _parse_onstep_local_datetime(date_reply, time_reply)
            delta_s = abs((system_local - onstep_local).total_seconds())
            threshold = self._safety_config.clock_warning_threshold_s
            return {
                "available": True,
                "onstep_date_reply": date_reply,
                "onstep_time_reply": time_reply,
                "onstep_local": onstep_local.isoformat(timespec="seconds"),
                "system_local": system_local.isoformat(timespec="seconds"),
                "comparison_source": (
                    "gps_runtime"
                    if abs(self._safety_config.time_offset_s) > 0.001
                    else "raspberry_system"
                ),
                "delta_s": round(delta_s, 1),
                "threshold_s": threshold,
                "warning": delta_s > threshold,
                "message": (
                    f"OnStep clock differs from active SmartTScope time by {delta_s:.0f}s"
                    if delta_s > threshold
                    else None
                ),
            }
        except Exception as exc:
            return {
                "available": False,
                "onstep_date_reply": date_reply or None,
                "onstep_time_reply": time_reply or None,
                "system_local": system_local.isoformat(timespec="seconds"),
                "comparison_source": (
                    "gps_runtime"
                    if abs(self._safety_config.time_offset_s) > 0.001
                    else "raspberry_system"
                ),
                "warning": True,
                "message": f"OnStep clock readback failed: {exc}",
            }

    def read_onstep_sidereal_consistency(self) -> dict[str, object]:
        reply = ""
        try:
            reply = self._send(":GS#")
            onstep_lst_h = _parse_ra(reply)
            expected_lst_h = _lst_hours(
                self._safety_config.observer_lon,
                self._active_now_utc(),
            )
            delta_h = abs(((onstep_lst_h - expected_lst_h + 12.0) % 24.0) - 12.0)
            delta_s = delta_h * 3600.0
            threshold_s = self._safety_config.clock_warning_threshold_s
            ok = delta_s <= threshold_s
            return {
                "ok": ok,
                "reason": None if ok else "onstep_sidereal_time_mismatch",
                "onstep_reply": reply,
                "onstep_lst_h": round(onstep_lst_h, 6),
                "expected_lst_h": round(expected_lst_h, 6),
                "delta_s": round(delta_s, 1),
                "threshold_s": threshold_s,
            }
        except Exception as exc:
            return {
                "ok": False,
                "reason": "onstep_sidereal_time_unavailable",
                "onstep_reply": reply or None,
                "message": str(exc),
            }

    def read_onstep_site(self) -> dict[str, object]:
        lat_reply = ""
        lon_reply = ""
        try:
            lat_reply = self._send(":Gt#")
            lon_reply = self._send(":Gg#")
            lat = _parse_degrees(lat_reply)
            # OnStep/LX200 longitude is west-positive; SmartTScope is east-positive.
            lon = -_parse_degrees(lon_reply)
            return {
                "available": True,
                "lat_reply": lat_reply,
                "lon_reply": lon_reply,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "alt_m": None,
                "message": None,
            }
        except Exception as exc:
            return {
                "available": False,
                "lat_reply": lat_reply or None,
                "lon_reply": lon_reply or None,
                "lat": None,
                "lon": None,
                "alt_m": None,
                "message": f"OnStep site readback failed: {exc}",
            }

    def _active_now_utc(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=self._safety_config.time_offset_s)

    def _apply_clock_safety_lock(self) -> None:
        system_clock = self._last_system_clock_check or self.read_system_clock_sanity()
        onstep_clock = self._last_clock_check or self.read_onstep_clock()
        using_runtime_time = abs(self._safety_config.time_offset_s) > 0.001
        if not using_runtime_time and not bool(system_clock.get("valid")):
            self._safety_lock = SafetyViolation(
                reason="system_clock_invalid",
                command="clock_check",
                severity=SafetySeverity.UNKNOWN,
                recovery_hint=(
                    "Raspberry time is not trusted. Wait for GPS/NTP or set/check the Pi time "
                    "before syncing OnStep or moving the mount."
                ),
            )
            return
        if bool(onstep_clock.get("warning")):
            self._safety_lock = SafetyViolation(
                reason="onstep_clock_invalid",
                command="clock_check",
                severity=SafetySeverity.UNKNOWN,
                onstep_reply=str(onstep_clock.get("onstep_date_reply") or ""),
                recovery_hint=(
                    "OnStep time is not trusted. If the Raspberry time/location are correct, "
                    "sync the OnStep clock from SmartTScope before allowing mount motion."
                ),
            )
            return
        if self._safety_lock and self._safety_lock.reason in {
            "system_clock_invalid",
            "onstep_clock_invalid",
        }:
            self._safety_lock = None

    def sync_onstep_clock_from_system(
        self,
        *,
        confirmed_by_user: bool = False,
        sync_utc_offset: bool = False,
    ) -> dict[str, object]:
        if not confirmed_by_user:
            raise OnStepSafetyError(SafetyViolation(
                reason="clock_sync_confirmation_required",
                command="sync_onstep_clock",
                severity=SafetySeverity.UNKNOWN,
                recovery_hint="Confirm Raspberry time and SmartTScope location before syncing OnStep.",
            ))
        system_clock = self.read_system_clock_sanity()
        self._last_system_clock_check = system_clock
        if not bool(system_clock.get("valid")):
            self._safety_lock = SafetyViolation(
                reason="system_clock_invalid",
                command="sync_onstep_clock",
                severity=SafetySeverity.UNKNOWN,
                recovery_hint=(
                    "Raspberry time is not sane, so SmartTScope will not copy it into OnStep. "
                    "Wait for GPS/NTP or set/check the Pi time first."
                ),
            )
            raise OnStepSafetyError(self._safety_lock)

        before = self.read_onstep_clock()
        now = self._active_now_utc().astimezone()
        date_reply = self._send(now.strftime(":SC%m/%d/%y#"))
        time_reply = self._send(now.strftime(":SL%H:%M:%S#"))
        utc_offset_reply = None
        if sync_utc_offset:
            utc_offset = _format_onstep_utc_offset(now)
            utc_offset_reply = self._send(f":SG{utc_offset}#")
        if not date_reply.startswith("1") or not time_reply.startswith("1"):
            raise RuntimeError(
                f"OnStep rejected clock update: date={date_reply!r} time={time_reply!r}"
            )
        self._safety_config = replace(self._safety_config, time_trust_source="user_confirmed")
        self._last_clock_check = self.read_onstep_clock()
        self._last_sidereal_check = self.read_onstep_sidereal_consistency()
        self._apply_clock_safety_lock()
        self._persist_last_state(last_command="sync_onstep_clock", force=True)
        return {
            "ok": (
                not bool((self._last_clock_check or {}).get("warning"))
                and bool((self._last_sidereal_check or {}).get("ok"))
            ),
            "system_clock": self._last_system_clock_check,
            "before": before,
            "after": self._last_clock_check,
            "sidereal": self._last_sidereal_check,
            "date_reply": date_reply,
            "time_reply": time_reply,
            "utc_offset_reply": utc_offset_reply,
        }

    def update_observer_reference(
        self,
        *,
        lat: float,
        lon: float,
        alt_m: float = 0.0,
        time_offset_s: float = 0.0,
    ) -> None:
        self._safety_config = replace(
            self._safety_config,
            observer_lat=lat,
            observer_lon=lon,
            observer_alt_m=alt_m,
            time_offset_s=time_offset_s,
            time_trust_source=(
                "gps_runtime"
                if abs(time_offset_s) > 0.001
                else self._safety_config.time_trust_source
            ),
        )
        self.refresh_safety_state()

    def sync_onstep_time_location(
        self,
        *,
        lat: float,
        lon: float,
        alt_m: float = 0.0,
        utc_datetime: datetime | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, object]:
        if not confirmed_by_user:
            raise OnStepSafetyError(SafetyViolation(
                reason="site_sync_confirmation_required",
                command="sync_onstep_time_location",
                severity=SafetySeverity.UNKNOWN,
                recovery_hint="Confirm the GPS site/time before syncing OnStep.",
            ))
        self._last_system_clock_check = self.read_system_clock_sanity()
        if not bool(self._last_system_clock_check.get("valid")):
            self._safety_lock = SafetyViolation(
                reason="system_clock_invalid",
                command="sync_onstep_time_location",
                severity=SafetySeverity.UNKNOWN,
                recovery_hint=(
                    "Raspberry time is not sane, so it will not be copied into OnStep. "
                    "Wait for GPS/NTP or set/check the Pi time first."
                ),
            )
            raise OnStepSafetyError(self._safety_lock)
        source_utc = utc_datetime or self._active_now_utc()
        local_dt = source_utc.astimezone()
        date_reply = self._send(local_dt.strftime(":SC%m/%d/%y#"))
        time_reply = self._send(local_dt.strftime(":SL%H:%M:%S#"))
        utc_offset = _format_onstep_utc_offset(local_dt)
        utc_offset_reply = self._send(f":SG{utc_offset}#")
        lat_reply = self._send(f":St{_format_site_degrees(lat, 2)}#")
        # OnStep/LX200 longitude is west-positive; SmartTScope is east-positive.
        lon_reply = self._send(f":Sg{_format_site_degrees(-lon, 3)}#")
        if not all(
            str(r).startswith("1")
            for r in (date_reply, time_reply, utc_offset_reply, lat_reply, lon_reply)
        ):
            raise RuntimeError(
                "OnStep rejected site/time update: "
                f"date={date_reply!r} time={time_reply!r} utc_offset={utc_offset_reply!r} "
                f"lat={lat_reply!r} lon={lon_reply!r}"
            )
        time_offset_s = (
            (source_utc - datetime.now(timezone.utc)).total_seconds()
            if utc_datetime is not None
            else 0.0
        )
        self._safety_config = replace(
            self._safety_config,
            observer_lat=lat,
            observer_lon=lon,
            observer_alt_m=alt_m,
            time_offset_s=time_offset_s,
        )
        self._last_clock_check = self.read_onstep_clock()
        self._last_sidereal_check = self.read_onstep_sidereal_consistency()
        self._apply_clock_safety_lock()
        self._persist_last_state(last_command="sync_onstep_time_location", force=True)
        return {
            "ok": (
                not bool((self._last_clock_check or {}).get("warning"))
                and bool((self._last_sidereal_check or {}).get("ok"))
            ),
            "lat_reply": lat_reply,
            "lon_reply": lon_reply,
            "date_reply": date_reply,
            "time_reply": time_reply,
            "utc_offset": utc_offset,
            "utc_offset_reply": utc_offset_reply,
            "observer": {"lat": lat, "lon": lon, "alt_m": alt_m},
            "onstep_clock": self._last_clock_check,
            "onstep_sidereal_time": self._last_sidereal_check,
        }

    def _read_limit(self, cmd: str) -> float | None:
        reply = self._send(cmd)
        if not reply:
            return None
        try:
            return _parse_degrees(reply)
        except ValueError:
            _log.warning("OnStepMount: could not parse %s reply %r", cmd, reply)
            return None

    def _sync_configured_limits_to_onstep(self) -> None:
        cfg = self._safety_config
        if cfg.configured_horizon_limit_deg is not None:
            value = _format_limit_degrees(cfg.configured_horizon_limit_deg, signed=True)
            if self._send(f":Sh{value}#") != "1":
                _log.warning("OnStepMount: OnStep rejected horizon limit update :Sh%s#", value)
        if cfg.configured_overhead_limit_deg is not None:
            value = _format_limit_degrees(cfg.configured_overhead_limit_deg, signed=False)
            if self._send(f":So{value}#") != "1":
                _log.warning("OnStepMount: OnStep rejected overhead limit update :So%s#", value)

    def _inspect_status(self, status: str) -> None:
        self._last_status = status
        self._last_decoded_status = _decode_onstep_status(status)
        if "l" in status:
            self._lock_limit("status_limit", "status", onstep_reply=status)
        if self._last_decoded_status.get("park_failed"):
            self._invalidate_mechanical_trust("onstep_park_failed")
        if self._last_decoded_status.get("at_limit"):
            self._invalidate_mechanical_trust("onstep_at_limit")
        self._persist_last_state()

    def _persist_last_state(
        self,
        *,
        ra: float | None = None,
        dec: float | None = None,
        last_command: str | None = None,
        force: bool = False,
    ) -> None:
        try:
            if ra is not None:
                self._last_ra = ra
            if dec is not None:
                self._last_dec = dec
            self._state_store.maybe_save(
                {
                    "updated_at_utc": datetime.now(timezone.utc).isoformat(),
                    "port": self._port,
                    "parked": bool(self._last_decoded_status.get("parked")),
                    "tracking": bool(self._last_decoded_status.get("tracking")),
                    "slewing": bool(self._last_decoded_status.get("slewing")),
                    "at_limit": bool(self._last_decoded_status.get("at_limit")),
                    "home_confirmed": self._home_confirmed,
                    "home_reference_confirmed": self._home_confirmed,
                    "park_pose_confirmed": self._park_pose_confirmed,
                    "position_authority": self._position_authority(),
                    "mechanical_axis_position": self._mechanical_axis_position,
                    "mechanical_calibration_file": str(self._mechanical_calibration_path()),
                    "ra": self._last_ra,
                    "dec": self._last_dec,
                    "last_command": last_command,
                    "status_raw": self._last_status or None,
                    "status": self._last_decoded_status,
                    "safety_locked": self._safety_lock is not None,
                    "safety_reason": self._safety_lock.reason if self._safety_lock else None,
                    "onstep_limits": {
                        "horizon_deg": self._onstep_limits.horizon_deg,
                        "overhead_deg": self._onstep_limits.overhead_deg,
                    },
                },
                force=force,
            )
        except Exception as exc:
            _log.debug("OnStepMount: persisted state write skipped: %s", exc)

    def _lock_limit(
        self,
        reason: str,
        command: str,
        *,
        axis: str | None = None,
        current_value: float | int | None = None,
        target_value: float | int | None = None,
        limit_value: float | int | None = None,
        onstep_reply: str | None = None,
    ) -> None:
        self._safety_lock = SafetyViolation(
            reason=reason,
            command=command,
            severity=SafetySeverity.LIMIT_HIT,
            axis=axis,
            current_value=current_value,
            target_value=target_value,
            limit_value=limit_value,
            onstep_reply=onstep_reply,
            recovery_hint="Stop motion, return to HOME when safe, then re-confirm HOME/PARK.",
        )

    def _raise_if_locked(self, command: str, *, allow_clock_lock: bool = False) -> None:
        if self._safety_lock is None:
            return
        locked = self._safety_lock
        if allow_clock_lock and locked.reason in {"system_clock_invalid", "onstep_clock_invalid"}:
            return
        raise OnStepLimitError(SafetyViolation(
            reason=locked.reason,
            command=command,
            severity=locked.severity,
            axis=locked.axis,
            current_value=locked.current_value,
            target_value=locked.target_value,
            limit_value=locked.limit_value,
            onstep_reply=locked.onstep_reply,
            recovery_hint=locked.recovery_hint,
        ))

    def _raise_if_not_astronomy_ready(self, command: str) -> None:
        time_ready = self._time_readiness()
        location_ready = self._location_readiness()
        limit_ready = self._limit_readiness()
        checks = (
            ("time_not_trusted", time_ready),
            ("location_unverified", location_ready),
            ("limits_unverified", limit_ready),
        )
        for fallback_reason, readiness in checks:
            if readiness.get("ready"):
                continue
            reasons = readiness.get("reasons")
            reason = fallback_reason
            if isinstance(reasons, list) and reasons:
                reason = str(reasons[0])
            raise OnStepSafetyError(SafetyViolation(
                reason=reason,
                command=command,
                severity=SafetySeverity.UNKNOWN,
                recovery_hint=str(readiness.get("required_action") or "Resolve adapter readiness before astronomical motion."),
            ))

    def _compute_ha_altaz(self, ra: float, dec: float) -> tuple[float, float, float]:
        cfg = self._safety_config
        now = self._active_now_utc()
        lst_hours = _lst_hours(cfg.observer_lon, now)
        ha = ((lst_hours - ra + 12.0) % 24.0) - 12.0
        alt, az = _compute_altaz_stdlib_at(ra, dec, cfg.observer_lat, cfg.observer_lon, now)
        return ha, alt, az

    def _effective_min_altitude(self, az: float) -> float:
        values = [self._safety_config.min_alt_deg]
        if self._onstep_limits.horizon_deg is not None:
            values.append(self._onstep_limits.horizon_deg)
        if self._horizon is not None:
            values.append(self._horizon.min_alt_at(az))
        return max(values)

    def _target_safety_context(self, ra: float, dec: float, margin_deg: float = 0.0) -> dict[str, object]:
        ha, alt, az = self._compute_ha_altaz(ra, dec)
        cfg = self._safety_config
        meridian_flip_boundary_h = cfg.ha_west_limit_h - (cfg.meridian_margin_deg / 15.0)
        smart_min = cfg.min_alt_deg
        onstep_min = self._onstep_limits.horizon_deg
        horizon_min = self._horizon.min_alt_at(az) if self._horizon is not None else None
        smart_max, overhead_corridor = self._smart_max_altitude_at(az)
        effective_max = self._effective_max_altitude_at(az)
        min_values = [smart_min]
        if onstep_min is not None:
            min_values.append(onstep_min)
        if horizon_min is not None:
            min_values.append(horizon_min)
        return {
            "ra": ra,
            "dec": dec,
            "ha_hours": round(ha, 4),
            "alt_deg": round(alt, 2),
            "az_deg": round(az, 2),
            "limits": {
                "smart_min_alt_deg": smart_min,
                "onstep_horizon_deg": onstep_min,
                "horizon_file_min_alt_deg": round(horizon_min, 2) if horizon_min is not None else None,
                "effective_min_alt_deg": round(max(min_values) + margin_deg, 2),
                "smart_max_alt_deg": smart_max,
                "base_smart_max_alt_deg": cfg.max_alt_deg,
                "overhead_corridor": overhead_corridor,
                "onstep_overhead_deg": self._onstep_limits.overhead_deg,
                "effective_max_alt_deg": round(effective_max - margin_deg, 2),
                "ha_east_limit_h": cfg.ha_east_limit_h,
                "ha_west_limit_h": cfg.ha_west_limit_h,
                "meridian_h": 0.0,
                "meridian_flip_boundary_h": round(meridian_flip_boundary_h, 4),
                "meridian_margin_deg": cfg.meridian_margin_deg,
                "degrees_to_meridian": round(-ha * 15.0, 2),
                "degrees_to_west_stop": round((cfg.ha_west_limit_h - ha) * 15.0, 2),
                "degrees_to_flip_recommendation": round((meridian_flip_boundary_h - ha) * 15.0, 2),
                "dec_min_deg": cfg.dec_min_deg,
                "dec_max_deg": cfg.dec_max_deg,
                "margin_deg": margin_deg,
            },
        }

    def validate_target(self, ra: float, dec: float, margin_deg: float = 0.0) -> dict[str, object]:
        context = self._target_safety_context(ra, dec, margin_deg)
        try:
            self._check_target_safe(
                "validate",
                ra,
                dec,
                margin_deg=margin_deg,
                require_motion_preflight=False,
            )
        except OnStepSafetyError as exc:
            return {
                "allowed": False,
                "context": context,
                "violation": exc.violation.to_dict(),
            }
        return {
            "allowed": True,
            "context": context,
            "violation": None,
        }

    def target_context(self, ra: float, dec: float, margin_deg: float = 0.0) -> dict[str, object]:
        """Return computed HA/Alt/Az and limits without enforcing safety."""
        return self._target_safety_context(ra, dec, margin_deg)

    def _effective_max_altitude(self) -> float:
        """Return the strictest global max altitude, ignoring AZ corridors."""
        values = [self._safety_config.max_alt_deg]
        if self._onstep_limits.overhead_deg is not None:
            values.append(self._onstep_limits.overhead_deg)
        return min(values)

    def _smart_max_altitude_at(self, az: float) -> tuple[float, dict[str, float] | None]:
        for corridor in self._safety_config.safe_overhead_corridors:
            if corridor.contains(az):
                return corridor.max_alt_deg, {
                    "az_center_deg": corridor.az_center_deg,
                    "az_half_width_deg": corridor.az_half_width_deg,
                    "max_alt_deg": corridor.max_alt_deg,
                }
        return self._safety_config.max_alt_deg, None

    def _effective_max_altitude_at(self, az: float) -> float:
        smart_max, _ = self._smart_max_altitude_at(az)
        values = [smart_max]
        if self._onstep_limits.overhead_deg is not None:
            values.append(self._onstep_limits.overhead_deg)
        return min(values)

    def motion_safety_preflight(
        self,
        *,
        command: str,
        normal_motion: bool = True,
        margin_deg: float = 0.0,
    ) -> dict[str, object]:
        """Read one fresh logical/mechanical safety snapshot before motion."""
        sample_started = time.monotonic()
        sampled_at = datetime.now(timezone.utc)
        blockers: list[str] = []

        try:
            state = self.get_state()
        except Exception as exc:
            state = MountState.UNKNOWN
            blockers.append(f"onstep_status_unavailable:{exc}")
        decoded = dict(self._last_decoded_status or {})
        authority = self._mechanical_position_authority()
        at_home = bool(decoded.get("at_home") or self._at_mechanical_home)
        parked = state == MountState.PARKED or bool(decoded.get("parked"))
        terminal_state = parked or at_home

        try:
            direct_pier = self.read_pier_side()
        except Exception as exc:
            direct_pier = {"available": False, "value": None, "raw": "", "command": ":Gm#", "error": str(exc)}
        try:
            logical_axes = self.read_onstep_axis_position()
        except Exception as exc:
            logical_axes = {"available": False, "axis1_deg": None, "axis2_deg": None, "error": str(exc)}

        derived_axes: dict[str, object] | None = None
        if (
            self._home_confirmed
            and isinstance(logical_axes.get("axis1_deg"), (int, float))
            and isinstance(logical_axes.get("axis2_deg"), (int, float))
        ):
            derived_axes = _instrument_to_mount_axes(
                float(logical_axes["axis1_deg"]),
                float(logical_axes["axis2_deg"]),
                self._safety_config.observer_lat,
            )

        direct_pier_value = direct_pier.get("value")
        derived_pier_value = derived_axes.get("pier_side") if isinstance(derived_axes, dict) else None
        if direct_pier_value in {"east", "west"}:
            pier_side = str(direct_pier_value)
            pier_source = "onstep_gm"
        elif derived_pier_value in {"east", "west"}:
            pier_side = str(derived_pier_value)
            pier_source = "home_validated_logical_axes"
        else:
            pier_side = None
            pier_source = "unavailable"
        pier_consistent = not (
            direct_pier_value in {"east", "west"}
            and derived_pier_value in {"east", "west"}
            and direct_pier_value != derived_pier_value
        )
        if (
            (decoded.get("tracking") or state == MountState.TRACKING)
            and self._meridian_initial_pier_side is None
            and pier_side in {"east", "west"}
        ):
            self._meridian_initial_pier_side = pier_side

        position: dict[str, float] | None = None
        context: dict[str, object] | None = None
        ha_hours: float | None = None
        sidereal: dict[str, object] = {
            "available": False,
            "raw": "",
            "lst_hours": None,
            "source": "onstep_GS",
        }
        try:
            pos = self.get_position()
            position = {"ra": pos.ra, "dec": pos.dec}
            context = self._target_safety_context(pos.ra, pos.dec, margin_deg)
        except Exception as exc:
            blockers.append(f"logical_position_unavailable:{exc}")
        if position is not None:
            try:
                sidereal_reply = self._send(":GS#")
                onstep_lst_h = _parse_ra(sidereal_reply)
                ha_hours = ((onstep_lst_h - float(position["ra"]) + 12.0) % 24.0) - 12.0
                sidereal = {
                    "available": True,
                    "raw": sidereal_reply,
                    "lst_hours": round(onstep_lst_h, 6),
                    "source": "onstep_GS",
                    "raspberry_context_ha_hours": (
                        context.get("ha_hours") if isinstance(context, dict) else None
                    ),
                }
            except Exception as exc:
                blockers.append(f"onstep_sidereal_time_unavailable:{exc}")

        counterweight = _counterweight_safety_state(
            ha_hours=ha_hours,
            pier_side=pier_side,
            east_limit_h=self._safety_config.ha_east_limit_h,
            west_limit_h=self._safety_config.ha_west_limit_h,
            warning_margin_deg=self._safety_config.meridian_margin_deg,
            preflip_pier_side=self._meridian_initial_pier_side,
            terminal_state=terminal_state,
        )

        if authority.get("state") != "trusted":
            blockers.append("mechanical_position_authority_untrusted")
        if state == MountState.UNKNOWN:
            blockers.append("onstep_state_unknown")
        if decoded.get("park_failed"):
            blockers.append("onstep_park_failed")
        if decoded.get("at_limit"):
            blockers.append("onstep_at_limit")
        if not pier_consistent:
            blockers.append("pier_side_axis_inconsistent")
        if pier_side is None and not terminal_state:
            blockers.append("pier_side_unavailable")
        if ha_hours is None and not terminal_state:
            blockers.append("hour_angle_unavailable")
        if counterweight.get("hard_limit_reached"):
            blockers.append("counterweight_hard_limit")
        if normal_motion and parked:
            blockers.append("mount_parked")
        if normal_motion and state == MountState.SLEWING:
            blockers.append("mount_already_slewing")

        blockers = list(dict.fromkeys(blockers))
        mechanical_blockers = {
            "mechanical_position_authority_untrusted",
            "onstep_state_unknown",
            "onstep_park_failed",
            "onstep_at_limit",
            "pier_side_axis_inconsistent",
            "pier_side_unavailable",
            "hour_angle_unavailable",
            "counterweight_hard_limit",
        }
        mechanical_safe = not any(
            blocker in mechanical_blockers
            or blocker.startswith("onstep_status_unavailable:")
            or blocker.startswith("logical_position_unavailable:")
            for blocker in blockers
        )
        sample_age_ms = round((time.monotonic() - sample_started) * 1000.0, 3)
        motion_refused = bool(blockers)
        return {
            "command": command,
            "sampled_at_utc": sampled_at.isoformat(),
            "safety_sample_age_ms": sample_age_ms,
            "state": state.name.lower(),
            "tracking": bool(decoded.get("tracking") or state == MountState.TRACKING),
            "slewing": bool(decoded.get("slewing") or state == MountState.SLEWING),
            "parked": parked,
            "at_home": at_home,
            "mechanical_position_authority": authority,
            "mechanical_safe": mechanical_safe,
            "logical_position": position,
            "sidereal_time": sidereal,
            "logical_axis_position": logical_axes,
            "derived_mount_axes": derived_axes,
            "pier_side": {
                "value": pier_side,
                "source": pier_source,
                "direct": direct_pier,
                "consistent": pier_consistent,
            },
            "ha_hours": round(ha_hours, 4) if ha_hours is not None else None,
            "meridian_distance_deg": round(ha_hours * 15.0, 4) if ha_hours is not None else None,
            **counterweight,
            "capture_pause_required": bool(counterweight.get("hard_limit_reached")),
            "tracking_stop_required": bool(
                (decoded.get("tracking") or state == MountState.TRACKING)
                and counterweight.get("hard_limit_reached")
            ),
            "motion_refused": motion_refused,
            "motion_refusal_reason": blockers[0] if blockers else None,
            "blockers": blockers,
            "context": context,
        }

    def current_tracking_safety(self, margin_deg: float = 0.0) -> dict[str, object]:
        preflight = self.motion_safety_preflight(
            command="current_tracking_safety",
            normal_motion=False,
            margin_deg=margin_deg,
        )
        state_name = str(preflight.get("state") or "unknown")
        state = MountState[state_name.upper()] if state_name.upper() in MountState.__members__ else MountState.UNKNOWN
        decoded = dict(self._last_decoded_status or {})
        pier_side_info = preflight.get("pier_side")
        pier_side = pier_side_info.get("value") if isinstance(pier_side_info, dict) else None
        blockers = list(preflight.get("blockers") or [])
        onstep_limit_state = "ok"
        if decoded.get("park_failed"):
            onstep_limit_state = "park_failed"
        if decoded.get("at_limit"):
            onstep_limit_state = "at_limit"
        at_home = bool(preflight.get("at_home"))
        position = preflight.get("logical_position")
        validation: dict[str, object] | None = None
        context = preflight.get("context")
        if isinstance(position, dict):
            validation = self.validate_target(
                float(position["ra"]),
                float(position["dec"]),
                margin_deg=margin_deg,
            )
        astronomical_target_safe = bool(validation and validation.get("allowed"))
        violation = validation.get("violation") if isinstance(validation, dict) else None
        counterweight_state = preflight.get("counterweight_state")
        if (
            isinstance(violation, dict)
            and violation.get("reason") in {"hour_angle_east", "meridian_limit"}
            and counterweight_state == "normal"
        ):
            astronomical_target_safe = True
        mechanical_safe = bool(preflight.get("mechanical_safe"))
        tracking = bool(preflight.get("tracking"))
        meridian_flip_recommended = False
        ha_limit_state = "unknown"
        raw_ha_hours = preflight.get("ha_hours")
        ha_hours = float(raw_ha_hours) if isinstance(raw_ha_hours, (int, float)) else None
        flip_boundary_h: float | None = None
        tracking_action = "not_tracking"
        if ha_hours is not None:
            flip_boundary_h = self._safety_config.ha_west_limit_h - (self._safety_config.meridian_margin_deg / 15.0)
            postflip_confirmed = bool(
                self._meridian_flip_completed
                and self._meridian_postflip_pier_side in {"east", "west"}
                and pier_side == self._meridian_postflip_pier_side
            )
            if at_home:
                ha_limit_state = "not_applicable_at_home"
            elif counterweight_state == "hard_limit_reached":
                ha_limit_state = "west_stop_required" if ha_hours >= 0.0 else "east_stop_required"
            elif counterweight_state == "approaching_limit":
                ha_limit_state = "flip_recommended"
            elif postflip_confirmed:
                ha_limit_state = "postflip_tracking_allowed"
            elif ha_hours >= 0.0:
                ha_limit_state = "post_meridian_allowed"
            else:
                ha_limit_state = "pre_meridian_allowed"
            meridian_flip_recommended = bool(tracking and ha_limit_state == "flip_recommended")
        if at_home and not astronomical_target_safe:
            astronomical_target_safe = True
        if tracking and not at_home and not astronomical_target_safe:
            if isinstance(violation, dict) and violation.get("reason"):
                blockers.append(str(violation["reason"]))
            else:
                blockers.append("tracking_not_astronomically_safe")
        blockers = list(dict.fromkeys(blockers))
        tracking_stop_required = bool(
            preflight.get("tracking_stop_required")
            or (tracking and (blockers or not astronomical_target_safe))
        )
        tracking_safe_now = bool(tracking and mechanical_safe and astronomical_target_safe and not blockers)
        if tracking_stop_required:
            tracking_action = "stop_tracking"
        elif meridian_flip_recommended:
            tracking_action = "request_meridian_flip"
        elif tracking:
            tracking_action = "continue_tracking"
        extended_limits = self._onstep_extended_limits or self.read_onstep_extended_limits()
        operational_protection = _evaluate_onstep_operational_protection(
            state=state.name.lower(),
            at_home=at_home,
            pier_side=pier_side if isinstance(pier_side, str) else None,
            ha_hours=ha_hours,
            limits=extended_limits,
            flip_boundary_h=(
                flip_boundary_h
                if flip_boundary_h is not None
                else self._safety_config.ha_west_limit_h - (self._safety_config.meridian_margin_deg / 15.0)
            ),
            west_stop_h=self._safety_config.ha_west_limit_h,
        )
        return {
            "state": state.name.lower(),
            "tracking": tracking,
            "mechanical_safe": mechanical_safe,
            "astronomical_target_safe": astronomical_target_safe,
            "tracking_safe_now": tracking_safe_now,
            "tracking_stop_required": tracking_stop_required,
            "meridian_flip_recommended": meridian_flip_recommended,
            "tracking_action": tracking_action,
            "ha_limit_state": ha_limit_state,
            "ha_hours": round(ha_hours, 4) if ha_hours is not None else None,
            "ha_east_limit_h": self._safety_config.ha_east_limit_h,
            "ha_west_limit_h": self._safety_config.ha_west_limit_h,
            "meridian_flip_boundary_h": round(flip_boundary_h, 4) if flip_boundary_h is not None else None,
            "meridian_margin_deg": self._safety_config.meridian_margin_deg,
            "pier_side": preflight.get("pier_side"),
            "meridian_session": {
                "initial_pier_side": self._meridian_initial_pier_side,
                "postflip_pier_side": self._meridian_postflip_pier_side,
                "flip_completed": self._meridian_flip_completed,
                "selection_policy": "onstep_best_observed",
            },
            "onstep_limit_state": onstep_limit_state,
            "onstep_operational_protection": operational_protection,
            "blockers": blockers,
            "position": position,
            "context": context,
            "validation": validation,
            "mechanical_position_authority": preflight.get("mechanical_position_authority"),
            "counterweight_state": preflight.get("counterweight_state"),
            "counterweight_up": preflight.get("counterweight_up"),
            "meridian_distance_deg": preflight.get("meridian_distance_deg"),
            "operational_limit_margin_deg": preflight.get("operational_limit_margin_deg"),
            "limit_warning": preflight.get("limit_warning"),
            "capture_pause_required": preflight.get("capture_pause_required"),
            "motion_refused": preflight.get("motion_refused"),
            "motion_refusal_reason": preflight.get("motion_refusal_reason"),
            "safety_sample_age_ms": preflight.get("safety_sample_age_ms"),
            "motion_preflight": preflight,
            "diagnostic_note": (
                "RA/DEC/HA/ALT/AZ validation is diagnostic for HOME/PARK and authoritative for astronomical "
                "tracking/goto. At HOME, RA is not an operational target coordinate, so HA/meridian validation "
                "must not be used as a tracking stop reason."
            ),
        }

    def tracking_guard_tick(self, margin_deg: float = 0.0) -> dict[str, object]:
        safety = self.current_tracking_safety(margin_deg=margin_deg)
        stopped = False
        if safety.get("tracking_stop_required"):
            stopped = self.disable_tracking()
        safety["stop_sent"] = stopped
        safety["command"] = "tracking_guard_tick"
        return safety

    def controlled_meridian_flip(
        self,
        *,
        timeout_s: float = 120.0,
        poll_s: float = 1.0,
        ra_tolerance_h: float = 0.02,
        dec_tolerance_deg: float = 0.25,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> dict[str, object]:
        """Flip to the opposite GEM pier side by slewing to the current target.

        OnStepX ``:MN#`` commands a goto to the current RA/Dec on the opposite
        pier side. Completion is accepted only after ``:Gm#`` changes from the
        side selected by BEST to its opposite and the target is reacquired.
        """
        before_safety = self.current_tracking_safety(margin_deg=0.0)
        before_pier = before_safety.get("pier_side")
        before_pier_value = before_pier.get("value") if isinstance(before_pier, dict) else None
        if self.get_state() != MountState.TRACKING:
            raise OnStepSafetyError(SafetyViolation(
                reason="meridian_flip_requires_tracking",
                command="controlled_meridian_flip",
                severity=SafetySeverity.BLOCKED,
                recovery_hint="Start normal astronomical tracking before requesting a meridian flip.",
            ))
        if before_pier_value not in {"east", "west"}:
            raise OnStepSafetyError(SafetyViolation(
                reason="meridian_flip_requires_known_pier_side",
                command="controlled_meridian_flip",
                severity=SafetySeverity.BLOCKED,
                recovery_hint="Read a valid OnStep pier side before starting the controlled flip.",
            ))
        if self._meridian_initial_pier_side is None:
            self._meridian_initial_pier_side = str(before_pier_value)
        if before_pier_value != self._meridian_initial_pier_side:
            raise OnStepSafetyError(SafetyViolation(
                reason="meridian_flip_session_side_changed",
                command="controlled_meridian_flip",
                severity=SafetySeverity.BLOCKED,
                recovery_hint="Restart the meridian session because the live pier side changed unexpectedly.",
            ))
        expected_after_pier = "west" if before_pier_value == "east" else "east"
        if not before_safety.get("meridian_flip_recommended"):
            raise OnStepSafetyError(SafetyViolation(
                reason="meridian_flip_not_recommended",
                command="controlled_meridian_flip",
                severity=SafetySeverity.BLOCKED,
                recovery_hint="Wait until the configured flip boundary is reached.",
            ))

        target = self.get_position()
        disabled = self.disable_tracking_verified(timeout_s=8.0, poll_s=0.25, attempts=2)
        if not disabled.get("ok"):
            return {
                "ok": False,
                "reason": "tracking_disable_not_confirmed",
                "before_safety": before_safety,
                "disable_tracking": disabled,
            }

        try:
            flip_reply = self._bus.send_fixed(":MN#", size=1, timeout=3.0)
        except TimeoutError:
            return {
                "ok": False,
                "reason": "opposite_pier_goto_reply_timeout",
                "target": {"ra": target.ra, "dec": target.dec},
                "initial_pier_side": before_pier_value,
                "expected_pier_side": expected_after_pier,
                "before_safety": before_safety,
                "disable_tracking": disabled,
            }
        if flip_reply != "0":
            return {
                "ok": False,
                "reason": "opposite_pier_goto_rejected",
                "onstep_reply": flip_reply,
                "target": {"ra": target.ra, "dec": target.dec},
                "initial_pier_side": before_pier_value,
                "expected_pier_side": expected_after_pier,
                "before_safety": before_safety,
                "disable_tracking": disabled,
            }
        deadline = time.monotonic() + max(1.0, timeout_s)
        polls: list[dict[str, object]] = []
        stable = 0
        side_change_confirmed = False
        while time.monotonic() < deadline:
            state = self.get_state()
            pier = self.read_pier_side()
            position = self.get_position()
            ra_error = abs(((position.ra - target.ra + 12.0) % 24.0) - 12.0)
            dec_error = abs(position.dec - target.dec)
            poll = {
                "state": state.name.lower(),
                "status": self._last_status,
                "pier_side": pier,
                "ra": position.ra,
                "dec": position.dec,
                "ra_error_h": round(ra_error, 6),
                "dec_error_deg": round(dec_error, 4),
                "phase": "opposite_side_slew",
            }
            polls.append(poll)
            if progress_callback is not None:
                progress_callback(poll)
            settled = (
                state != MountState.SLEWING
                and pier.get("value") == expected_after_pier
            )
            stable = stable + 1 if settled else 0
            if stable >= 2:
                side_change_confirmed = True
                break
            time.sleep(max(0.1, poll_s))

        if not side_change_confirmed:
            self.stop()
            return {
                "ok": False,
                "reason": "opposite_pier_side_not_confirmed",
                "target": {"ra": target.ra, "dec": target.dec},
                "initial_pier_side": before_pier_value,
                "expected_pier_side": expected_after_pier,
                "before_safety": before_safety,
                "disable_tracking": disabled,
                "opposite_pier_goto": {"command": ":MN#", "reply": flip_reply},
                "polls": polls,
            }

        self._meridian_postflip_pier_side = expected_after_pier
        self._meridian_flip_completed = True
        tracking_enabled = self.enable_tracking()
        if not tracking_enabled:
            self._meridian_postflip_pier_side = None
            self._meridian_flip_completed = False
            return {
                "ok": False,
                "reason": "postflip_tracking_enable_failed",
                "target": {"ra": target.ra, "dec": target.dec},
                "initial_pier_side": before_pier_value,
                "expected_pier_side": expected_after_pier,
                "before_safety": before_safety,
                "disable_tracking": disabled,
                "opposite_pier_goto": {"command": ":MN#", "reply": flip_reply},
                "polls": polls,
            }

        preferred_before = self.read_onstep_preferred_pier_side()
        preferred_raw = str(preferred_before.get("raw") or "B").upper()
        if preferred_raw not in {"E", "W", "B", "A"}:
            preferred_raw = "B"
        forced_side_raw = "E" if expected_after_pier == "east" else "W"
        force_reply = self._bus.send_fixed(
            f":SX96,{forced_side_raw}#",
            size=1,
            timeout=2.0,
        )
        sr_reply = self._bus.send_fixed(
            f":Sr{_format_ra(target.ra)}#",
            size=1,
            timeout=2.0,
        )
        sd_reply = self._bus.send_fixed(
            f":Sd{_format_dec(target.dec)}#",
            size=1,
            timeout=2.0,
        )
        goto_reply = self._bus.send_fixed(":MS#", size=1, timeout=3.0)
        restore_reply = self._bus.send_fixed(
            f":SX96,{preferred_raw}#",
            size=1,
            timeout=2.0,
        )
        preferred_after_restore = self.read_onstep_preferred_pier_side()
        correction = {
            "command": ":MS#",
            "forced_preferred_pier_command": f":SX96,{forced_side_raw}#",
            "forced_preferred_pier_reply": force_reply,
            "set_ra_reply": sr_reply,
            "set_dec_reply": sd_reply,
            "goto_reply": goto_reply,
            "restore_preferred_pier_command": f":SX96,{preferred_raw}#",
            "restore_preferred_pier_reply": restore_reply,
            "preferred_pier_after_restore": preferred_after_restore,
        }
        if (
            force_reply != "1"
            or sr_reply != "1"
            or sd_reply != "1"
            or goto_reply != "0"
            or restore_reply != "1"
            or preferred_after_restore.get("raw") != preferred_raw
        ):
            self.stop()
            self._meridian_postflip_pier_side = None
            self._meridian_flip_completed = False
            return {
                "ok": False,
                "reason": "forced_side_target_correction_rejected",
                "target": {"ra": target.ra, "dec": target.dec},
                "initial_pier_side": before_pier_value,
                "expected_pier_side": expected_after_pier,
                "before_safety": before_safety,
                "disable_tracking": disabled,
                "opposite_pier_goto": {"command": ":MN#", "reply": flip_reply},
                "forced_side_correction": correction,
                "polls": polls,
            }

        stable = 0
        reacquired = False
        while time.monotonic() < deadline:
            state = self.get_state()
            pier = self.read_pier_side()
            position = self.get_position()
            ra_error = abs(((position.ra - target.ra + 12.0) % 24.0) - 12.0)
            dec_error = abs(position.dec - target.dec)
            poll = {
                "state": state.name.lower(),
                "status": self._last_status,
                "pier_side": pier,
                "ra": position.ra,
                "dec": position.dec,
                "ra_error_h": round(ra_error, 6),
                "dec_error_deg": round(dec_error, 4),
                "phase": "forced_side_target_reacquisition",
            }
            polls.append(poll)
            if progress_callback is not None:
                progress_callback(poll)
            settled = (
                state != MountState.SLEWING
                and pier.get("value") == expected_after_pier
                and ra_error <= ra_tolerance_h
                and dec_error <= dec_tolerance_deg
            )
            stable = stable + 1 if settled else 0
            if stable >= 2:
                reacquired = True
                break
            time.sleep(max(0.1, poll_s))

        if not reacquired:
            self.stop()
            self._meridian_postflip_pier_side = None
            self._meridian_flip_completed = False
            return {
                "ok": False,
                "reason": "postflip_target_not_reacquired",
                "target": {"ra": target.ra, "dec": target.dec},
                "initial_pier_side": before_pier_value,
                "expected_pier_side": expected_after_pier,
                "before_safety": before_safety,
                "disable_tracking": disabled,
                "opposite_pier_goto": {"command": ":MN#", "reply": flip_reply},
                "forced_side_correction": correction,
                "polls": polls,
            }

        tracking_enabled = self.enable_tracking()
        tracking_confirmed = False
        confirm_deadline = time.monotonic() + min(10.0, max(2.0, timeout_s))
        while tracking_enabled and time.monotonic() < confirm_deadline:
            state = self.get_state()
            if state == MountState.TRACKING and self.read_pier_side().get("value") == expected_after_pier:
                tracking_confirmed = True
                break
            time.sleep(max(0.1, poll_s))
        after_safety = self.current_tracking_safety(margin_deg=0.0)
        ok = bool(
            reacquired
            and tracking_confirmed
            and not after_safety.get("meridian_flip_recommended")
        )
        if not ok:
            self._meridian_postflip_pier_side = None
            self._meridian_flip_completed = False
        self._persist_last_state(last_command="controlled_meridian_flip", force=True)
        return {
            "ok": ok,
            "reason": None if ok else "postflip_tracking_or_guard_not_confirmed",
            "target": {"ra": target.ra, "dec": target.dec},
            "initial_pier_side": before_pier_value,
            "expected_pier_side": expected_after_pier,
            "before_safety": before_safety,
            "disable_tracking": disabled,
            "opposite_pier_goto": {"command": ":MN#", "reply": flip_reply},
            "forced_side_correction": correction,
            "polls": polls,
            "tracking_enable_ok": tracking_enabled,
            "tracking_confirmed": tracking_confirmed,
            "after_safety": after_safety,
        }

    def _check_target_safe(
        self,
        command: str,
        ra: float,
        dec: float,
        margin_deg: float = 0.0,
        *,
        require_motion_preflight: bool = True,
    ) -> None:
        self._raise_if_locked(command)
        if not self._home_confirmed:
            raise OnStepSafetyError(SafetyViolation(
                reason="home_not_confirmed",
                command=command,
                severity=SafetySeverity.UNKNOWN,
                recovery_hint="Confirm the mount is physically at HOME/PARK before enabling movement.",
            ))
        self._raise_if_not_astronomy_ready(command)
        if require_motion_preflight:
            preflight = self.motion_safety_preflight(
                command=command,
                normal_motion=True,
                margin_deg=margin_deg,
            )
            if preflight.get("motion_refused"):
                raise OnStepSafetyError(SafetyViolation(
                    reason=str(preflight.get("motion_refusal_reason") or "motion_preflight_failed"),
                    command=command,
                    severity=(
                        SafetySeverity.LIMIT_HIT
                        if preflight.get("counterweight_state") == "hard_limit_reached"
                        else SafetySeverity.BLOCKED
                    ),
                    current_value=preflight.get("meridian_distance_deg"),
                    limit_value=self._safety_config.ha_west_limit_h * 15.0,
                    recovery_hint=(
                        "Stop motion and use only an explicit HOME/PARK or recovery route "
                        "that moves the mount back toward safety."
                    ),
                ))
        ha, alt, az = self._compute_ha_altaz(ra, dec)
        cfg = self._safety_config
        min_alt = self._effective_min_altitude(az) + margin_deg
        max_alt = self._effective_max_altitude_at(az) - margin_deg
        if dec < cfg.dec_min_deg:
            raise OnStepSafetyError(SafetyViolation(
                reason="declination_min",
                command=command,
                axis="dec",
                target_value=round(dec, 4),
                limit_value=cfg.dec_min_deg,
                recovery_hint="Target is below the configured declination axis limit.",
            ))
        if dec > cfg.dec_max_deg:
            raise OnStepSafetyError(SafetyViolation(
                reason="declination_max",
                command=command,
                axis="dec",
                target_value=round(dec, 4),
                limit_value=cfg.dec_max_deg,
                recovery_hint="Target is above the configured declination axis limit.",
            ))
        if ha < cfg.ha_east_limit_h:
            raise OnStepSafetyError(SafetyViolation(
                reason="hour_angle_east",
                command=command,
                axis="ra",
                target_value=round(ha, 4),
                limit_value=cfg.ha_east_limit_h,
                recovery_hint="Choose a target inside the configured GEM hour-angle limits.",
            ))
        if ha >= cfg.ha_west_limit_h:
            raise OnStepSafetyError(SafetyViolation(
                reason="meridian_limit",
                command=command,
                axis="ra",
                target_value=round(ha, 4),
                limit_value=cfg.ha_west_limit_h,
                recovery_hint="Stop before the meridian limit and let SmartTScope control any flip workflow.",
            ))
        if alt < min_alt:
            raise OnStepSafetyError(SafetyViolation(
                reason="below_horizon",
                command=command,
                axis="altitude",
                target_value=round(alt, 2),
                limit_value=round(min_alt, 2),
                recovery_hint="Target is below the stricter of OnStep, SmartTScope, and horizon-profile limits.",
            ))
        if alt > max_alt:
            raise OnStepSafetyError(SafetyViolation(
                reason="overhead_limit",
                command=command,
                axis="altitude",
                target_value=round(alt, 2),
                limit_value=round(max_alt, 2),
                recovery_hint="Target is above the stricter OnStep/SmartTScope overhead limit.",
            ))

    def get_state(self) -> MountState:
        r = self._send(":GU#")
        if not r:
            return MountState.UNKNOWN
        self._inspect_status(r)
        decoded = self._last_decoded_status
        if decoded.get("parked"):
            return MountState.PARKED
        if decoded.get("slewing"):
            return MountState.SLEWING
        if decoded.get("at_limit"):
            return MountState.AT_LIMIT
        if decoded.get("tracking"):
            return MountState.TRACKING
        return MountState.UNPARKED

    def _wait_for_status_flag(
        self,
        flag: str,
        *,
        timeout_s: float = 45.0,
        poll_s: float = 2.0,
    ) -> tuple[bool, list[dict[str, object]], MountState]:
        polls: list[dict[str, object]] = []
        state = MountState.UNKNOWN
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            time.sleep(poll_s)
            state = self.get_state()
            decoded = dict(self._last_decoded_status or {})
            entry = {
                "elapsed_s": round(timeout_s - max(0.0, deadline - time.monotonic()), 1),
                "state": state.name.lower(),
                "status": self._last_status,
                flag: bool(decoded.get(flag)),
                "decoded": decoded,
            }
            polls.append(entry)
            if decoded.get(flag):
                if flag == "at_home":
                    self._at_mechanical_home = True
                    self.confirm_home_position()
                return True, polls, state
        return False, polls, state

    def unpark(self) -> bool:
        self._raise_if_locked("unpark", allow_clock_lock=True)
        try:
            reply = self._bus.send_fixed(":hR#", size=1, timeout=5.0)
        except TimeoutError as exc:
            raise RuntimeError("OnStep serial bus busy during unpark") from exc
        self._persist_last_state(last_command="unpark", force=True)
        if reply == "0":
            _log.warning("OnStepMount.unpark(): OnStep rejected :hR# with reply '0'")
            return False
        return True

    def recovery_unpark_stop_tracking(self) -> dict[str, object]:
        self._raise_if_locked("recovery_unpark_stop_tracking", allow_clock_lock=True)
        try:
            reply = self._bus.send_fixed(":hR#", size=1, timeout=5.0)
        except TimeoutError as exc:
            raise RuntimeError("OnStep serial bus busy during recovery unpark") from exc
        accepted = reply != "0"
        self._persist_last_state(last_command="recovery_unpark", force=True)
        time.sleep(0.2)
        state_after_unpark = self.get_state()
        tracking_disable_sent = False
        tracking_disable_ok: bool | None = None
        final_state = state_after_unpark
        if state_after_unpark == MountState.TRACKING:
            tracking_disable_sent = True
            tracking_disable_ok = self.disable_tracking()
            time.sleep(0.2)
            final_state = self.get_state()
        state_confirms_unparked = final_state not in {MountState.PARKED, MountState.TRACKING}
        ok = bool(state_confirms_unparked)
        if not accepted and state_confirms_unparked:
            _log.info(
                "OnStepMount.recovery_unpark_stop_tracking(): :hR# replied '0' but status confirms final_state=%s",
                final_state.name.lower(),
            )
        elif not accepted:
            _log.warning("OnStepMount.recovery_unpark_stop_tracking(): OnStep rejected :hR# with reply '0'")
        self._persist_last_state(last_command="recovery_unpark_stop_tracking", force=True)
        return {
            "ok": ok,
            "accepted": accepted,
            "onstep_reply": reply,
            "state_confirms_unparked": state_confirms_unparked,
            "state_after_unpark": state_after_unpark.name.lower(),
            "tracking_disable_sent": tracking_disable_sent,
            "tracking_disable_ok": tracking_disable_ok,
            "final_state": final_state.name.lower(),
        }

    def return_home_mechanical(self) -> dict[str, object]:
        """Command OnStep Find/Home as a watched mechanical route leg.

        This is intentionally not an astronomical goto and does not depend on
        time/location readiness. Callers must supervise and poll status.
        """
        self._raise_if_locked("return_home_mechanical", allow_clock_lock=True)
        state_before = self.get_state()
        tracking_disable_sent = False
        tracking_disable_ok: bool | None = None
        if state_before == MountState.PARKED:
            raise OnStepSafetyError(SafetyViolation(
                reason="unpark_before_return_home",
                command="return_home_mechanical",
                severity=SafetySeverity.BLOCKED,
                recovery_hint="Unpark and disable tracking before commanding the mechanical HOME route.",
            ))
        if state_before == MountState.TRACKING:
            tracking_disable_sent = True
            tracking_disable_ok = self.disable_tracking()
            if not tracking_disable_ok:
                raise OnStepSafetyError(SafetyViolation(
                    reason="disable_tracking_required",
                    command="return_home_mechanical",
                    severity=SafetySeverity.BLOCKED,
                    recovery_hint="Disable tracking before commanding the mechanical HOME route.",
                ))
        try:
            self._bus.write_no_reply(":hC#", timeout=0.05)
        except TimeoutError as exc:
            raise RuntimeError("OnStep serial bus busy during return_home_mechanical") from exc
        self._persist_last_state(last_command="return_home_mechanical", force=True)
        return {
            "ok": True,
            "command": ":hC#",
            "state_before": state_before.name.lower(),
            "tracking_disable_sent": tracking_disable_sent,
            "tracking_disable_ok": tracking_disable_ok,
        }

    def go_home(self) -> bool:
        """Start the mechanical OnStep HOME route.

        This compatibility method implements the MountPort command contract.
        It deliberately does not unpark automatically or wait for completion;
        callers that start PARKED or need status-confirmed completion must use
        ``unpark_to_home_stop_tracking()``.
        """
        result = self.return_home_mechanical()
        return bool(result.get("ok"))

    def unpark_to_home_stop_tracking(
        self,
        *,
        timeout_s: float = 45.0,
        poll_s: float = 2.0,
    ) -> dict[str, object]:
        """Resume from PARK, disable tracking, and route to mechanical HOME.

        Completion is based on OnStep's at_home status flag, not the intermittent
        slewing/not-slewing flag observed during mechanical HOME travel.
        """
        started = self.recovery_unpark_stop_tracking()
        home_command = self.return_home_mechanical()
        at_home, polls, final_state = self._wait_for_status_flag(
            "at_home",
            timeout_s=timeout_s,
            poll_s=poll_s,
        )
        if at_home:
            self._at_mechanical_home = True
            self._mechanical_axis_position = {"axis1_deg": 0.0, "axis2_deg": 0.0}
        self._persist_last_state(last_command="unpark_to_home_stop_tracking", force=True)
        return {
            "ok": bool(started.get("ok")) and bool(home_command.get("ok")) and at_home,
            "unpark": started,
            "home": home_command,
            "at_home": at_home,
            "polls": polls,
            "final_state": final_state.name.lower(),
            "final_status": self._last_status,
        }

    def enable_tracking(self) -> bool:
        pos = self.get_position()
        self._check_target_safe("enable_tracking", pos.ra, pos.dec, margin_deg=0.25)
        try:
            r = self._bus.send_fixed(":Te#", size=1, timeout=2.0)
        except TimeoutError as exc:
            raise RuntimeError("OnStep serial bus busy during enable_tracking") from exc
        ok = r == "1"
        if ok:
            if not self._meridian_flip_completed:
                self.begin_meridian_tracking_session()
            self._persist_last_state(last_command="enable_tracking", force=True)
        else:
            _log.warning("OnStepMount.enable_tracking(): OnStep rejected :Te# with reply %r", r)
        return ok

    def enable_tracking_for_autonomous_watch(self) -> dict[str, object]:
        """Enable OnStep tracking for a watched firmware-safety audit.

        This deliberately bypasses SmartTScope's RA/DEC/HA validation because
        the test is asking a different question: whether OnStep's own firmware
        limits/status react if the Raspberry side dies or stops supervising.
        It must only be used by watched diagnostic code after routing to HOME.
        """
        self._raise_if_locked("enable_tracking_for_autonomous_watch", allow_clock_lock=True)
        try:
            r = self._bus.send_fixed(":Te#", size=1, timeout=2.0)
        except TimeoutError as exc:
            raise RuntimeError("OnStep serial bus busy during enable_tracking_for_autonomous_watch") from exc
        ok = r == "1"
        if ok:
            self._persist_last_state(last_command="enable_tracking_for_autonomous_watch", force=True)
        else:
            _log.warning(
                "OnStepMount.enable_tracking_for_autonomous_watch(): OnStep rejected :Te# with reply %r",
                r,
            )
        return {
            "ok": ok,
            "reply": r,
            "command": ":Te#",
            "bypassed_smart_validation": True,
            "purpose": "watched OnStep firmware autonomous limit/status audit",
        }

    def get_position(self) -> MountPosition:
        ra = _parse_ra(self._send(":GR#"))
        dec = _parse_dec(self._send(":GD#"))
        self._persist_last_state(ra=ra, dec=dec)
        return MountPosition(ra=ra, dec=dec)

    def sync(self, ra: float, dec: float) -> bool:
        self._check_target_safe("sync", ra, dec)
        self._send(f":Sr{_format_ra(ra)}#")
        self._send(f":Sd{_format_dec(dec)}#")
        self._send(":CM#")
        return True

    def goto(self, ra: float, dec: float, *, preserve_meridian_session: bool = False) -> bool:
        self._check_target_safe("goto", ra, dec)
        if not preserve_meridian_session:
            self._meridian_initial_pier_side = None
            self._meridian_postflip_pier_side = None
            self._meridian_flip_completed = False
        self._at_mechanical_home = False
        self._send(f":Sr{_format_ra(ra)}#")
        self._send(f":Sd{_format_dec(dec)}#")
        resp = self._send(":MS#")
        if resp != "0":
            ms_codes = {
                "1": "below horizon limit",
                "2": "no object selected",
                "4": "position unreachable",
                "5": "not aligned",
                "6": "outside limits",
            }
            reason = ms_codes.get(resp, f"code {resp!r}")
            if resp in ("4", "6"):
                self._lock_limit(reason.replace(" ", "_"), "goto", onstep_reply=resp)
            _log.error(
                "OnStepMount.goto(): :MS# returned %r (%s) RA=%s Dec=%s",
                resp, reason, _format_ra(ra), _format_dec(dec),
            )
            raise OnStepLimitError(SafetyViolation(
                reason=reason.replace(" ", "_"),
                command="goto",
                severity=SafetySeverity.LIMIT_HIT if resp in ("4", "6") else SafetySeverity.BLOCKED,
                target_value=dec,
                onstep_reply=resp,
                recovery_hint="Read OnStep status and verify mount/home state before issuing more motion.",
            ))
        self._persist_last_state(ra=ra, dec=dec, last_command="goto", force=True)
        return True

    def recovery_offset(
        self,
        *,
        ra_offset_h: float = 0.0,
        dec_offset_deg: float = 0.0,
        max_ra_offset_h: float = 1.0,
        max_dec_offset_deg: float = 5.0,
    ) -> dict[str, object]:
        state = self.get_state()
        if state == MountState.PARKED:
            raise OnStepSafetyError(SafetyViolation(
                reason="unpark_before_recovery_offset",
                command="recovery_offset",
                severity=SafetySeverity.BLOCKED,
                recovery_hint="Run recovery_unpark_stop_tracking before a watched recovery offset.",
            ))
        if state == MountState.TRACKING:
            disabled = self.disable_tracking()
            if not disabled:
                raise OnStepSafetyError(SafetyViolation(
                    reason="disable_tracking_required",
                    command="recovery_offset",
                    severity=SafetySeverity.BLOCKED,
                    recovery_hint="Disable tracking before a watched recovery offset.",
                ))
            state = self.get_state()
        if state not in {MountState.UNPARKED, MountState.AT_LIMIT}:
            raise OnStepSafetyError(SafetyViolation(
                reason="recovery_offset_state_invalid",
                command="recovery_offset",
                severity=SafetySeverity.BLOCKED,
                recovery_hint="Recovery offset requires an unparked, non-tracking mount.",
            ))
        if abs(ra_offset_h) > max_ra_offset_h or abs(dec_offset_deg) > max_dec_offset_deg:
            raise OnStepSafetyError(SafetyViolation(
                reason="recovery_offset_too_large",
                command="recovery_offset",
                severity=SafetySeverity.BLOCKED,
                recovery_hint="Reduce the watched recovery offset or raise the explicit test cap.",
            ))
        if abs(ra_offset_h) < 1e-9 and abs(dec_offset_deg) < 1e-9:
            raise OnStepSafetyError(SafetyViolation(
                reason="recovery_offset_empty",
                command="recovery_offset",
                severity=SafetySeverity.BLOCKED,
                recovery_hint="Specify a non-zero RA or DEC recovery offset.",
            ))
        current = self.get_position()
        target_ra = (current.ra + ra_offset_h) % 24.0
        target_dec = max(-90.0, min(90.0, current.dec + dec_offset_deg))
        start_context = self._target_safety_context(current.ra, current.dec, 0.0)
        target_context = self._target_safety_context(target_ra, target_dec, 0.0)
        self._send(f":Sr{_format_ra(target_ra)}#")
        self._send(f":Sd{_format_dec(target_dec)}#")
        resp = self._send(":MS#")
        if resp != "0":
            raise OnStepLimitError(SafetyViolation(
                reason="recovery_offset_rejected",
                command="recovery_offset",
                severity=SafetySeverity.LIMIT_HIT if resp in ("4", "6") else SafetySeverity.BLOCKED,
                onstep_reply=resp,
                recovery_hint="OnStep rejected the watched recovery offset; stop and inspect status.",
            ))
        self._persist_last_state(ra=target_ra, dec=target_dec, last_command="recovery_offset", force=True)
        return {
            "ok": True,
            "onstep_reply": resp,
            "start": {"ra": current.ra, "dec": current.dec},
            "target": {"ra": target_ra, "dec": target_dec},
            "offset": {"ra_h": ra_offset_h, "dec_deg": dec_offset_deg},
            "start_context": start_context,
            "target_context": target_context,
            "state_before": state.name.lower(),
        }

    def is_slewing(self) -> bool:
        return "|" in self._send(":D#")

    def stop(self) -> None:
        self._bus.write_bypass(b":Q#")
        self._persist_last_state(last_command="stop", force=True)

    def park(self) -> bool:
        self._raise_if_locked("park", allow_clock_lock=True)
        try:
            reply = self._bus.send_fixed(":hP#", size=1, timeout=0.25)
        except TimeoutError as exc:
            _log.warning("OnStepMount.park(): timeout waiting for :hP# acknowledgement: %s", exc)
            self._persist_last_state(last_command="park_ack_timeout", force=True)
            return False
        ok = reply == "1"
        if not ok:
            _log.warning("OnStepMount.park(): OnStep did not accept :hP#; reply=%r", reply)
        if ok:
            self._at_mechanical_home = False
        self._persist_last_state(last_command="park", force=True)
        return ok

    def park_via_home(
        self,
        *,
        timeout_s: float = 45.0,
        poll_s: float = 2.0,
        home_park_settle_s: float | None = None,
    ) -> dict[str, object]:
        """Route to mechanical HOME, settle, then command PARK.

        This follows the field-proven sequence for this rig: HOME completion is
        proven by at_home, PARK is delayed briefly, and final completion is
        proven only by parked status.
        """
        data: dict[str, object] = {}
        state = self.get_state()
        data["initial_state"] = state.name.lower()
        data["initial_status"] = self._last_status
        if state == MountState.PARKED:
            return {
                "ok": True,
                "already_parked": True,
                "initial_state": state.name.lower(),
                "initial_status": self._last_status,
            }
        if not self._last_decoded_status.get("at_home"):
            if state == MountState.TRACKING:
                data["disable_tracking_ok"] = self.disable_tracking()
            home_command = self.return_home_mechanical()
            at_home, home_polls, home_state = self._wait_for_status_flag(
                "at_home",
                timeout_s=timeout_s,
                poll_s=poll_s,
            )
            data.update({
                "home": home_command,
                "at_home": at_home,
                "home_polls": home_polls,
                "home_final_state": home_state.name.lower(),
            })
            if not at_home:
                data.update({"ok": False, "reason": "home_route_timeout", "final_status": self._last_status})
                return data
        settle_s = self._safety_config.home_park_settle_s if home_park_settle_s is None else home_park_settle_s
        if settle_s > 0:
            time.sleep(settle_s)
        park_accepted = self.park()
        data["park_accepted"] = park_accepted
        parked, park_polls, park_state = self._wait_for_status_flag(
            "parked",
            timeout_s=timeout_s,
            poll_s=poll_s,
        )
        data.update({
            "ok": parked,
            "parked": parked,
            "park_polls": park_polls,
            "final_state": park_state.name.lower(),
            "final_status": self._last_status,
        })
        if parked and not park_accepted:
            data["status_overrode_park_ack"] = True
        if not parked and not park_accepted:
            data["reason"] = "park_not_accepted_or_timeout"
        self._persist_last_state(last_command="park_via_home", force=True)
        return data

    def get_park_position(self) -> MountPosition | None:
        record = self.get_stored_park_position()
        if record is None:
            return None
        return MountPosition(ra=record.ra, dec=record.dec)

    def disable_tracking_verified(
        self,
        *,
        timeout_s: float = 8.0,
        poll_s: float = 0.5,
        attempts: int = 2,
        send_stop: bool = True,
    ) -> dict[str, object]:
        """Disable tracking and trust live ``:GU#`` status, not the acknowledgement.

        ``:Td#`` replies only report command acceptance. Field testing also
        showed that a single accepted command can leave OnStep reporting
        tracking. A normal or emergency stop therefore consumes the one-byte
        reply, optionally sends ``:Q#``, and polls status until tracking is
        actually absent.
        """
        result: dict[str, object] = {
            "ok": False,
            "attempts": [],
            "polls": [],
            "final_state": MountState.UNKNOWN.name.lower(),
            "final_status": self._last_status,
        }
        attempt_count = max(1, attempts)
        per_attempt_timeout = max(poll_s, timeout_s / attempt_count)
        for attempt_number in range(1, attempt_count + 1):
            if send_stop:
                self._bus.write_bypass(b":Q#")
            try:
                reply = self._bus.send_fixed(":Td#", size=1, timeout=2.0)
            except TimeoutError as exc:
                reply = "timeout"
                _log.warning(
                    "OnStepMount.disable_tracking_verified(): serial busy on attempt %s",
                    attempt_number,
                )
                if attempt_number == attempt_count:
                    result["error"] = str(exc)
            attempt = {
                "attempt": attempt_number,
                "reply": reply,
                "stop_sent": send_stop,
            }
            cast_attempts = result["attempts"]
            assert isinstance(cast_attempts, list)
            cast_attempts.append(attempt)

            deadline = time.monotonic() + per_attempt_timeout
            while time.monotonic() < deadline:
                try:
                    state = self.get_state()
                except Exception as exc:
                    cast_polls = result["polls"]
                    assert isinstance(cast_polls, list)
                    cast_polls.append({
                        "attempt": attempt_number,
                        "state": "unknown",
                        "status": self._last_status,
                        "tracking": True,
                        "error": str(exc),
                    })
                    time.sleep(max(0.05, poll_s))
                    continue
                decoded = dict(self._last_decoded_status or {})
                poll = {
                    "attempt": attempt_number,
                    "state": state.name.lower(),
                    "status": self._last_status,
                    "tracking": bool(decoded.get("tracking")),
                    "decoded": decoded,
                }
                cast_polls = result["polls"]
                assert isinstance(cast_polls, list)
                cast_polls.append(poll)
                result.update({
                    "final_state": state.name.lower(),
                    "final_status": self._last_status,
                    "tracking": bool(decoded.get("tracking")),
                })
                if not decoded.get("tracking"):
                    result["ok"] = True
                    result["status_overrode_ack"] = reply != "1"
                    self._persist_last_state(last_command="disable_tracking_verified", force=True)
                    return result
                time.sleep(max(0.05, poll_s))

        _log.error(
            "OnStepMount.disable_tracking_verified(): tracking remains active; status=%r attempts=%r",
            self._last_status,
            result["attempts"],
        )
        self._persist_last_state(last_command="disable_tracking_failed", force=True)
        return result

    def disable_tracking(self) -> bool:
        return bool(self.disable_tracking_verified().get("ok"))

    def _normalized_axis_direction(
        self,
        *,
        axis: Literal["ra", "dec"],
        direction: str,
    ) -> Literal["e", "w", "n", "s"]:
        value = direction.lower().strip()
        aliases = {
            "east": "e",
            "west": "w",
            "north": "n",
            "south": "s",
        }
        value = aliases.get(value, value)
        allowed = {"e", "w"} if axis == "ra" else {"n", "s"}
        if value not in allowed:
            raise ValueError(f"invalid {axis.upper()} direction: {direction!r}")
        return value  # type: ignore[return-value]

    def _motion_rate(
        self,
        *,
        mode: Literal["guide", "center"],
        axis: Literal["ra", "dec"],
        direction: Literal["e", "w", "n", "s"],
    ) -> float | None:
        if self._motion_calibration is None:
            return None
        rate = self._motion_calibration.rate_for(
            mode=mode,
            axis=axis,
            direction=direction,
        )
        if not math.isfinite(rate) or rate <= 0.0:
            raise ValueError(f"invalid {mode} {axis} {direction} calibration rate: {rate!r}")
        return rate

    def _project_axis_motion(
        self,
        *,
        position: MountPosition,
        axis: Literal["ra", "dec"],
        signed_arcsec: float,
    ) -> MountPosition:
        if axis == "dec":
            return MountPosition(
                ra=position.ra,
                dec=position.dec + signed_arcsec / 3600.0,
            )
        cos_dec = math.cos(math.radians(position.dec))
        if abs(cos_dec) < 0.01:
            raise OnStepSafetyError(SafetyViolation(
                reason="ra_offset_projection_unstable_near_pole",
                command="move_ra",
                severity=SafetySeverity.BLOCKED,
                current_value=position.dec,
                recovery_hint="Use timed guiding with direct image feedback near the celestial pole.",
            ))
        ra_delta_h = signed_arcsec / (15.0 * 3600.0 * cos_dec)
        return MountPosition(
            ra=(position.ra + ra_delta_h) % 24.0,
            dec=position.dec,
        )

    def _axis_motion(
        self,
        *,
        axis: Literal["ra", "dec"],
        direction: str,
        duration_ms: int,
        mode: Literal["guide", "center"],
        requested_arcsec: float | None,
        cancel_check: Callable[[], bool] | None,
    ) -> AxisMotionResult:
        if mode not in {"guide", "center"}:
            raise ValueError(f"invalid axis-motion mode: {mode!r}")
        d = self._normalized_axis_direction(axis=axis, direction=direction)
        duration = int(duration_ms)
        maximum = 16399 if mode == "guide" else 120000
        if duration < 20 or duration > maximum:
            raise ValueError(f"{mode} duration must be between 20 and {maximum} ms")
        if not self._axis_motion_lock.acquire(blocking=False):
            raise OnStepSafetyError(SafetyViolation(
                reason="axis_motion_already_in_progress",
                command=f"move_{axis}_timed",
                severity=SafetySeverity.BLOCKED,
            ))

        commands: list[str] = []
        before: MountPosition | None = None
        tracking_before = False
        cancelled = False
        hard_limit_violation: SafetyViolation | None = None
        rate_selected = False
        motion_started = False
        try:
            preflight = self.motion_safety_preflight(
                command=f"move_{axis}_{mode}",
                normal_motion=True,
                margin_deg=0.25,
            )
            if preflight.get("motion_refused"):
                raise OnStepSafetyError(SafetyViolation(
                    reason=str(preflight.get("motion_refusal_reason") or "axis_motion_preflight_failed"),
                    command=f"move_{axis}_{mode}",
                    severity=SafetySeverity.BLOCKED,
                    recovery_hint="Resolve the fresh OnStep motion-preflight blockers before correction.",
                ))
            if preflight.get("at_home"):
                raise OnStepSafetyError(SafetyViolation(
                    reason="axis_motion_refused_at_home",
                    command=f"move_{axis}_{mode}",
                    severity=SafetySeverity.BLOCKED,
                    recovery_hint="Acquire a real astronomical target before guide or centering corrections.",
                ))
            tracking_before = bool(preflight.get("tracking"))
            if mode == "guide" and not tracking_before:
                raise OnStepSafetyError(SafetyViolation(
                    reason="guide_requires_tracking",
                    command=f"move_{axis}_guide",
                    severity=SafetySeverity.BLOCKED,
                ))
            logical = preflight.get("logical_position")
            if not isinstance(logical, dict):
                raise OnStepSafetyError(SafetyViolation(
                    reason="logical_position_unavailable",
                    command=f"move_{axis}_{mode}",
                    severity=SafetySeverity.UNKNOWN,
                ))
            before = MountPosition(ra=float(logical["ra"]), dec=float(logical["dec"]))

            rate = self._motion_rate(mode=mode, axis=axis, direction=d)
            projected_arcsec = requested_arcsec
            if projected_arcsec is None and rate is not None:
                sign = 1.0 if d in {"e", "n"} else -1.0
                projected_arcsec = sign * rate * duration / 1000.0
            if projected_arcsec is not None:
                target = self._project_axis_motion(
                    position=before,
                    axis=axis,
                    signed_arcsec=projected_arcsec,
                )
                target_validation = self.validate_target(target.ra, target.dec, margin_deg=0.25)
                if not target_validation.get("allowed"):
                    violation = target_validation.get("violation")
                    reason = (
                        str(violation.get("reason"))
                        if isinstance(violation, dict)
                        else "projected_axis_motion_unsafe"
                    )
                    raise OnStepSafetyError(SafetyViolation(
                        reason=reason,
                        command=f"move_{axis}_{mode}",
                        severity=SafetySeverity.BLOCKED,
                        recovery_hint="Reduce or reverse the requested correction.",
                    ))

            rate_command = ":RG#" if mode == "guide" else ":RC#"
            self._bus.write_no_reply(rate_command, timeout=0.5)
            commands.append(rate_command)
            rate_selected = True
            if mode == "guide":
                move_command = f":Mg{d}{duration:04d}#"
            else:
                move_command = f":M{d}#"
            self._bus.write_no_reply(move_command, timeout=0.5)
            commands.append(move_command)
            motion_started = True

            deadline = time.monotonic() + duration / 1000.0
            next_safety_poll = time.monotonic() + 0.25
            while time.monotonic() < deadline:
                if cancel_check is not None and cancel_check():
                    cancelled = True
                    break
                now = time.monotonic()
                if now >= next_safety_poll:
                    live = self.motion_safety_preflight(
                        command=f"move_{axis}_{mode}_live",
                        normal_motion=False,
                        margin_deg=0.0,
                    )
                    if live.get("tracking_stop_required") or live.get("hard_limit_reached"):
                        hard_limit_violation = SafetyViolation(
                            reason="axis_motion_reached_hard_limit",
                            command=f"move_{axis}_{mode}",
                            severity=SafetySeverity.LIMIT_HIT,
                            recovery_hint="Only explicit recovery motion toward safety remains allowed.",
                        )
                        break
                    next_safety_poll = now + 0.25
                time.sleep(min(0.05, max(0.0, deadline - now)))
        finally:
            if motion_started:
                try:
                    stop_command = f":Q{d}#"
                    self._bus.write_no_reply(stop_command, timeout=0.5)
                    commands.append(stop_command)
                except Exception:
                    pass
            if hard_limit_violation is not None:
                try:
                    self._bus.send_fixed(":Td#", size=1, timeout=0.5)
                    commands.append(":Td#")
                except Exception:
                    pass
                self._bus.write_bypass(b":Q#")
                commands.append(":Q#")
            if rate_selected:
                try:
                    self._bus.write_no_reply(":RG#", timeout=0.5)
                    commands.append(":RG#")
                except Exception:
                    pass
            self._axis_motion_lock.release()

        if hard_limit_violation is not None:
            raise OnStepLimitError(hard_limit_violation)
        assert before is not None
        try:
            after = self.get_position()
            after_state = self.get_state()
            tracking_after: bool | None = (
                after_state == MountState.TRACKING
                or bool(self._last_decoded_status.get("tracking"))
            )
        except Exception:
            after = None
            tracking_after = None
        result = AxisMotionResult(
            ok=not cancelled,
            axis=axis,
            direction=d,
            mode=mode,
            requested_arcsec=requested_arcsec,
            estimated_duration_ms=duration,
            commands_sent=tuple(commands),
            before_ra=before.ra,
            before_dec=before.dec,
            after_ra=after.ra if after is not None else None,
            after_dec=after.dec if after is not None else None,
            tracking_before=tracking_before,
            tracking_after=tracking_after,
            cancelled=cancelled,
        )
        self._persist_last_state(
            last_command=f"move_{axis}_{mode}_{d}_{duration}ms",
            force=True,
        )
        return result

    def move_ra_timed(
        self,
        direction: Literal["east", "west", "e", "w"],
        duration_ms: int,
        *,
        mode: Literal["guide", "center"] = "center",
        cancel_check: Callable[[], bool] | None = None,
    ) -> AxisMotionResult:
        return self._axis_motion(
            axis="ra",
            direction=direction,
            duration_ms=duration_ms,
            mode=mode,
            requested_arcsec=None,
            cancel_check=cancel_check,
        )

    def move_dec_timed(
        self,
        direction: Literal["north", "south", "n", "s"],
        duration_ms: int,
        *,
        mode: Literal["guide", "center"] = "center",
        cancel_check: Callable[[], bool] | None = None,
    ) -> AxisMotionResult:
        return self._axis_motion(
            axis="dec",
            direction=direction,
            duration_ms=duration_ms,
            mode=mode,
            requested_arcsec=None,
            cancel_check=cancel_check,
        )

    def move_ra(
        self,
        offset_arcsec: float,
        *,
        mode: Literal["guide", "center"] = "center",
        cancel_check: Callable[[], bool] | None = None,
    ) -> AxisMotionResult:
        if not math.isfinite(offset_arcsec) or offset_arcsec == 0.0:
            raise ValueError("RA offset_arcsec must be finite and non-zero")
        direction = "e" if offset_arcsec > 0 else "w"
        rate = self._motion_rate(mode=mode, axis="ra", direction=direction)
        if rate is None:
            raise ValueError("motion calibration is required for angular RA offsets")
        duration = round(abs(offset_arcsec) / rate * 1000.0)
        return self._axis_motion(
            axis="ra",
            direction=direction,
            duration_ms=duration,
            mode=mode,
            requested_arcsec=float(offset_arcsec),
            cancel_check=cancel_check,
        )

    def move_dec(
        self,
        offset_arcsec: float,
        *,
        mode: Literal["guide", "center"] = "center",
        cancel_check: Callable[[], bool] | None = None,
    ) -> AxisMotionResult:
        if not math.isfinite(offset_arcsec) or offset_arcsec == 0.0:
            raise ValueError("DEC offset_arcsec must be finite and non-zero")
        direction = "n" if offset_arcsec > 0 else "s"
        rate = self._motion_rate(mode=mode, axis="dec", direction=direction)
        if rate is None:
            raise ValueError("motion calibration is required for angular DEC offsets")
        duration = round(abs(offset_arcsec) / rate * 1000.0)
        return self._axis_motion(
            axis="dec",
            direction=direction,
            duration_ms=duration,
            mode=mode,
            requested_arcsec=float(offset_arcsec),
            cancel_check=cancel_check,
        )

    def guide(self, direction: str, duration_ms: int) -> bool:
        d = direction.lower()
        if d not in ("n", "s", "e", "w"):
            return False
        axis: Literal["ra", "dec"] = "ra" if d in {"e", "w"} else "dec"
        try:
            result = self._axis_motion(
                axis=axis,
                direction=d,
                duration_ms=duration_ms,
                mode="guide",
                requested_arcsec=None,
                cancel_check=None,
            )
        except (ValueError, OnStepSafetyError):
            return False
        return result.ok

    def recovery_pulse(self, direction: str, duration_ms: int) -> bool:
        self._raise_if_locked("recovery_pulse", allow_clock_lock=True)
        state = self.get_state()
        if state == MountState.PARKED:
            raise OnStepSafetyError(SafetyViolation(
                reason="unpark_before_recovery_pulse",
                command="recovery_pulse",
                severity=SafetySeverity.BLOCKED,
                recovery_hint="Unpark first, keep tracking disabled, and watch the mount before recovery pulses.",
            ))
        if state == MountState.TRACKING and not self.disable_tracking():
            raise OnStepSafetyError(SafetyViolation(
                reason="disable_tracking_required",
                command="recovery_pulse",
                severity=SafetySeverity.BLOCKED,
                recovery_hint="Disable tracking before issuing watched recovery pulses.",
            ))
        d = direction.lower()
        if d not in ("n", "s", "e", "w"):
            return False
        ms = max(50, min(2000, duration_ms))
        try:
            self._bus.write_no_reply(f":Mg{d}{ms:04d}#", timeout=0.5)
        except TimeoutError as exc:
            raise RuntimeError("OnStep serial bus busy during recovery pulse") from exc
        self._persist_last_state(last_command=f"recovery_pulse_{d}", force=True)
        return True

    def mechanical_manual_move(
        self,
        direction: str,
        duration_ms: int,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        """Run a watched manual normal-guide move for mechanical calibration.

        This is intentionally separate from recovery_pulse(): recovery pulses
        use OnStep pulse-guide and are capped small, while mechanical limit
        probing needs visible operator-supervised movement. Callers must keep
        line of sight and provide an emergency-stop path.
        """
        self._raise_if_locked("mechanical_manual_move", allow_clock_lock=True)
        state = self.get_state()
        if state == MountState.PARKED:
            raise OnStepSafetyError(SafetyViolation(
                reason="unpark_before_mechanical_manual_move",
                command="mechanical_manual_move",
                severity=SafetySeverity.BLOCKED,
                recovery_hint="Route to HOME/unparked before manual mechanical movement.",
            ))
        if state == MountState.TRACKING and not self.disable_tracking():
            raise OnStepSafetyError(SafetyViolation(
                reason="disable_tracking_required",
                command="mechanical_manual_move",
                severity=SafetySeverity.BLOCKED,
                recovery_hint="Disable tracking before manual mechanical movement.",
            ))
        d = direction.lower()
        if d not in ("n", "s", "e", "w"):
            return {"ok": False, "reason": "invalid_direction", "direction": direction}
        self._at_mechanical_home = False
        ms = max(50, min(120000, int(duration_ms)))
        interrupted = False
        try:
            self._bus.write_no_reply(f":M{d}#", timeout=0.5)
            deadline = time.monotonic() + (ms / 1000.0)
            while time.monotonic() < deadline:
                if cancel_check is not None and cancel_check():
                    interrupted = True
                    break
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        except TimeoutError as exc:
            raise RuntimeError("OnStep serial bus busy during mechanical_manual_move") from exc
        finally:
            try:
                self._bus.write_no_reply(f":Q{d}#", timeout=0.5)
            except Exception:
                pass
            self.stop()
        self._persist_last_state(last_command=f"mechanical_manual_move_{d}_{ms}ms", force=True)
        return {
            "ok": not interrupted,
            "direction": d,
            "duration_ms": ms,
            "mode": "normal_guide",
            "start_command": f":M{d}#",
            "stop_command": f":Q{d}# then :Q#",
            "interrupted": interrupted,
        }

    def start_alignment(self, num_stars: int) -> bool:
        n = max(1, min(9, num_stars))
        return self._send(f":A{n}#") == "1"

    def accept_alignment_star(self) -> bool:
        return self._send(":A+#") == "1"

    def save_alignment(self) -> bool:
        return self._send(":AW#") == "1"
