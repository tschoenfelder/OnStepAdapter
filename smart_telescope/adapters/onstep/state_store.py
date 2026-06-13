"""Throttled persistence for last-known OnStep controller state.

The store writes a small JSON file only when state changes meaningfully or a
minimum interval has elapsed. This avoids turning the Raspberry Pi SD card into
a write target on every status poll.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class OnStepStateStore:
    def __init__(self, path: str | Path, min_write_interval_s: float = 30.0) -> None:
        self._path = Path(path).expanduser()
        self._min_write_interval_s = min_write_interval_s
        self._last_write_at = 0.0
        self._last_signature = ""

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, Any] | None:
        try:
            if not self._path.exists():
                return None
            with self._path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def maybe_save(self, state: dict[str, Any], *, force: bool = False) -> bool:
        signature = json.dumps(self._signature_state(state), sort_keys=True, separators=(",", ":"))
        now = time.monotonic()
        if not force and now - self._last_write_at < self._min_write_interval_s:
            return False
        if not force and signature == self._last_signature:
            return False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(state)
        payload["schema"] = "onstep-last-state-v1"
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self._path)
        self._last_signature = signature
        self._last_write_at = now
        return True

    def _signature_state(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "parked": state.get("parked"),
            "home_confirmed": state.get("home_confirmed"),
            "home_reference_confirmed": state.get("home_reference_confirmed"),
            "park_pose_confirmed": state.get("park_pose_confirmed"),
            "mechanical_axis_position": state.get("mechanical_axis_position"),
            "ra": _round_or_none(state.get("ra"), 5),
            "dec": _round_or_none(state.get("dec"), 4),
            "status_raw": state.get("status_raw"),
            "safety_locked": state.get("safety_locked"),
            "safety_reason": state.get("safety_reason"),
        }


def _round_or_none(value: Any, digits: int) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None
