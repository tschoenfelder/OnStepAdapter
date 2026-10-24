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
    status_revision: int = -1
    park_evidence_consistent: bool = False


class IndiHomeRouter:
    """Mechanical route only; no astronomical goto or implicit park on failure."""

    def __init__(
        self, transport: IndiTransport, reader: IndiStatusReader, device: str,
        *, park_stable_hold_seconds: float = 0.0,
    ) -> None:
        self.transport = transport
        self.reader = reader
        self.device = device
        self.authority_established = False
        self.park_stable_hold_seconds = park_stable_hold_seconds

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
        before = self._fresh_preflight(timeout=timeout)
        if not self._ready(before) or before.parked or before.slewing or before.tracking:
            return IndiPositionResult(
                "home", False, before.raw_status,
                "Fresh unparked, stationary, non-tracking status is required", None,
            )
        return self._move_to_home(timeout=timeout)

    def park(
        self, *, timeout: float = 120.0,
        operator_confirmed_existing_park: bool = False,
    ) -> IndiPositionResult:
        """Move directly to stored PARK; never silently route via HOME."""
        before = self.reader.read()
        if before.parked:
            return self._accept_existing_park(
                before,
                operator_confirmed=operator_confirmed_existing_park,
            )
        before = self._fresh_preflight(timeout=timeout)
        standard = self.transport.get_property(self.device, "TELESCOPE_PARK")
        park_pending = bool(
            standard is not None and (
                standard.state.lower() == "busy" or
                (standard.values.get("PARK") == "On" and not before.parked)
            )
        )
        if park_pending:
            return IndiPositionResult(
                "park", False, before.raw_status,
                "PARK_PENDING: existing PARK trajectory has not completed", None,
            )
        if not self._ready(before) or before.slewing or before.tracking:
            return IndiPositionResult(
                "park", False, before.raw_status,
                "Fresh stationary, non-tracking and fault-free status is required", None,
            )
        self.authority_established = False
        if before.parked:
            return self._accept_existing_park(
                before,
                operator_confirmed=operator_confirmed_existing_park,
            )
        return self._move_to_park(timeout=timeout)

    def _accept_existing_park(
        self, before, *, operator_confirmed: bool,
    ) -> IndiPositionResult:
        fatal = {
            "indi_device_disconnected", "onstep_status_alert",
            "onstep_status_unavailable", "onstep_reported_error",
            "onstep_limit_or_park_fault", "park_state_conflict",
        }
        safe = bool(
            before.raw_status and before.park_evidence_consistent and
            not before.slewing and not before.tracking and not before.at_limit and
            not fatal.intersection(before.blockers)
        )
        if not operator_confirmed:
            return IndiPositionResult(
                "park", False, before.raw_status,
                "Existing PARK requires explicit physical operator confirmation",
                None,
            )
        if not safe:
            return IndiPositionResult(
                "park", False, before.raw_status,
                "Operator confirmation cannot override conflicting or unsafe PARK evidence",
                None,
            )
        self.authority_established = False
        return IndiPositionResult(
            "park", True, before.raw_status, None, None,
            status_revision=before.status_revision,
            park_evidence_consistent=True,
        )

    def _move_to_park(self, *, timeout: float) -> IndiPositionResult:
        raw_before = self.transport.get_property(self.device, "OnStep Status")
        park_before = self.transport.get_property(self.device, "TELESCOPE_PARK")
        raw_revision = raw_before.revision if raw_before is not None else -1
        park_revision = park_before.revision if park_before is not None else -1
        last_raw = raw_before.values.get(":GU# return") if raw_before else None
        try:
            self.transport.issue_switch(self.device, "TELESCOPE_PARK", "PARK")
            status = self._wait_park_completion(
                timeout=timeout,
                after_status_revision=raw_revision,
                after_park_revision=park_revision,
            )
            return IndiPositionResult(
                "park", True, status["raw"], None, None,
                status_revision=status["_status_revision"],
                park_evidence_consistent=True,
            )
        except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
            try:
                stopped = stop_mount_via_indi(self.transport, self.device)
            except (ConnectionError, RuntimeError, TimeoutError, ValueError) as stop_exc:
                return IndiPositionResult(
                    "park", False, last_raw, f"{exc}; stop also failed: {stop_exc}", None
                )
            return IndiPositionResult("park", False, last_raw, str(exc), stopped)

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
            return IndiPositionResult(
                destination, True, status["raw"], None, None,
                status_revision=status["_status_revision"],
            )
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
                 "onstep_reported_error", "onstep_limit_or_park_fault",
                 "park_state_conflict"}.intersection(
                     before.blockers
                 )
        )

    def unpark(self, *, timeout: float = 20.0) -> IndiUnparkResult:
        """Leave PARKED and disable resumed tracking; never request HOME."""
        self.authority_established = False
        before = self._fresh_preflight(timeout=timeout)
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
            if not status["tracking"]:
                return IndiUnparkResult(True, "unparked", last_raw, None, None)
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

    def _fresh_preflight(self, *, timeout: float):
        """Wait for one post-subscription OnStep update before motion checks."""
        before = self.reader.read()
        if before.status_live:
            return before
        current = self.transport.get_property(self.device, "OnStep Status")
        revision = current.revision if current is not None else -1
        try:
            self.transport.wait_property(
                self.device,
                "OnStep Status",
                timeout=min(max(timeout, 0.0), 5.0),
                after_revision=revision,
            )
        except (ConnectionError, RuntimeError, TimeoutError, ValueError):
            return before
        return self.reader.read()

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
        decoded["_status_revision"] = revision
        return decoded

    def _wait_park_completion(
        self, *, timeout: float, after_status_revision: int,
        after_park_revision: int,
    ) -> dict:
        deadline = time.monotonic() + timeout
        revision = after_status_revision
        park_fresh = False
        stable_since: float | None = None
        stable_polls = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("OnStep did not provide stable, reconciled PARK evidence")
            current = self.transport.wait_property(
                self.device, "OnStep Status", timeout=remaining,
                after_revision=revision,
            )
            revision = current.revision
            raw = current.values.get(":GU# return", "")
            decoded = decode_gu(raw) if raw else {}
            if (
                not decoded or current.state.lower() == "alert" or
                current.values.get("Error", "None") not in {"None", "", "0"} or
                decoded.get("park_failed") or decoded.get("at_limit")
            ):
                raise RuntimeError("OnStep fault or limit during PARK")
            standard = self.transport.get_property(self.device, "TELESCOPE_PARK")
            if standard is not None and standard.revision > after_park_revision:
                park_fresh = True
            standard_parked = bool(
                standard is not None and
                standard.state.lower() in {"idle", "ok"} and
                standard.values.get("PARK") == "On" and
                standard.values.get("UNPARK", "Off") != "On"
            )
            reconciled = bool(
                park_fresh and standard_parked and decoded.get("parked") and
                decoded.get("not_slewing") and not decoded.get("tracking")
            )
            if reconciled:
                stable_polls += 1
                stable_since = stable_since or time.monotonic()
                if (
                    stable_polls >= 2 and
                    time.monotonic() - stable_since >= self.park_stable_hold_seconds
                ):
                    decoded["_status_revision"] = revision
                    return decoded
            else:
                stable_polls = 0
                stable_since = None
