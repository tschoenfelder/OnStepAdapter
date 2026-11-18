"""Bounded pole-aware terrestrial motion through standard INDI properties."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import math
import threading
import time
from typing import Callable, Literal, TypeVar

from .indi_config import IndiRuntimeConfig
from .indi_mode import IndiModeController, IndiOperatingMode
from .indi_stop import IndiStopResult
from .indi_transport import IndiTransport


T = TypeVar("T")

logger = logging.getLogger(__name__)


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
    slew_rate_element: str = ""


@dataclass(frozen=True)
class IndiTerrestrialRecoveryResult:
    direction: str
    requested_duration_s: float
    stop_confirmed: bool
    final_raw_status: str
    limit_or_fault_present: bool
    slew_rate_element: str
    axis: str = "ra"
    recovery_mode: bool = True


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
        self._stationary_reference: tuple[float, float] | None = None

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def capture_park_reference(self, snapshot) -> None:
        coordinates_present = (
            snapshot.ra_hours is not None and snapshot.dec_deg is not None and
            math.isfinite(snapshot.ra_hours) and math.isfinite(snapshot.dec_deg)
        )
        if (
            not snapshot.parked or not snapshot.status_live or
            not snapshot.park_evidence_consistent or
            snapshot.slewing or snapshot.tracking or snapshot.at_limit or
            not coordinates_present
        ):
            raise RuntimeError(
                "A reconciled stationary PARKED sample with logical coordinates "
                "is required"
            )
        self._park_reference = (snapshot.ra_hours, snapshot.dec_deg, self.clock())

    def authorize_stationary_start(self, snapshot) -> None:
        """Anchor the verified non-moving position after terrestrial UNPARK."""
        if (
            snapshot.parked or snapshot.at_home or snapshot.slewing or
            snapshot.guiding or snapshot.tracking or snapshot.at_limit or
            snapshot.ra_hours is None or snapshot.dec_deg is None or
            snapshot.altitude_deg is None or
            {
                "indi_device_disconnected", "onstep_status_alert",
                "onstep_reported_error", "onstep_limit_or_park_fault",
                "park_state_conflict",
            }.intersection(snapshot.blockers)
        ):
            raise RuntimeError("Verified stationary terrestrial UNPARK is required")
        self._stationary_reference = (snapshot.ra_hours, snapshot.dec_deg)

    def move_toward_north_horizon(
        self, step_deg: float | None = None, *, timeout_s: float = 45.0,
        poll_s: float = 0.1,
    ) -> IndiTerrestrialMoveResult:
        return self._move(
            "toward", step_deg, timeout_s=timeout_s, poll_s=poll_s,
        )

    def move_away_from_north_horizon(
        self, step_deg: float | None = None, *, timeout_s: float = 45.0,
        poll_s: float = 0.1,
    ) -> IndiTerrestrialMoveResult:
        return self._move(
            "away", step_deg, timeout_s=timeout_s, poll_s=poll_s,
        )

    def move_ra_axis(
        self, offset_deg: float, *, timeout_s: float = 45.0,
        poll_s: float = 0.1,
    ) -> IndiTerrestrialMoveResult:
        """Run one bounded Axis-1 path using a fixed INDI E/W direction."""
        requested = float(offset_deg)
        if (
            not math.isfinite(requested) or requested == 0 or
            abs(requested) > self.config.terrestrial_max_step_deg
        ):
            raise ValueError(
                "Terrestrial RA step must be finite, non-zero and within "
                "max_step_deg"
            )
        if timeout_s <= 0 or poll_s <= 0:
            raise ValueError("Terrestrial timeout and polling interval must be positive")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Another terrestrial motion is active or stopping")
        motion_started = False
        try:
            self.mode.require(IndiOperatingMode.TERRESTRIAL)
            start = self.observe()
            self._validate_start(start)
            requested_magnitude = abs(requested)
            if requested_magnitude <= self.config.terrestrial_brake_lead_deg:
                raise RuntimeError("Requested travel is below the braking allowance")
            element = "MOTION_WEST" if requested > 0 else "MOTION_EAST"
            expected_sign = -1.0 if element == "MOTION_WEST" else 1.0
            slew_rate_element = self._resolve_slew_rate_element()
            self.transport.set_switch(
                self.config.device, "TELESCOPE_SLEW_RATE", slew_rate_element,
            )
            self._start_manual_motion("TELESCOPE_MOTION_WE", element)
            motion_started = True
            deadline = time.monotonic() + timeout_s
            target_stop = requested_magnitude - self.config.terrestrial_brake_lead_deg
            floor = (
                self.config.movement_min_alt_deg +
                self.config.terrestrial_horizon_margin_deg
            )
            measured = 0.0
            last = start
            last_progress_at = time.monotonic()
            next_progress_log = 0.25
            logger.info(
                "Terrestrial RA motion started: request=%+.3fdeg direction=%s "
                "start_ra=%.6fh start_dec=%.6fdeg target_stop=%.3fdeg",
                requested, element, start.ra_hours, start.dec_deg, target_stop,
            )
            while True:
                if time.monotonic() >= deadline:
                    logger.error(
                        "Terrestrial RA motion timed out before STOP: "
                        "request=%+.3fdeg direction=%s measured=%.3fdeg "
                        "last_ra=%.6fh last_dec=%.6fdeg",
                        requested, element, measured, last.ra_hours, last.dec_deg,
                    )
                    raise TimeoutError("Terrestrial RA step timed out before bounded stop")
                time.sleep(poll_s)
                sample = self.observe()
                error_code = self._raw_general_error_code(sample.raw_status)
                if error_code > 0:
                    reason = self._raw_general_error_name(error_code)
                    logger.error(
                        "OnStep stopped terrestrial RA motion: request=%+.3fdeg "
                        "direction=%s measured=%.3fdeg raw_status=%r "
                        "controller_error=%s",
                        requested, element, measured, sample.raw_status, reason,
                    )
                    raise RuntimeError(
                        "Terrestrial RA motion stopped before its target; "
                        f"OnStep reports {reason} (code {error_code})"
                    )
                self._validate_progress(sample)
                assert sample.ra_hours is not None and sample.dec_deg is not None
                segment = (
                    (sample.ra_hours - last.ra_hours + 12.0) % 24.0 - 12.0
                ) * 15.0
                signed_segment = segment * expected_sign
                if signed_segment < -0.05:
                    raise RuntimeError(
                        "Axis-1 feedback moved opposite to the commanded INDI direction"
                    )
                if abs(sample.dec_deg - start.dec_deg) > 0.25:
                    raise RuntimeError("DEC changed during terrestrial Axis-1 motion")
                if sample.altitude_deg < floor:
                    raise RuntimeError("Terrestrial horizon floor was crossed")
                progress = max(0.0, signed_segment)
                measured += progress
                if progress >= 0.005:
                    last_progress_at = time.monotonic()
                last = sample
                if measured >= next_progress_log:
                    logger.info(
                        "Terrestrial RA progress: request=%+.3fdeg direction=%s "
                        "measured=%.3fdeg ra=%.6fh dec=%.6fdeg slewing=%s",
                        requested, element, measured, sample.ra_hours,
                        sample.dec_deg, sample.slewing,
                    )
                    next_progress_log = measured + 0.25
                if (
                    time.monotonic() - last_progress_at >=
                    self.config.terrestrial_stall_timeout_seconds
                ):
                    error_code = self._raw_general_error_code(sample.raw_status)
                    reason = self._raw_general_error_name(error_code)
                    logger.error(
                        "Terrestrial RA motion stopped progressing: request=%+.3fdeg "
                        "direction=%s measured=%.3fdeg raw_status=%r "
                        "controller_error=%s",
                        requested, element, measured, sample.raw_status, reason,
                    )
                    raise RuntimeError(
                        "Terrestrial RA motion stopped before its target; "
                        f"OnStep reports {reason} (code {error_code})"
                    )
                if measured >= target_stop:
                    break
            logger.info(
                "Terrestrial RA bounded STOP requested: request=%+.3fdeg "
                "direction=%s measured=%.3fdeg",
                requested, element, measured,
            )
            stopped = self.stop(timeout=min(5.0, timeout_s))
            motion_started = False
            if not stopped.stopped_confirmed:
                raise RuntimeError("Terrestrial RA STOP was not confirmed")
            final = self._wait_stable(last, deadline=deadline, poll_s=poll_s)
            final_segment = (
                (final.ra_hours - last.ra_hours + 12.0) % 24.0 - 12.0
            ) * 15.0 * expected_sign
            if final_segment < -0.05:
                raise RuntimeError("Axis-1 reversed while settling after STOP")
            measured += max(0.0, final_segment)
            logger.info(
                "Terrestrial RA motion completed: request=%+.3fdeg direction=%s "
                "measured=%.3fdeg final_ra=%.6fh final_dec=%.6fdeg",
                requested, element, measured, final.ra_hours, final.dec_deg,
            )
            self._stationary_reference = (final.ra_hours, final.dec_deg)
            return IndiTerrestrialMoveResult(
                mode=IndiOperatingMode.TERRESTRIAL,
                state="stopped",
                requested_deg=requested_magnitude,
                commanded_deg=requested_magnitude,
                measured_deg=measured,
                clipped=False,
                pole_branch="axis1",
                indi_direction=element,
                start_altitude_deg=start.altitude_deg,
                final_altitude_deg=final.altitude_deg,
                stop_confirmed=True,
                slew_rate_element=slew_rate_element,
            )
        except BaseException:
            if motion_started:
                self.stop(timeout=min(5.0, timeout_s))
            raise
        finally:
            self._lock.release()

    def recover_ra_timed(
        self, direction: str, duration_s: float, *, stop_timeout_s: float = 5.0,
    ) -> IndiTerrestrialRecoveryResult:
        """Operator-observed Axis-1 recovery, bounded by time rather than RA/DEC."""
        return self._recover_axis_timed(
            "ra", direction, duration_s, stop_timeout_s=stop_timeout_s,
            recovery_mode=True,
        )

    def recover_dec_timed(
        self, direction: str, duration_s: float, *, stop_timeout_s: float = 5.0,
    ) -> IndiTerrestrialRecoveryResult:
        """Operator-observed Axis-2 recovery, bounded by time rather than coordinates."""
        return self._recover_axis_timed(
            "dec", direction, duration_s, stop_timeout_s=stop_timeout_s,
            recovery_mode=True,
        )

    def move_ra_timed(
        self, direction: str, duration_s: float, *, stop_timeout_s: float = 5.0,
    ) -> IndiTerrestrialRecoveryResult:
        """Normal supervised Axis-1 jog for an established terrestrial session."""
        return self._recover_axis_timed(
            "ra", direction, duration_s, stop_timeout_s=stop_timeout_s,
            recovery_mode=False,
        )

    def move_dec_timed(
        self, direction: str, duration_s: float, *, stop_timeout_s: float = 5.0,
    ) -> IndiTerrestrialRecoveryResult:
        """Normal supervised Axis-2 jog for an established terrestrial session."""
        return self._recover_axis_timed(
            "dec", direction, duration_s, stop_timeout_s=stop_timeout_s,
            recovery_mode=False,
        )

    def run_finite_axis_jog(self, operation: Callable[[], T]) -> T:
        """Run a finite target move under terrestrial session authority."""
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Another terrestrial motion is active or stopping")
        try:
            self.mode.require(IndiOperatingMode.TERRESTRIAL)
            self._validate_timed_jog_start(self.observe())
            result = operation()
            final = self.observe()
            if final.ra_hours is not None and final.dec_deg is not None:
                self._stationary_reference = (final.ra_hours, final.dec_deg)
            return result
        finally:
            self._lock.release()

    def _recover_axis_timed(
        self, axis: Literal["ra", "dec"], direction: str, duration_s: float,
        *, stop_timeout_s: float, recovery_mode: bool,
    ) -> IndiTerrestrialRecoveryResult:
        normalized = direction.strip().lower()
        allowed = {"east", "west"} if axis == "ra" else {"north", "south"}
        if normalized not in allowed:
            choices = " or ".join(sorted(allowed))
            raise ValueError(f"{axis.upper()} recovery direction must be {choices}")
        duration = float(duration_s)
        maximum = self.config.terrestrial_ra_recovery_max_seconds
        if not math.isfinite(duration) or not 0.1 <= duration <= maximum:
            raise ValueError(
                f"{axis.upper()} recovery duration must be within "
                f"0.1..{maximum:g} seconds"
            )
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Another terrestrial motion is active or stopping")
        motion_started = False
        try:
            self.mode.require(IndiOperatingMode.TERRESTRIAL)
            initial_stop = self.stop(timeout=stop_timeout_s)
            if not initial_stop.stopped_confirmed:
                raise RuntimeError(f"{axis.upper()} recovery pre-stop was not confirmed")
            start = self.observe()
            if recovery_mode:
                self._validate_recovery_start(start)
            else:
                self._validate_timed_jog_start(start)
            if axis == "ra":
                property_name = "TELESCOPE_MOTION_WE"
                element = "MOTION_EAST" if normalized == "east" else "MOTION_WEST"
            else:
                property_name = "TELESCOPE_MOTION_NS"
                element = "MOTION_NORTH" if normalized == "north" else "MOTION_SOUTH"
            slew_rate_element = self._resolve_slew_rate_element()
            self.transport.set_switch(
                self.config.device, "TELESCOPE_SLEW_RATE", slew_rate_element,
            )
            self._start_manual_motion(property_name, element)
            motion_started = True
            time.sleep(duration)
            stopped = self.stop(timeout=stop_timeout_s)
            motion_started = False
            if not stopped.stopped_confirmed:
                raise RuntimeError(f"{axis.upper()} recovery STOP was not confirmed")
            final = self.observe()
            return IndiTerrestrialRecoveryResult(
                direction=normalized,
                requested_duration_s=duration,
                stop_confirmed=True,
                final_raw_status=final.raw_status,
                limit_or_fault_present=bool(final.at_limit or {
                    "onstep_status_alert", "onstep_reported_error",
                    "onstep_limit_or_park_fault",
                }.intersection(final.blockers)),
                slew_rate_element=slew_rate_element,
                axis=axis,
                recovery_mode=recovery_mode,
            )
        except BaseException:
            if motion_started:
                self.stop(timeout=stop_timeout_s)
            raise
        finally:
            self._lock.release()

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
            slew_rate_element = self._resolve_slew_rate_element()
            self.transport.set_switch(
                self.config.device, "TELESCOPE_SLEW_RATE",
                slew_rate_element,
            )
            self._start_manual_motion("TELESCOPE_MOTION_NS", element)
            motion_started = True
            deadline = time.monotonic() + timeout_s
            target_stop = commanded - self.config.terrestrial_brake_lead_deg
            last = start
            measured = 0.0
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Terrestrial step timed out before bounded stop")
                time.sleep(poll_s)
                sample = self.observe()
                self._validate_progress(sample)
                # Sum short consecutive sky arcs instead of comparing only
                # the endpoints.  Around the celestial pole OnStep changes
                # RA by about 12h and reverses logical DEC even though Axis-2
                # continues smoothly in one mechanical direction.
                measured += self._sky_separation(last, sample)
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
            measured += self._sky_separation(last, final)
            self._stationary_reference = (final.ra_hours, final.dec_deg)
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
                slew_rate_element=slew_rate_element,
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
        complete = (
            sample.altitude_deg is not None and sample.ra_hours is not None and
            sample.dec_deg is not None
        )
        live = sample.status_live and sample.coordinates_live and sample.horizontal_live
        anchored = self._matches_stationary_reference(sample) if complete else False
        if not complete or not (live or anchored):
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

    @staticmethod
    def _validate_recovery_start(sample) -> None:
        blockers = {
            "indi_device_disconnected", "park_state_conflict",
            "pier_side_conflict", "pier_side_unknown",
        }.intersection(sample.blockers)
        if blockers or not sample.status_live:
            raise RuntimeError(
                f"Axis recovery status is unavailable or conflicting: {sorted(blockers)}"
            )
        if sample.parked or sample.at_home:
            raise RuntimeError("Axis recovery requires an unparked mount")
        if sample.tracking or sample.guiding or sample.slewing:
            raise RuntimeError("Axis recovery requires confirmed idle tracking-off state")

    def _validate_timed_jog_start(self, sample) -> None:
        if self._park_reference is None or self._stationary_reference is None:
            raise RuntimeError(
                "Normal terrestrial jog requires PARK then UNPARK through this client"
            )
        blockers = {
            "indi_device_disconnected", "park_state_conflict",
            "pier_side_conflict", "pier_side_unknown", "onstep_status_alert",
            "onstep_reported_error", "onstep_limit_or_park_fault",
        }.intersection(sample.blockers)
        if blockers or not sample.status_live or sample.at_limit:
            raise RuntimeError(
                f"Normal terrestrial jog requires fresh fault-free status: "
                f"{sorted(blockers)}"
            )
        if sample.parked or sample.at_home:
            raise RuntimeError("Normal terrestrial jog requires an unparked mount")
        if sample.tracking or sample.guiding or sample.slewing:
            raise RuntimeError(
                "Normal terrestrial jog requires confirmed idle tracking-off state"
            )

    def _resolve_slew_rate_element(self) -> str:
        prop = self.transport.get_property(
            self.config.device, "TELESCOPE_SLEW_RATE"
        )
        if prop is None:
            raise RuntimeError("INDI TELESCOPE_SLEW_RATE is unavailable")
        configured = self.config.terrestrial_slew_rate_element
        if configured in prop.values:
            return configured
        if configured.upper() == "SLEW_MAX":
            numeric = sorted(
                (int(name), name) for name in prop.values if name.isdigit()
            )
            if numeric:
                return numeric[-1][1]
        available = ", ".join(sorted(prop.values))
        raise RuntimeError(
            f"Configured terrestrial slew rate {configured!r} is unavailable; "
            f"driver offers: {available or 'none'}"
        )

    def _start_manual_motion(self, property_name: str, element: str) -> None:
        """Re-arm an INDI manual-motion vector before selecting its direction.

        The LX200 OnStep driver can retain the last direction as ``On`` after
        an abort. Sending that same direction again may then be accepted by
        INDI without starting a new movement. An explicit all-Off vector makes
        every finite jog a fresh edge, including repeated same-direction jogs.
        """
        if property_name == "TELESCOPE_MOTION_WE":
            elements = ("MOTION_WEST", "MOTION_EAST")
        elif property_name == "TELESCOPE_MOTION_NS":
            elements = ("MOTION_NORTH", "MOTION_SOUTH")
        else:
            raise ValueError(f"Unsupported manual-motion property {property_name!r}")
        if element not in elements:
            raise ValueError(f"Unsupported direction {element!r} for {property_name}")
        logger.info(
            "Re-arming INDI manual motion: property=%s direction=%s",
            property_name, element,
        )
        off_revision = self.transport.issue_switches(
            self.config.device, property_name,
            {name: False for name in elements},
        )
        deadline = time.monotonic() + 3.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"INDI did not confirm {property_name} was idle before motion"
                )
            idle = self.transport.wait_property(
                self.config.device, property_name,
                timeout=remaining, after_revision=off_revision,
            )
            off_revision = idle.revision
            if idle.state.lower() == "alert":
                raise RuntimeError(f"INDI rejected idle {property_name}")
            if all(idle.values.get(name) == "Off" for name in elements):
                break
        on_revision = self.transport.issue_switches(
            self.config.device, property_name,
            {name: name == element for name in elements},
        )
        deadline = time.monotonic() + 3.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"INDI did not confirm {property_name}.{element} started"
                )
            active = self.transport.wait_property(
                self.config.device, property_name,
                timeout=remaining, after_revision=on_revision,
            )
            on_revision = active.revision
            if active.state.lower() == "alert":
                raise RuntimeError(f"INDI rejected {property_name}.{element}")
            if active.values.get(element) == "On":
                break

    @staticmethod
    def _raw_general_error_code(raw_status: str | None) -> int:
        if raw_status and len(raw_status) >= 3 and raw_status[-1].isdigit():
            return int(raw_status[-1])
        return -1

    @staticmethod
    def _raw_general_error_name(code: int) -> str:
        return {
            0: "no controller error",
            1: "motor fault",
            2: "minimum-altitude limit",
            3: "physical limit switch",
            4: "declination limit",
            5: "azimuth limit",
            6: "under-pole limit",
            7: "meridian limit",
            8: "sync error",
            9: "park error",
        }.get(code, "unknown controller error")

    def _matches_stationary_reference(self, sample) -> bool:
        if self._stationary_reference is None:
            return False
        ra_hours, dec_deg = self._stationary_reference
        return self._coordinate_separation(
            ra_hours, dec_deg, sample.ra_hours, sample.dec_deg
        ) <= 0.02

    def _validate_progress(self, sample) -> None:
        if not sample.coordinates_live or not sample.horizontal_live:
            raise RuntimeError("Terrestrial coordinate feedback became stale")
        if {
            "indi_device_disconnected", "onstep_status_alert",
            "onstep_reported_error", "onstep_limit_or_park_fault",
            "park_state_conflict",
        }.intersection(sample.blockers):
            raise RuntimeError("Fault or conflicting feedback appeared during terrestrial motion")
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
        return IndiTerrestrialController._coordinate_separation(
            start.ra_hours, start.dec_deg, current.ra_hours, current.dec_deg
        )

    @staticmethod
    def _coordinate_separation(
        ra1_hours: float, dec1_deg: float, ra2_hours: float, dec2_deg: float,
    ) -> float:
        ra1 = math.radians(ra1_hours * 15.0)
        ra2 = math.radians(ra2_hours * 15.0)
        dec1 = math.radians(dec1_deg)
        dec2 = math.radians(dec2_deg)
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
