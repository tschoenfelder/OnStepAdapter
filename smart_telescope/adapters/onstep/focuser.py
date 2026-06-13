"""OnStep V4 focuser adapter — delegates serial I/O to OnStepSerialBus.

The OnStep controller uses a single serial port for both mount and focuser
commands.  OnStepFocuser holds no serial handle of its own; it calls methods
on OnStepSerialBus so that all traffic shares the mount's lock.
"""

from __future__ import annotations

import logging
import json
import time
from pathlib import Path

from ...ports.focuser import FocuserPort
from .safety import OnStepSafetyConfig, OnStepSafetyError, SafetyViolation
from .results import FocuserMoveResult, FocuserStatus
from .serial_bus import OnStepSerialBus

_log = logging.getLogger(__name__)

_MAX_FA_ATTEMPTS = 3
_FA_RETRY_DELAY_S = 0.3


class OnStepFocuser(FocuserPort):
    """Controls the OnStep focuser via the shared serial bus.

    Args:
        bus: OnStepSerialBus instance obtained from OnStepMount.serial_bus.
    """

    def __init__(
        self,
        bus: OnStepSerialBus,
        safety_config: OnStepSafetyConfig | None = None,
    ) -> None:
        if safety_config is None:
            from .mount import _default_safety_config
            safety_config = _default_safety_config()
        self._bus = bus
        self._safety_config = safety_config
        self._available: bool = False
        self._max_position: int = 0

    # ── FocuserPort ───────────────────────────────────────────────────────────

    def connect(self) -> bool:
        if self._available:
            if not self._max_position:
                self._max_position = self._fetch_max_position()
                if not self._max_position:
                    self._max_position = self._safety_config.focuser_max_position
            _log.info(
                "OnStepFocuser.connect(): already available; using cached max_position=%d",
                self._max_position,
            )
            return True

        for attempt in range(_MAX_FA_ATTEMPTS):
            if attempt:
                time.sleep(_FA_RETRY_DELAY_S)
            reply = self._bus.send(":FA#")
            self._available = reply == "1"
            _log.info(
                "OnStepFocuser.connect(): attempt=%d :FA# reply=%r available=%s",
                attempt + 1, reply, self._available,
            )
            if self._available:
                break
        if self._available:
            self._max_position = self._fetch_max_position()
            calibrated_max = self._load_calibrated_max_position()
            if not self._max_position:
                self._max_position = calibrated_max or self._safety_config.focuser_max_position
            elif calibrated_max:
                self._max_position = min(self._max_position, calibrated_max)
            _log.info("OnStepFocuser.connect(): max_position=%d", self._max_position)
        else:
            _log.warning(
                "OnStepFocuser.connect(): focuser not available after %d attempts"
                " — check OnStep focuser wiring/config",
                _MAX_FA_ATTEMPTS,
            )
        return True

    def disconnect(self) -> None:
        pass  # serial owned by OnStepSerialBus

    @property
    def is_available(self) -> bool:
        return self._available

    def get_position(self) -> int:
        reply = self._bus.send(":FG#")
        try:
            return int(reply)
        except ValueError:
            return 0

    def get_max_position(self) -> int:
        return self._max_position

    def status(self) -> FocuserStatus:
        return FocuserStatus(
            available=self._available,
            position=self.get_position() if self._available else 0,
            max_position=self._max_position,
            moving=self.is_moving() if self._available else False,
        )

    def set_calibrated_max_position(self, max_position: int) -> int:
        self._max_position = max(self._safety_config.focuser_min_position, int(max_position))
        return self._max_position

    def move(self, steps: int) -> None:
        self.move_absolute(steps)

    def move_absolute(self, steps: int) -> FocuserMoveResult:
        min_pos = self._safety_config.focuser_min_position
        max_pos = self._max_position or self._safety_config.focuser_max_position
        if steps < min_pos or steps > max_pos:
            raise OnStepSafetyError(SafetyViolation(
                reason="focuser_limit",
                command="focuser_move",
                axis="focuser",
                target_value=steps,
                limit_value=max_pos if steps > max_pos else min_pos,
                recovery_hint="Move within the configured/learned OnStep focuser range.",
            ))
        _log.info("OnStepFocuser.move(): steps=%d", steps)
        start_position = self.get_position() if self._available else 0
        reply = self._bus.send_fixed(f":FS{steps}#", size=1, timeout=1.0)
        if reply != "1":
            raise OnStepSafetyError(SafetyViolation(
                reason="focuser_move_rejected",
                command="focuser_move",
                axis="focuser",
                current_value=start_position,
                target_value=steps,
                onstep_reply=reply,
                recovery_hint="Check OnStep focuser state, limits, and motor configuration.",
            ))
        return FocuserMoveResult(
            accepted=True,
            target_position=steps,
            start_position=start_position,
            onstep_reply=reply,
        )

    def is_moving(self) -> bool:
        return self._bus.send(":FT#") == "M"

    def stop(self) -> None:
        # Bypass the serial lock — stop must be immediate even when another
        # command is in progress.  Write-only: no response expected from :FQ#.
        self._bus.write_bypass(b":FQ#")

    # ── private ───────────────────────────────────────────────────────────────

    def _fetch_max_position(self) -> int:
        reply = self._bus.send(":FM#")
        try:
            return int(reply)
        except ValueError:
            return 0

    def _load_calibrated_max_position(self) -> int:
        try:
            from ... import config

            base = Path(config.APP_STATE_DIR).expanduser() if config.APP_STATE_DIR else Path.home() / ".SmartTScope"
            path = base / "onstep_focuser_calibration.json"
            if not path.exists():
                return 0
            data = json.loads(path.read_text(encoding="utf-8"))
            return int(data.get("max_position") or 0)
        except Exception:
            return 0
