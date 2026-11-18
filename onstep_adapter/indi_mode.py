"""Shared operating-mode arbitration with one-shot PARK authorization."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import threading
import time
from typing import Iterator
from uuid import uuid4


class IndiOperatingMode(str, Enum):
    UNKNOWN = "unknown"
    ASTRONOMICAL = "astronomical"
    TERRESTRIAL = "terrestrial"


@dataclass(frozen=True)
class IndiModeStatus:
    mode: IndiOperatingMode
    generation: int
    shared: bool
    transition_authorized: bool
    updated_at: str | None
    error: str | None = None


@dataclass(frozen=True)
class IndiModeTransitionResult:
    previous_mode: IndiOperatingMode
    mode: IndiOperatingMode
    changed: bool
    generation: int
    park_authorization_consumed: bool
    mount_remains_parked: bool


@dataclass(frozen=True)
class _ParkAuthorization:
    token: str
    generation: int
    status_revision: int
    issued_at: str
    issued_monotonic: float


_FALLBACK_LOCK = threading.RLock()


class IndiModeController:
    """Persist mode across local clients; never persist transition authority."""

    SCHEMA = 1

    def __init__(self, state_path: str | None) -> None:
        self.path = Path(state_path).expanduser() if state_path else None
        self._authorization: _ParkAuthorization | None = None

    def status(self) -> IndiModeStatus:
        if self.path is None:
            return IndiModeStatus(
                IndiOperatingMode.UNKNOWN, 0, False, False, None,
                "shared_mode_state_not_configured",
            )
        try:
            with self._locked():
                data = self._read_locked()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return IndiModeStatus(
                IndiOperatingMode.UNKNOWN, 0, True, False, None,
                f"mode_state_unavailable: {exc}",
            )
        authorization = self._authorization
        return IndiModeStatus(
            mode=data["mode"],
            generation=data["generation"],
            shared=True,
            transition_authorized=(
                authorization is not None and
                authorization.generation == data["generation"] and
                time.monotonic() - authorization.issued_monotonic <= 30.0
            ),
            updated_at=data["updated_at"],
        )

    def authorize_after_park(self, *, status_revision: int) -> None:
        status = self.status()
        if not status.shared or status.error:
            self._authorization = None
            raise RuntimeError(status.error or "Shared mode state is unavailable")
        self._authorization = _ParkAuthorization(
            token=uuid4().hex,
            generation=status.generation,
            status_revision=status_revision,
            issued_at=datetime.now(timezone.utc).isoformat(),
            issued_monotonic=time.monotonic(),
        )

    def clear_authorization(self) -> None:
        self._authorization = None

    def set_mode(self, target: IndiOperatingMode | str, snapshot) -> IndiModeTransitionResult:
        requested = IndiOperatingMode(target)
        if requested is IndiOperatingMode.UNKNOWN:
            raise ValueError("UNKNOWN is observational and cannot be selected")
        if self.path is None:
            raise RuntimeError("Shared mode state is not configured; transition refused")
        if not snapshot.status_live or not snapshot.parked:
            raise RuntimeError("Fresh verified PARKED status is required")
        if snapshot.slewing or snapshot.tracking or snapshot.guiding or snapshot.at_limit:
            raise RuntimeError("Mode transition requires parked, idle, non-tracking status")
        if not getattr(snapshot, "park_evidence_consistent", False):
            raise RuntimeError("Raw and standard INDI PARK evidence is unavailable or conflicting")
        authorization = self._authorization
        if authorization is None:
            raise RuntimeError("An explicit verified park() is required for this transition")
        if time.monotonic() - authorization.issued_monotonic > 30.0:
            self._authorization = None
            raise RuntimeError("Explicit PARK authorization expired; call park() again")
        try:
            with self._locked():
                data = self._read_locked()
                if authorization.generation != data["generation"]:
                    raise RuntimeError("Mode changed in another client; PARK authorization is stale")
                if snapshot.status_revision < authorization.status_revision:
                    raise RuntimeError("PARK evidence predates this transition authorization")
                previous = data["mode"]
                if previous is requested:
                    changed = False
                    generation = data["generation"]
                else:
                    changed = True
                    generation = data["generation"] + 1
                    self._write_locked(requested, generation)
        finally:
            self._authorization = None
        return IndiModeTransitionResult(
            previous_mode=previous,
            mode=requested,
            changed=changed,
            generation=generation,
            park_authorization_consumed=True,
            mount_remains_parked=True,
        )

    def require(self, expected: IndiOperatingMode) -> IndiModeStatus:
        status = self.status()
        if status.error or status.mode is not expected:
            raise RuntimeError(
                f"Operation requires {expected.value} mode; current mode={status.mode.value}"
            )
        return status

    @contextmanager
    def _locked(self) -> Iterator[None]:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with _FALLBACK_LOCK:
            with lock_path.open("a+b") as stream:
                try:
                    import fcntl  # type: ignore[import-not-found]
                except ImportError:
                    fcntl = None
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _read_locked(self) -> dict:
        assert self.path is not None
        if not self.path.exists():
            return {
                "mode": IndiOperatingMode.UNKNOWN,
                "generation": 0,
                "updated_at": None,
            }
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("schema") != self.SCHEMA:
            raise ValueError("unsupported mode-state schema")
        return {
            "mode": IndiOperatingMode(data["mode"]),
            "generation": int(data["generation"]),
            "updated_at": str(data["updated_at"]),
        }

    def _write_locked(self, mode: IndiOperatingMode, generation: int) -> None:
        assert self.path is not None
        payload = {
            "schema": self.SCHEMA,
            "mode": mode.value,
            "generation": generation,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        temporary = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)
