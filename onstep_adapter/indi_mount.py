"""Mount facade for the in-process INDI client."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .indi_axis_motion import AxisMotionMode
from .indi_guiding import IndiGuideCalibration
from .indi_trace import traced_operation

if TYPE_CHECKING:
    from .indi_client import OnStepIndiClient


_traced = traced_operation(lambda mount: getattr(mount._client, "trace", None))


class IndiMount:
    def __init__(self, client: OnStepIndiClient) -> None:
        self._client = client

    @_traced
    def get_status(self):
        return self._client.observe_mount()

    @_traced
    def unpark(self):
        return self._client.unpark()

    @_traced
    def go_home(self):
        """Start the move to HOME; returns a ``MotionHandle`` at once."""
        return self._client.go_home()

    @_traced
    def park(self):
        """Start the move to the stored PARK; returns a ``MotionHandle`` at once."""
        return self._client.park()

    @_traced
    def stop(self):
        """Stop all motion now, including a running handle's move."""
        return self._client.emergency_stop()

    @_traced
    def guide_pulse(
        self, direction: str, duration_ms: int, *, command_timeout: float = 3.0
    ):
        """Apply one bounded astronomical guide pulse while tracking remains on."""
        return self._client.guide_pulse(
            direction, duration_ms, command_timeout=command_timeout
        )

    @_traced
    def guide(self, direction: str, duration_ms: int) -> bool:
        """Compatibility wrapper returning whether a guide pulse completed."""
        return self.guide_pulse(direction, duration_ms).pulse_completed

    @_traced
    def guide_correction_ra(
        self,
        offset_arcsec: float,
        *,
        calibration: IndiGuideCalibration | None = None,
        command_timeout: float = 3.0,
    ):
        """Apply a calibrated sky-RA correction while tracking remains on.

        Positive offsets guide east and negative offsets guide west. INDI guide
        directions are sky directions and therefore do not invert by pier side.
        """
        return self._client.guide_correction(
            "ra", offset_arcsec, calibration=calibration,
            command_timeout=command_timeout,
        )

    @_traced
    def guide_correction_dec(
        self,
        offset_arcsec: float,
        *,
        calibration: IndiGuideCalibration | None = None,
        command_timeout: float = 3.0,
    ):
        """Apply a calibrated sky-DEC correction while tracking remains on.

        Positive offsets guide north and negative offsets guide south.
        """
        return self._client.guide_correction(
            "dec", offset_arcsec, calibration=calibration,
            command_timeout=command_timeout,
        )

    def get_guide_calibration(self):
        return self._client.get_guide_calibration()

    def set_guide_calibration(self, calibration: IndiGuideCalibration | None) -> None:
        self._client.set_guide_calibration(calibration)

    @_traced
    def meridian_status(self):
        return self._client.meridian_status()

    @property
    def ra_axis_defined(self) -> bool:
        """False after connecting and at HOME until DEC has moved; RA moves are
        refused until then (move DEC first)."""
        return self._client.ra_axis_defined

    @_traced
    def move_ra_axis_deg(
        self, offset_deg: float, *, timeout_s: float = 30.0,
        mode: AxisMotionMode | str = AxisMotionMode.TERRESTRIAL,
    ):
        """Finite same-pier RA-axis target; positive increases hour angle.

        Refused with ``RaAxisUndefinedError`` after connecting and at HOME until
        a DEC axis move has completed (see ``ra_axis_defined``).

        Returns a ``MotionHandle`` at once; ``wait()`` gives the
        ``IndiAxisMoveResult``.
        """
        return self._client.move_axis_deg(
            "ra", offset_deg, timeout_s=timeout_s, mode=mode
        )

    @_traced
    def move_dec_axis_deg(
        self, offset_deg: float, *, timeout_s: float = 30.0,
        mode: AxisMotionMode | str = AxisMotionMode.TERRESTRIAL,
    ):
        """Finite same-pier DEC-axis target; positive is northward.

        Returns a ``MotionHandle`` at once; ``wait()`` gives the
        ``IndiAxisMoveResult``.
        """
        return self._client.move_axis_deg(
            "dec", offset_deg, timeout_s=timeout_s, mode=mode
        )

    @_traced
    def move_ra(
        self, offset_arcsec: float, *, mode: str = "manual", timeout_s: float = 30.0
    ):
        """HOME-neutral local RA move in arcseconds, compatible with 0.3."""
        if mode != "manual":
            raise ValueError("INDI 0.4 axis-angle movement supports mode='manual' only")
        return self.move_ra_axis_deg(offset_arcsec / 3600.0, timeout_s=timeout_s)

    @_traced
    def move_dec(
        self, offset_arcsec: float, *, mode: str = "manual", timeout_s: float = 30.0
    ):
        """HOME-neutral local DEC move in arcseconds, compatible with 0.3."""
        if mode != "manual":
            raise ValueError("INDI 0.4 axis-angle movement supports mode='manual' only")
        return self.move_dec_axis_deg(offset_arcsec / 3600.0, timeout_s=timeout_s)

    @_traced
    def goto(self, ra_hours: float, dec_deg: float):
        raise NotImplementedError(
            "INDI goto is unavailable until the 0.4 safety and HOME gates are validated"
        )

    @_traced
    def enable_tracking(self):
        return self._client.enable_tracking()
