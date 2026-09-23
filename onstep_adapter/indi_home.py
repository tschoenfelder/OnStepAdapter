"""Status-confirmed mechanical PARK to HOME route through INDI."""

from __future__ import annotations

from dataclasses import dataclass
import time

from .indi_status import IndiStatusReader
from .indi_stop import IndiStopResult, stop_mount_via_indi
from .indi_transport import IndiTransport
from .indi_gu import decode_gu


@dataclass(frozen=True)
class IndiHomeRouteResult:
    authority_established: bool
    stage: str
    final_raw_status: str | None
    error: str | None
    stop_result: IndiStopResult | None


@dataclass(frozen=True)
class IndiUnparkResult:
    unparked_confirmed: bool
    stage: str
    final_raw_status: str | None
    error: str | None
    stop_result: IndiStopResult | None


@dataclass(frozen=True)
class IndiPositionResult:
    destination: str
    confirmed: bool
    final_raw_status: str | None
    error: str | None
    stop_result: IndiStopResult | None


class IndiHomeRouter:
    """Mechanical route only; no astronomical goto or implicit park on failure."""

    def __init__(self, transport: IndiTransport, reader: IndiStatusReader, device: str) -> None:
        self.transport = transport
        self.reader = reader
        self.device = device
        self.authority_established = False

    def park_to_home(
        self,
        *,
        unpark_timeout: float = 20.0,
        home_timeout: float = 120.0,
    ) -> IndiHomeRouteResult:
        unparked = self.unpark(timeout=unpark_timeout)
        if not unparked.unparked_confirmed:
            return IndiHomeRouteResult(
                False, unparked.stage, unparked.final_raw_status,
                unparked.error, unparked.stop_result,
            )
        home = self._move_to_home(timeout=home_timeout)
        return IndiHomeRouteResult(
            home.confirmed, "home_confirmed" if home.confirmed else "home",
            home.final_raw_status, home.error, home.stop_result,
        )

    def go_home(self, *, timeout: float = 120.0) -> IndiPositionResult:
        """Explicitly move an unparked, stationary mount to controller HOME."""
        before = self.reader.read()
        if not self._ready(before) or before.parked or before.slewing or before.tracking:
            return IndiPositionResult(
                "home", False, before.raw_status,
                "Fresh unparked, stationary, non-tracking status is required", None,
            )
        return self._move_to_home(timeout=timeout)

    def park(self, *, timeout: float = 120.0) -> IndiPositionResult:
        """Move from confirmed HOME to stored PARK; never silently route via HOME."""
        before = self.reader.read()
        if (
            not self._ready(before) or not before.at_home or before.parked or
            before.slewing or before.tracking
        ):
            return IndiPositionResult(
                "park", False, before.raw_status,
                "Fresh HOME, stationary, non-tracking status is required", None,
            )
        self.authority_established = False
        return self._move_to_position(
            destination="park", property_name="TELESCOPE_PARK", element="PARK",
            predicate=lambda decoded: (
                decoded["parked"] and decoded["not_slewing"] and
                not decoded["tracking"] and not decoded["park_failed"]
            ),
            timeout=timeout,
        )

    def _move_to_home(self, *, timeout: float) -> IndiPositionResult:
        result = self._move_to_position(
            destination="home", property_name="TELESCOPE_HOME", element="GO",
            predicate=lambda decoded: (
                decoded["at_home"] and decoded["not_parked"] and
                decoded["not_slewing"] and not decoded["tracking"] and
                not decoded["park_failed"]
            ),
            timeout=timeout,
        )
        self.authority_established = result.confirmed
        return result

    def _move_to_position(
        self, *, destination: str, property_name: str, element: str,
        predicate, timeout: float,
    ) -> IndiPositionResult:
        current = self.transport.get_property(self.device, "OnStep Status")
        last_raw = current.values.get(":GU# return") if current else None
        revision = current.revision if current is not None else -1
        try:
            self.transport.issue_switch(self.device, property_name, element)
            status = self._wait_status(
                predicate, timeout=timeout, stable_polls=2,
                after_revision=revision,
            )
            return IndiPositionResult(destination, True, status["raw"], None, None)
        except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
            try:
                stopped = stop_mount_via_indi(self.transport, self.device)
            except (ConnectionError, RuntimeError, TimeoutError, ValueError) as stop_exc:
                return IndiPositionResult(
                    destination, False, last_raw,
                    f"{exc}; stop also failed: {stop_exc}", None,
                )
            return IndiPositionResult(destination, False, last_raw, str(exc), stopped)

    @staticmethod
    def _ready(before) -> bool:
        return (
            before.status_live and not before.at_limit and
            not {"indi_device_disconnected", "onstep_status_alert",
                 "onstep_reported_error", "onstep_limit_or_park_fault"}.intersection(
                     before.blockers
                 )
        )

    def unpark(self, *, timeout: float = 20.0) -> IndiUnparkResult:
        """Leave PARKED and disable resumed tracking; never request HOME."""
        self.authority_established = False
        before = self.reader.read()
        if not self._ready(before) or not before.parked or before.slewing:
            return IndiUnparkResult(
                False, "preflight", before.raw_status,
                "Fresh parked, stationary and fault-free status is required", None,
            )
        last_raw = before.raw_status
        try:
            before_unpark = self.transport.get_property(self.device, "OnStep Status")
            revision = before_unpark.revision if before_unpark is not None else -1
            self.transport.issue_switch(self.device, "TELESCOPE_PARK", "UNPARK")
            status = self._wait_status(
                lambda decoded: (
                    decoded["not_parked"] and decoded["not_slewing"] and
                    not decoded["park_failed"]
                ),
                timeout=timeout,
                after_revision=revision,
            )
            last_raw = status["raw"]
            before_off = self.transport.get_property(self.device, "OnStep Status")
            off_revision = before_off.revision if before_off is not None else -1
            # The lx200_OnStep driver can apply TRACK_OFF without publishing a
            # fresh TELESCOPE_TRACK_STATE vector.  Do not treat that missing
            # echo as a failure: the continuously refreshed OnStep Status
            # below is the authoritative confirmation for this transaction.
            self.transport.issue_switch(
                self.device, "TELESCOPE_TRACK_STATE", "TRACK_OFF"
            )
            status = self._wait_status(
                lambda decoded: (
                    decoded["not_parked"] and decoded["not_slewing"] and
                    not decoded["tracking"] and not decoded["park_failed"]
                ),
                timeout=timeout,
                after_revision=off_revision,
            )
            last_raw = status["raw"]
            return IndiUnparkResult(True, "unparked", last_raw, None, None)
        except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
            try:
                stopped = stop_mount_via_indi(self.transport, self.device)
            except (ConnectionError, RuntimeError, TimeoutError, ValueError) as stop_exc:
                return IndiUnparkResult(
                    False, "unpark", last_raw, f"{exc}; stop also failed: {stop_exc}", None
                )
            return IndiUnparkResult(False, "unpark", last_raw, str(exc), stopped)

    def _wait_status(
        self, predicate, *, timeout: float, stable_polls: int = 1,
        after_revision: int | None = None,
    ) -> dict:
        current = self.transport.get_property(self.device, "OnStep Status")
        revision = (
            after_revision if after_revision is not None
            else current.revision if current is not None else -1
        )
        deadline = time.monotonic() + timeout
        stable = 0
        while stable < stable_polls:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("OnStep did not confirm the mechanical route")
            current = self.transport.wait_property(
                self.device, "OnStep Status", timeout=remaining, after_revision=revision
            )
            revision = current.revision
            raw = current.values.get(":GU# return", "")
            if not raw:
                raise RuntimeError("OnStep status is unavailable during mechanical route")
            decoded = decode_gu(raw)
            if (
                current.state.lower() == "alert" or
                current.values.get("Error", "None") not in {"None", "", "0"} or
                decoded["park_failed"] or decoded["at_limit"]
            ):
                raise RuntimeError("OnStep fault or limit during mechanical route")
            stable = stable + 1 if predicate(decoded) else 0
        return decoded
