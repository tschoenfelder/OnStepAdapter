"""Bounded pole-aware terrestrial motion through standard INDI properties."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import threading
import time
from typing import Callable, Literal

from .indi_config import IndiRuntimeConfig
from .indi_mode import IndiModeController, IndiOperatingMode
from .indi_stop import IndiStopResult
from .indi_transport import IndiTransport


@dataclass(frozen=True)
class IndiTerrestrialMoveResult:
    mode: IndiOperatingMode
    state: str
    requested_deg: float
    commanded_deg: float
    measured_deg: float
    clipped: bool
    pole_branch: str
    indi_direction: str
    start_altitude_deg: float
    final_altitude_deg: float | None
    stop_confirmed: bool
    endpoint_is_estimated: bool = True


class IndiTerrestrialController:
    """Manual north-axis stepping with feedback, early stop and settle hold."""

    def __init__(
        self,
        transport: IndiTransport,
        config: IndiRuntimeConfig,
        mode: IndiModeController,
        *,
        observe: Callable,
        stop: Callable[..., IndiStopResult],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.transport = transport
        self.config = config
        self.mode = mode
        self.observe = observe
        self.stop = stop
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._park_reference: tuple[float, float, datetime] | None = None

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def capture_park_reference(self, snapshot) -> None:
        if (
            not snapshot.parked or not snapshot.status_live or
            not snapshot.coordinates_live or
            not snapshot.park_evidence_consistent or
            snapshot.ra_hours is None or snapshot.dec_deg is None
        ):
            raise RuntimeError("A fresh reconciled PARKED coordinate sample is required")
        self._park_reference = (snapshot.ra_hours, snapshot.dec_deg, self.clock())

    def move_toward_north_horizon(
        self, step_deg: float | None = None, *, timeout_s: float = 45.0,
        poll_s: float = 0.1,
    ) -> IndiTerrestrialMoveResult:
        return self._move("toward", step_deg, timeout_s=timeout_s, poll_s=poll_s)

    def move_away_from_north_horizon(
        self, step_deg: float | None = None, *, timeout_s: float = 45.0,
        poll_s: float = 0.1,
    ) -> IndiTerrestrialMoveResult:
        return self._move("away", step_deg, timeout_s=timeout_s, poll_s=poll_s)

    def _move(
        self, intent: Literal["toward", "away"], step_deg: float | None,
        *, timeout_s: float, poll_s: float,
    ) -> IndiTerrestrialMoveResult:
        requested = self.config.terrestrial_step_deg if step_deg is None else float(step_deg)
        if not math.isfinite(requested) or not 0 < requested <= self.config.terrestrial_max_step_deg:
            raise ValueError("Terrestrial step must be finite, positive and within max_step_deg")
        if timeout_s <= 0 or poll_s <= 0:
            raise ValueError("Terrestrial timeout and polling interval must be positive")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Another terrestrial motion is active or stopping")
        motion_started = False
        try:
            self.mode.require(IndiOperatingMode.TERRESTRIAL)
            start = self.observe()
            self._validate_start(start)
            branch = self._pole_branch(start)
            if branch == "ambiguous":
                raise RuntimeError("Pole branch is ambiguous; terrestrial motion refused")
            floor = self.config.movement_min_alt_deg + self.config.terrestrial_horizon_margin_deg
            if intent == "toward":
                available = start.altitude_deg - floor
                if available <= 0:
                    raise RuntimeError("Northern-horizon safety floor is already reached")
                commanded = min(requested, available)
            else:
                commanded = requested
            if commanded <= self.config.terrestrial_brake_lead_deg:
                raise RuntimeError("Remaining safe travel is below the braking allowance")
            north = (intent == "toward") == (branch == "near")
            element = "MOTION_NORTH" if north else "MOTION_SOUTH"
            self.transport.set_switch(
                self.config.device, "TELESCOPE_SLEW_RATE",
                self.config.terrestrial_slew_rate_element,
            )
            self.transport.issue_switch(self.config.device, "TELESCOPE_MOTION_NS", element)
            motion_started = True
            deadline = time.monotonic() + timeout_s
            target_stop = commanded - self.config.terrestrial_brake_lead_deg
            last = start
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Terrestrial step timed out before bounded stop")
                time.sleep(poll_s)
                sample = self.observe()
                self._validate_progress(sample)
                measured = self._sky_separation(start, sample)
                if intent == "toward" and sample.altitude_deg < floor:
                    raise RuntimeError("Terrestrial horizon floor was crossed")
                last = sample
                if measured >= target_stop:
                    break
            stopped = self.stop(timeout=min(5.0, timeout_s))
            motion_started = False
            if not stopped.stopped_confirmed:
                raise RuntimeError("Terrestrial STOP was not confirmed")
            final = self._wait_stable(last, deadline=deadline, poll_s=poll_s)
            measured = self._sky_separation(start, final)
            return IndiTerrestrialMoveResult(
                mode=IndiOperatingMode.TERRESTRIAL,
                state="stopped",
                requested_deg=requested,
                commanded_deg=commanded,
                measured_deg=measured,
                clipped=commanded < requested,
                pole_branch=branch,
                indi_direction=element,
                start_altitude_deg=start.altitude_deg,
                final_altitude_deg=final.altitude_deg,
                stop_confirmed=True,
            )
        except BaseException:
            if motion_started:
                self.stop(timeout=min(5.0, timeout_s))
            raise
        finally:
            self._lock.release()

    def _validate_start(self, sample) -> None:
        if self._park_reference is None:
            raise RuntimeError("No PARK reference captured for this terrestrial session")
        if (
            not sample.status_live or not sample.coordinates_live or
            not sample.horizontal_live or sample.altitude_deg is None or
            sample.ra_hours is None or sample.dec_deg is None
        ):
            raise RuntimeError("Fresh status, sky coordinates and horizontal feedback are required")
        if sample.parked or sample.at_home or sample.slewing or sample.guiding:
            raise RuntimeError("Terrestrial motion requires unparked stationary status")
        if sample.tracking:
            raise RuntimeError("Tracking must remain OFF in terrestrial mode")
        if sample.at_limit or {
            "indi_device_disconnected", "onstep_status_alert",
            "onstep_reported_error", "onstep_limit_or_park_fault",
            "park_state_conflict",
        }.intersection(sample.blockers):
            raise RuntimeError("Fault, limit or conflicting feedback blocks terrestrial motion")

    def _validate_progress(self, sample) -> None:
        if not sample.status_live or not sample.coordinates_live or not sample.horizontal_live:
            raise RuntimeError("Terrestrial feedback became stale")
        if sample.tracking or sample.at_limit or sample.parked:
            raise RuntimeError("Unsafe state appeared during terrestrial motion")

    def _pole_branch(self, sample) -> str:
        assert self._park_reference is not None
        reference_ra, _, reference_time = self._park_reference
        elapsed = (self.clock() - reference_time).total_seconds()
        expected_ra = (reference_ra + elapsed / 86164.0905 * 24.0) % 24.0
        residual = abs(((sample.ra_hours - expected_ra + 12.0) % 24.0) - 12.0)
        if residual <= 4.0:
            return "near"
        if residual >= 8.0:
            return "far"
        return "ambiguous"

    @staticmethod
    def _sky_separation(start, current) -> float:
        ra1 = math.radians(start.ra_hours * 15.0)
        ra2 = math.radians(current.ra_hours * 15.0)
        dec1 = math.radians(start.dec_deg)
        dec2 = math.radians(current.dec_deg)
        cosine = (
            math.sin(dec1) * math.sin(dec2) +
            math.cos(dec1) * math.cos(dec2) * math.cos(ra2 - ra1)
        )
        return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))

    def _wait_stable(self, initial, *, deadline: float, poll_s: float):
        previous = initial
        stable_since: float | None = None
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Terrestrial motion did not settle after STOP")
            time.sleep(poll_s)
            sample = self.observe()
            self._validate_progress(sample)
            stationary = (
                not sample.slewing and
                self._sky_separation(previous, sample) <= 0.02
            )
            if stationary:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= self.config.terrestrial_stop_hold_seconds:
                    return sample
            else:
                stable_since = None
            previous = sample
