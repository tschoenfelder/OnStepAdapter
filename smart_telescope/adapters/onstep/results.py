"""Structured public results for the OnStep hardware adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class OnStepConnectionResult:
    connected: bool
    mount_connected: bool
    focuser_available: bool
    port: str


@dataclass(frozen=True)
class FocuserStatus:
    available: bool
    position: int
    max_position: int
    moving: bool


@dataclass(frozen=True)
class FocuserMoveResult:
    accepted: bool
    target_position: int
    start_position: int
    onstep_reply: str


@dataclass(frozen=True)
class StoredParkPosition:
    ra: float
    dec: float
    axis1_deg: float | None
    axis2_deg: float | None
    pier_side: str | None
    captured_at_utc: str
    firmware_product: str | None
    firmware_version: str | None
    firmware_date: str | None
    home_authority_state: str
    source: str = "captured_when_set"
    controller_readback_supported: bool = False
    controller_match: str = "unverifiable"
    trusted: bool = False
    invalidation_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class SetParkPositionResult:
    ok: bool
    controller_updated: bool
    local_record_persisted: bool
    onstep_reply: str
    record: StoredParkPosition | None
    error: str | None = None


@dataclass(frozen=True)
class OnStepMotionCalibration:
    guide_ra_east_arcsec_per_s: float
    guide_ra_west_arcsec_per_s: float
    guide_dec_north_arcsec_per_s: float
    guide_dec_south_arcsec_per_s: float
    center_ra_east_arcsec_per_s: float
    center_ra_west_arcsec_per_s: float
    center_dec_north_arcsec_per_s: float
    center_dec_south_arcsec_per_s: float

    def rate_for(
        self,
        *,
        mode: Literal["guide", "center"],
        axis: Literal["ra", "dec"],
        direction: Literal["e", "w", "n", "s"],
    ) -> float:
        key = f"{mode}_{axis}_{direction}"
        mapping = {
            "guide_ra_e": self.guide_ra_east_arcsec_per_s,
            "guide_ra_w": self.guide_ra_west_arcsec_per_s,
            "guide_dec_n": self.guide_dec_north_arcsec_per_s,
            "guide_dec_s": self.guide_dec_south_arcsec_per_s,
            "center_ra_e": self.center_ra_east_arcsec_per_s,
            "center_ra_w": self.center_ra_west_arcsec_per_s,
            "center_dec_n": self.center_dec_north_arcsec_per_s,
            "center_dec_s": self.center_dec_south_arcsec_per_s,
        }
        return float(mapping[key])


@dataclass(frozen=True)
class AxisMotionResult:
    ok: bool
    axis: Literal["ra", "dec"]
    direction: Literal["e", "w", "n", "s"]
    mode: Literal["guide", "center"]
    requested_arcsec: float | None
    estimated_duration_ms: int
    commands_sent: tuple[str, ...]
    before_ra: float
    before_dec: float
    after_ra: float | None
    after_dec: float | None
    tracking_before: bool
    tracking_after: bool | None
    cancelled: bool
    verification_required: bool = True
    error: str | None = None
