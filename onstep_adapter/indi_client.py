"""Connection policy for the INDI-backed OnStep adapter under development."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .indi_config import IndiRuntimeConfig
from .indi_axis_motion import AxisMotionMode, IndiAxisMover, IndiAxisMoveResult
from .indi_focuser import IndiFocuser
from .indi_home import IndiHomeRouteResult, IndiHomeRouter, IndiPositionResult, IndiUnparkResult
from .indi_meridian import IndiMeridianState, IndiMeridianSupervisor, classify_meridian
from .indi_mount import IndiMount
from .indi_status import IndiMountSnapshot, IndiStatusReader
from .indi_stop import IndiStopResult, stop_mount_via_indi
from .indi_tracking import IndiTrackingResult, enable_tracking_via_indi
from .indi_transport import IndiTransport
from .indi_gu import decode_gu
from .meridian_policy import MeridianPolicy, derive_meridian_policy

TIME_SITE_SYNC_WARNING = (
    "Changing OnStep time or location while another client is tracking or "
    "slewing may change pointing and invalidate that client's safety calculations."
)


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
    ) -> None:
        self.device = config.device if config is not None else device
        self.safe_meridian_flip_via_home = (
            config.safe_meridian_flip_via_home if config is not None
            else safe_meridian_flip_via_home
        )
        self._config = config
        self._transport = transport or IndiTransport(
            host=config.host if config is not None else host,
            port=config.port if config is not None else port,
        )
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._auto_supervise = auto_supervise
        self._session_time_site_accepted = False
        self._accepted_time_values: dict[str, str] | None = None
        self._accepted_site_values: dict[str, str] | None = None
        self._status_reader: IndiStatusReader | None = None
        self._home_router: IndiHomeRouter | None = None
        self._axis_mover: IndiAxisMover | None = None
        self._supervisor: IndiMeridianSupervisor | None = None
        self._policy: MeridianPolicy | None = None
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

    def move_axis_deg(
        self, axis: str, offset_deg: float, *, timeout_s: float = 30.0,
        poll_s: float = 0.1,
        mode: AxisMotionMode | str = AxisMotionMode.TERRESTRIAL,
    ) -> IndiAxisMoveResult:
        if self._axis_mover is None or not self._transport.is_open:
            raise ConnectionError("INDI client is not connected")
        return self._axis_mover.move(
            axis, offset_deg, timeout_s=timeout_s, poll_s=poll_s, mode=mode
        )

    def meridian_status(self) -> IndiMeridianState:
        if self._policy is None:
            raise ConnectionError("INDI client is not connected")
        return classify_meridian(self.observe_mount(), self._policy)

    def start_supervision(self) -> None:
        if self._supervisor is None or not self._transport.is_open:
            raise ConnectionError("INDI client is not connected")
        self._supervisor.start()

    def emergency_stop(self, *, timeout: float = 5.0) -> IndiStopResult:
        """Request abort and tracking off without requiring time/site authority."""
        if not self._transport.is_open:
            raise ConnectionError("INDI client is not connected")
        return stop_mount_via_indi(
            self._transport, self.device, confirmation_timeout=timeout
        )

    def enable_tracking(self, *, timeout: float = 8.0) -> IndiTrackingResult:
        if not self._transport.is_open or self._status_reader is None:
            raise ConnectionError("INDI client is not connected")
        return enable_tracking_via_indi(
            self._transport, self.device, observe=self.observe_mount,
            meridian_status=self.meridian_status,
            emergency_stop=self.emergency_stop, timeout=timeout,
        )

    def route_park_to_home(
        self, *, unpark_timeout: float = 20.0, home_timeout: float = 120.0
    ) -> IndiHomeRouteResult:
        """Route mechanically; no normal motion is unlocked by this prototype."""
        self._require_home_motion()
        if self._home_router is None or not self._transport.is_open:
            raise ConnectionError("Connect with an INDI runtime configuration first")
        return self._home_router.park_to_home(
            unpark_timeout=unpark_timeout, home_timeout=home_timeout
        )

    def unpark(self, *, timeout: float = 20.0) -> IndiUnparkResult:
        """Leave PARKED without requesting a HOME slew."""
        self._require_home_motion()
        if self._home_router is None or not self._transport.is_open:
            raise ConnectionError("Connect with an INDI runtime configuration first")
        return self._home_router.unpark(timeout=timeout)

    def go_home(self, *, timeout: float = 120.0) -> IndiPositionResult:
        """Explicitly move to OnStep HOME and verify live arrival."""
        self._require_home_motion()
        if self._home_router is None or not self._transport.is_open:
            raise ConnectionError("Connect with an INDI runtime configuration first")
        return self._home_router.go_home(timeout=timeout)

    def park(self, *, timeout: float = 120.0) -> IndiPositionResult:
        """Explicitly move from confirmed HOME to OnStep PARK."""
        self._require_home_motion()
        if self._home_router is None or not self._transport.is_open:
            raise ConnectionError("Connect with an INDI runtime configuration first")
        return self._home_router.park(timeout=timeout)

    def _require_home_motion(self) -> None:
        if self._config is None or not self._config.home_motion_enabled:
            raise RuntimeError(
                "INDI HOME/PARK motion is disabled pending the supervised H-status check"
            )

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
            self._home_router = None
            self._transport.close()

    def __enter__(self) -> OnStepIndiClient:
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
