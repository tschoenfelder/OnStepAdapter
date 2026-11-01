"""Connection policy for the INDI-backed OnStep adapter under development."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Callable

from .indi_config import IndiRuntimeConfig
from .indi_axis_motion import AxisMotionMode, IndiAxisMover, IndiAxisMoveResult
from .indi_focuser import IndiFocuser
from .indi_guiding import (
    GuideAxis,
    IndiGuideCalibration,
    IndiGuideController,
    IndiGuideCorrectionResult,
    IndiGuidePulseResult,
)
from .indi_home import IndiHomeRouteResult, IndiHomeRouter, IndiPositionResult, IndiUnparkResult
from .indi_meridian import IndiMeridianState, IndiMeridianSupervisor, classify_meridian
from .indi_motion import MotionHandle, MotionToken
from .indi_mount import IndiMount
from .indi_status import IndiMountSnapshot, IndiStatusReader
from .indi_stop import IndiStopResult, stop_mount_via_indi
from .indi_tracking import IndiTrackingResult, enable_tracking_via_indi
from .indi_trace import TraceRecorder, as_recorder, traced_operation
from .indi_transport import IndiTransport
from .indi_gu import decode_gu
from .meridian_policy import MeridianPolicy, derive_meridian_policy

TIME_SITE_SYNC_WARNING = (
    "Changing OnStep time or location while another client is tracking or "
    "slewing may change pointing and invalidate that client's safety calculations."
)


_traced = traced_operation(lambda client: client.trace)


@dataclass(frozen=True)
class IndiStartupStatus:
    device_connected: bool
    safe_meridian_flip_enabled: bool
    meridian_policy: MeridianPolicy
    controller_time_verified: bool
    astronomical_motion_ready: bool
    time_snapshot_age_seconds: float | None


@dataclass(frozen=True)
class IndiSyncResult:
    time_accepted: bool
    location_accepted: bool
    time_authority: str
    requested_utc: str
    requested_lat: float
    requested_lon: float
    warning: str
    error: str | None = None


class OnStepIndiClient:
    """Own this application's INDI connection, mount, focuser and supervisor."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 7624,
        device: str = "LX200 OnStep",
        safe_meridian_flip_via_home: bool = True,
        config: IndiRuntimeConfig | None = None,
        transport: IndiTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        auto_supervise: bool = True,
        guide_calibration: IndiGuideCalibration | None = None,
        trace: TraceRecorder | str | Path | None = None,
    ) -> None:
        self.device = config.device if config is not None else device
        self.safe_meridian_flip_via_home = (
            config.safe_meridian_flip_via_home if config is not None
            else safe_meridian_flip_via_home
        )
        self._config = config
        if trace is None and config is not None and config.trace_path:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            trace = config.trace_path.replace("{utc}", stamp)
        self._owns_trace = trace is not None and not isinstance(trace, TraceRecorder)
        self.trace = as_recorder(trace, source="real")
        self._transport = transport or IndiTransport(
            host=config.host if config is not None else host,
            port=config.port if config is not None else port,
        )
        if self.trace is not None:
            self._transport.trace = self.trace
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._auto_supervise = auto_supervise
        self._guide_calibration = guide_calibration
        self._session_time_site_accepted = False
        self._accepted_time_values: dict[str, str] | None = None
        self._accepted_site_values: dict[str, str] | None = None
        self._status_reader: IndiStatusReader | None = None
        self._home_router: IndiHomeRouter | None = None
        self._axis_mover: IndiAxisMover | None = None
        self._guide_controller: IndiGuideController | None = None
        self._supervisor: IndiMeridianSupervisor | None = None
        self._policy: MeridianPolicy | None = None
        self._motion: MotionHandle | None = None
        self._motion_lock = threading.Lock()
        self.mount = IndiMount(self)
        self.focuser = IndiFocuser(self._transport, config) if config is not None else None

    @property
    def home_authority_established(self) -> bool:
        if not self._home_router or not self._home_router.authority_established:
            return False
        connection = self._transport.get_property(self.device, "CONNECTION")
        status = self._transport.get_property(self.device, "OnStep Status")
        raw = status.values.get(":GU# return", "") if status else ""
        try:
            decoded = decode_gu(raw) if raw else {}
        except ValueError:
            decoded = {}
        if (
            not self._transport.is_open or connection is None or
            connection.values.get("CONNECT") != "On" or status is None or
            status.state.lower() == "alert" or
            status.values.get("Error", "None") not in {"None", "", "0"} or
            not decoded or decoded["parked"] or decoded["park_failed"] or
            decoded["at_limit"]
        ):
            self._home_router.authority_established = False
        return self._home_router.authority_established

    @property
    def session_baseline_accepted(self) -> bool:
        """Invalidate only this client's baseline on observable shared edits."""
        if not self._session_time_site_accepted or not self._transport.is_open:
            return False
        connection = self._transport.get_property(self.device, "CONNECTION")
        time_prop = self._transport.get_property(self.device, "TIME_UTC")
        site_prop = self._transport.get_property(self.device, "GEOGRAPHIC_COORD")
        if (connection is None or connection.values.get("CONNECT") != "On" or
                time_prop is None or site_prop is None):
            self._session_time_site_accepted = False
        elif (time_prop.values != self._accepted_time_values or
              site_prop.values != self._accepted_site_values):
            self._session_time_site_accepted = False
        return self._session_time_site_accepted

    @_traced
    def connect(self, *, timeout: float = 5.0) -> IndiStartupStatus:
        if self._transport.is_open and self._policy is not None:
            raise RuntimeError("INDI client is already connected; close before reconnecting")
        if self._supervisor is not None:
            self._supervisor.close()
        self._supervisor = None
        self._policy = None
        self._session_time_site_accepted = False
        self._accepted_time_values = None
        self._accepted_site_values = None
        self._status_reader = None
        self._home_router = None
        self._axis_mover = None
        self._guide_controller = None
        try:
            self._transport.connect(timeout=timeout)
            connected = self._transport.wait_property(self.device, "CONNECTION", timeout=timeout)
            if connected.values.get("CONNECT") != "On":
                raise ConnectionError("OnStep INDI device is not connected")

            reported_time = self._transport.wait_property(self.device, "TIME_UTC", timeout=timeout)
            timestamp = datetime.fromisoformat(reported_time.values["UTC"])
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            # The driver publishes a cached snapshot, not a continuously read clock.
            # A small delta cannot establish current controller time either.
            snapshot_age = abs((datetime.now(timezone.utc) - timestamp).total_seconds())

            safe_flip = self._transport.wait_property(
                self.device, "SAFE_MERIDIAN_FLIP", timeout=timeout
            )
            if safe_flip.state.lower() in {"alert", "busy"}:
                raise RuntimeError("Safe HOME-route flip property is not ready")
            if self.safe_meridian_flip_via_home and safe_flip.values.get("ON") != "On":
                safe_flip = self._transport.set_switch(
                    self.device, "SAFE_MERIDIAN_FLIP", "ON", timeout=timeout
                )
            if self.safe_meridian_flip_via_home and safe_flip.values.get("ON") != "On":
                raise RuntimeError("Safe HOME-route flip could not be verified")
            if self.safe_meridian_flip_via_home and safe_flip.state.lower() != "ok":
                raise RuntimeError("Safe HOME-route flip activation was not confirmed")

            limits = self._transport.wait_property(
                self.device, "Minutes Past Meridian", timeout=timeout
            )
            if limits.state.lower() == "alert":
                raise RuntimeError("Firmware meridian guard readback is in Alert state")
            policy = derive_meridian_policy(
                east_guard_minutes=float(limits.values["East"]),
                west_guard_minutes=float(limits.values["West"]),
                flip_request_deg=self._config.flip_request_deg if self._config else None,
                hard_stop_deg=self._config.tracking_stop_deg if self._config else None,
                flip_allowance_seconds=self._config.flip_allowance_seconds if self._config else 120.0,
                reserve_seconds=self._config.reserve_seconds if self._config else 30.0,
            )
            self._policy = policy
            if self._config is not None:
                self._status_reader = IndiStatusReader(
                    self._transport, self._config,
                    baseline_accepted=lambda: self.session_baseline_accepted,
                    home_authority=lambda: self.home_authority_established,
                )
                self._home_router = IndiHomeRouter(
                    self._transport, self._status_reader, self.device
                )
                self._axis_mover = IndiAxisMover(
                    self._transport, self.device, self._config.observer_lat,
                    self._config.observer_lon, self.observe_mount,
                    self.emergency_stop,
                    min_alt_deg=self._config.movement_min_alt_deg,
                    min_dec_deg=self._config.movement_min_dec_deg,
                    max_dec_deg=self._config.movement_max_dec_deg,
                )
                self._guide_controller = IndiGuideController(
                    self._transport, self.device, observe=self.observe_mount,
                    meridian_status=self.meridian_status,
                    emergency_stop=self.emergency_stop,
                    authority_policy=self._config.tracking_authority_policy,
                )
                self._supervisor = IndiMeridianSupervisor(
                    observe=self.observe_mount, policy=policy, stop=self.emergency_stop
                )
                if self._auto_supervise:
                    self._supervisor.start()
            return IndiStartupStatus(
                device_connected=True,
                safe_meridian_flip_enabled=safe_flip.values.get("ON") == "On",
                meridian_policy=policy,
                controller_time_verified=False,
                astronomical_motion_ready=False,
                time_snapshot_age_seconds=snapshot_age,
            )
        except (ConnectionError, KeyError, RuntimeError, TimeoutError, ValueError):
            self._transport.close()
            raise

    def observe_mount(self) -> IndiMountSnapshot:
        """Return fresh logical mount diagnostics and motion-preflight inputs."""
        if self._status_reader is None:
            raise RuntimeError("Connect with an INDI runtime configuration first")
        return self._status_reader.read()

    @property
    def active_motion(self) -> MotionHandle | None:
        """The move that is still running, if any."""
        motion = self._motion
        return motion if motion is not None and not motion.done else None

    def _start_motion(self, kind: str, run) -> MotionHandle:
        """Start ``run(token)`` in a worker and return its handle at once."""
        with self._motion_lock:
            if self.active_motion is not None:
                raise RuntimeError(
                    f"Another mount move ({self._motion.kind}) is active; "
                    "stop it or wait for it first"
                )
            handle = MotionHandle(
                kind, run, stop=self.emergency_stop, on_finish=self._motion_finished
            )
            self._motion = handle
            if self.trace is not None:
                self.trace.record("motion_start", kind=kind)
            return handle._start()

    def _motion_finished(self, handle: MotionHandle) -> None:
        if self.trace is None:
            return
        progress = handle.progress()
        self.trace.record(
            "motion_end", kind=handle.kind, state=progress.state,
            duration_s=progress.elapsed_s, error=progress.error,
            result=handle._result,
        )

    def _require_no_motion(self) -> None:
        if self.active_motion is not None:
            raise RuntimeError(
                f"A mount move ({self._motion.kind}) is active; stop it or wait for it first"
            )

    def move_axis_deg(
        self, axis: str, offset_deg: float, *, timeout_s: float = 30.0,
        poll_s: float = 0.1,
        mode: AxisMotionMode | str = AxisMotionMode.TERRESTRIAL,
    ) -> MotionHandle:
        """Start a finite axis move and return its handle at once.

        ``handle.wait()`` returns an ``IndiAxisMoveResult`` or raises the
        error the move ended with.
        """
        if self._axis_mover is None or not self._transport.is_open:
            raise ConnectionError("INDI client is not connected")
        self._axis_mover.validate_request(
            axis, offset_deg, timeout_s=timeout_s, poll_s=poll_s, mode=mode
        )
        mover = self._axis_mover

        def run(token: MotionToken) -> IndiAxisMoveResult:
            return mover.move(
                axis, offset_deg, timeout_s=timeout_s, poll_s=poll_s, mode=mode,
                token=token,
            )

        return self._start_motion(f"{axis}_axis", run)

    def meridian_status(self) -> IndiMeridianState:
        if self._policy is None:
            raise ConnectionError("INDI client is not connected")
        return classify_meridian(self.observe_mount(), self._policy)

    def guide_pulse(
        self, direction: str, duration_ms: int, *, command_timeout: float = 3.0,
    ) -> IndiGuidePulseResult:
        if self._guide_controller is None or not self._transport.is_open:
            raise ConnectionError("INDI client is not connected")
        return self._guide_controller.pulse(
            direction, duration_ms, command_timeout=command_timeout
        )

    def get_guide_calibration(self) -> IndiGuideCalibration | None:
        return self._guide_calibration

    def set_guide_calibration(
        self, calibration: IndiGuideCalibration | None,
    ) -> None:
        self._guide_calibration = calibration

    def guide_correction(
        self,
        axis: GuideAxis,
        offset_arcsec: float,
        *,
        calibration: IndiGuideCalibration | None = None,
        command_timeout: float = 3.0,
    ) -> IndiGuideCorrectionResult:
        if self._guide_controller is None or not self._transport.is_open:
            raise ConnectionError("INDI client is not connected")
        return self._guide_controller.correction(
            axis,
            offset_arcsec,
            calibration if calibration is not None else self._guide_calibration,
            command_timeout=command_timeout,
        )

    def start_supervision(self) -> None:
        if self._supervisor is None or not self._transport.is_open:
            raise ConnectionError("INDI client is not connected")
        self._supervisor.start()

    def emergency_stop(self, *, timeout: float = 5.0) -> IndiStopResult:
        """Request abort and tracking off without requiring time/site authority.

        Also ends the running move, if any, so its handle reports ``stopped``.
        Safe to call from any thread at any time.
        """
        motion = self.active_motion
        if motion is not None:
            motion._request_stop()
        if not self._transport.is_open:
            raise ConnectionError("INDI client is not connected")
        result = stop_mount_via_indi(
            self._transport, self.device, confirmation_timeout=timeout
        )
        if motion is not None:
            motion._record_stop(result)
        return result

    def enable_tracking(self, *, timeout: float = 8.0) -> IndiTrackingResult:
        if not self._transport.is_open or self._status_reader is None:
            raise ConnectionError("INDI client is not connected")
        return enable_tracking_via_indi(
            self._transport, self.device, observe=self.observe_mount,
            meridian_status=self.meridian_status,
            emergency_stop=self.emergency_stop, timeout=timeout,
            authority_policy=self._config.tracking_authority_policy,
        )

    def route_park_to_home(
        self, *, unpark_timeout: float = 20.0, home_timeout: float = 120.0
    ) -> IndiHomeRouteResult:
        """Route mechanically; no normal motion is unlocked by this prototype."""
        self._require_home_motion()
        self._require_no_motion()
        if self._home_router is None or not self._transport.is_open:
            raise ConnectionError("Connect with an INDI runtime configuration first")
        return self._home_router.park_to_home(
            unpark_timeout=unpark_timeout, home_timeout=home_timeout
        )

    def unpark(self, *, timeout: float = 20.0) -> IndiUnparkResult:
        """Leave PARKED without requesting a HOME slew."""
        self._require_home_motion()
        self._require_no_motion()
        if self._home_router is None or not self._transport.is_open:
            raise ConnectionError("Connect with an INDI runtime configuration first")
        return self._home_router.unpark(timeout=timeout)

    def go_home(self, *, timeout: float = 120.0) -> MotionHandle:
        """Start the move to OnStep HOME and return its handle at once.

        ``handle.wait()`` returns an ``IndiPositionResult`` once live arrival
        is verified (or the move failed); ``handle.stop()`` ends it.
        """
        self._require_home_motion()
        if self._home_router is None or not self._transport.is_open:
            raise ConnectionError("Connect with an INDI runtime configuration first")
        router = self._home_router
        return self._start_motion(
            "home", lambda token: router.go_home(timeout=timeout, token=token)
        )

    def park(self, *, timeout: float = 120.0) -> MotionHandle:
        """Start the move to OnStep's stored PARK and return its handle at once."""
        self._require_home_motion()
        if self._home_router is None or not self._transport.is_open:
            raise ConnectionError("Connect with an INDI runtime configuration first")
        router = self._home_router
        return self._start_motion(
            "park", lambda token: router.park(timeout=timeout, token=token)
        )

    def _require_home_motion(self) -> None:
        if self._config is None or not self._config.home_motion_enabled:
            raise RuntimeError(
                "INDI HOME/PARK motion is disabled pending the supervised H-status check"
            )

    @_traced
    def sync_time_location(
        self, *, user_approved: bool, timeout: float = 5.0
    ) -> IndiSyncResult:
        """Apply operator-approved Raspberry time/site through the local server.

        Ok acknowledges INDI's setters; it does not independently read the
        controller clock. Partial completion is reported without granting a
        complete session baseline.
        """
        if not user_approved:
            raise PermissionError("User approval is required to change shared OnStep time/location")
        if self._config is None:
            raise ValueError("Observer configuration is required for time/location sync")
        if self._transport.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Raspberry time sync requires a client running beside the local INDI server")
        if not self._transport.is_open:
            raise ConnectionError("INDI client is not connected")
        connection = self._transport.get_property(self.device, "CONNECTION")
        if connection is None or connection.values.get("CONNECT") != "On":
            raise ConnectionError("OnStep INDI device is not connected")
        current = self._clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("Raspberry time source must be timezone-aware")
        utc = current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        offset = f"{current.utcoffset().total_seconds() / 3600:+.2f}"
        self._session_time_site_accepted = False
        self._accepted_time_values = None
        self._accepted_site_values = None
        time_accepted = False
        location_accepted = False
        error = None
        try:
            time_result = self._transport.set_text(
                self.device, "TIME_UTC", {"UTC": utc, "OFFSET": offset}, timeout=timeout
            )
            time_accepted = True
            site_result = self._transport.set_number(
                self.device, "GEOGRAPHIC_COORD",
                {
                    "LAT": self._config.observer_lat,
                    "LONG": self._config.observer_lon,
                    "ELEV": self._config.observer_alt_m,
                },
                timeout=timeout,
            )
            location_accepted = True
            self._accepted_time_values = dict(time_result.values)
            self._accepted_site_values = dict(site_result.values)
        except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
            error = str(exc)
        self._session_time_site_accepted = time_accepted and location_accepted
        return IndiSyncResult(
            time_accepted=time_accepted,
            location_accepted=location_accepted,
            time_authority=(
                "accepted_not_independently_read_back" if time_accepted else "unestablished"
            ),
            requested_utc=utc,
            requested_lat=self._config.observer_lat,
            requested_lon=self._config.observer_lon,
            warning=TIME_SITE_SYNC_WARNING,
            error=error,
        )

    def close(self) -> None:
        motion = self.active_motion
        if motion is not None and self._transport.is_open:
            # Never leave a move running without its supervisor and transport.
            try:
                self.emergency_stop()
            except (ConnectionError, RuntimeError, TimeoutError, ValueError):
                pass
        try:
            if self._supervisor is not None:
                self._supervisor.close()
        finally:
            self._supervisor = None
            self._policy = None
            self._session_time_site_accepted = False
            self._accepted_time_values = None
            self._accepted_site_values = None
            self._status_reader = None
            self._axis_mover = None
            self._guide_controller = None
            self._home_router = None
            self._transport.close()
            if motion is not None:
                motion._thread.join(timeout=1.0)

    def __enter__(self) -> OnStepIndiClient:
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            self.close()
        finally:
            if self._owns_trace and self.trace is not None:
                self.trace.close()
