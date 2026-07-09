"""One-owner connection facade for OnStep mount and focuser control."""

from __future__ import annotations

from types import TracebackType

from .focuser import OnStepFocuser
from .mount import OnStepMount
from .results import OnStepConnectionResult, OnStepMotionCalibration
from .safety import OnStepSafetyConfig
from .serial_bus import OnStepSerialBus


class OnStepClient:
    """Own one serial bus and expose separate mount and focuser adapters."""

    def __init__(
        self,
        port: str,
        *,
        baud_rate: int = 9600,
        timeout: float = 2.0,
        safety_config: OnStepSafetyConfig | None = None,
        motion_calibration: OnStepMotionCalibration | None = None,
        serial_bus: OnStepSerialBus | None = None,
    ) -> None:
        self.port = port
        self._bus = serial_bus or OnStepSerialBus()
        self.mount = OnStepMount(
            port,
            baud_rate=baud_rate,
            timeout=timeout,
            safety_config=safety_config,
            motion_calibration=motion_calibration,
            serial_bus=self._bus,
        )
        self.focuser = OnStepFocuser(
            self._bus,
            safety_config=self.mount.safety_config,
        )
        self._closed = False

    @property
    def is_open(self) -> bool:
        return self._bus.is_open

    def connect(self) -> OnStepConnectionResult:
        self._closed = False
        mount_connected = self.mount.connect()
        if mount_connected:
            self.focuser.connect()
        return OnStepConnectionResult(
            connected=mount_connected,
            mount_connected=mount_connected,
            focuser_available=self.focuser.is_available,
            port=self.port,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus.close()

    def __enter__(self) -> "OnStepClient":
        result = self.connect()
        if not result.connected:
            raise ConnectionError(f"Could not connect to OnStep on {self.port}")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
