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
    def park(self, *, operator_confirmed_existing_park: bool = False):
        """Start the move to the stored PARK; returns a ``MotionHandle`` at once."""
        return self._client.park(
            operator_confirmed_existing_park=operator_confirmed_existing_park
        )

    def mode_status(self):
        return self._client.mode_status()

    def set_mode(self, target):
        """Consume a fresh explicit-PARK authorization and change mode only."""
        return self._client.set_mode(target)

    def move_toward_north_horizon(self, step_deg: float | None = None, *, timeout_s=45.0):
        return self._client.move_toward_north_horizon(step_deg, timeout_s=timeout_s)

    def move_away_from_north_horizon(self, step_deg: float | None = None, *, timeout_s=45.0):
        return self._client.move_away_from_north_horizon(step_deg, timeout_s=timeout_s)

    def move_terrestrial_ra_axis(self, offset_deg: float, *, timeout_s=45.0):
        """Bounded signed Axis-1 jog; positive westward, negative eastward."""
        return self._client.move_terrestrial_ra_axis(
            offset_deg, timeout_s=timeout_s
        )

    def recover_terrestrial_ra(self, direction: str, duration_s: float):
        """Run one operator-observed, time-bounded Axis-1 recovery nudge."""
        return self._client.recover_terrestrial_ra(direction, duration_s)

    def recover_terrestrial_dec(self, direction: str, duration_s: float):
        """Run one operator-observed, time-bounded Axis-2 recovery nudge."""
        return self._client.recover_terrestrial_dec(direction, duration_s)

    def move_terrestrial_ra_timed(self, direction: str, duration_s: float):
        """Run one normal timed Axis-1 jog in an established terrestrial session."""
        return self._client.move_terrestrial_ra_timed(direction, duration_s)

    def move_terrestrial_dec_timed(self, direction: str, duration_s: float):
        """Run one normal timed Axis-2 jog in an established terrestrial session."""
        return self._client.move_terrestrial_dec_timed(direction, duration_s)

    def move_terrestrial_ra_deg(
        self, offset_deg: float, *, timeout_s: float = 30.0,
    ):
        """Finite same-pier RA jog after this client established PARK -> UNPARK."""
        return self._client.move_terrestrial_axis_deg(
            "ra", offset_deg, timeout_s=timeout_s,
        )

    def move_terrestrial_dec_deg(
        self, offset_deg: float, *, timeout_s: float = 30.0,
    ):
        """Finite same-pier DEC jog after this client established PARK -> UNPARK."""
        return self._client.move_terrestrial_axis_deg(
            "dec", offset_deg, timeout_s=timeout_s,
        )

    @_traced
    def reconnect_device(self, *, timeout: float = 20.0):
        """Reopen the INDI driver's link to the controller (DISCONNECT, CONNECT) and wait for
        fresh OnStep status; never moves the mount. Returns an ``IndiReconnectResult``."""
        return self._client.reconnect_device(timeout=timeout)

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
    def jog_ra(self, direction: int, **options):
        """Hold-to-move RA: +1 west (increasing HA), -1 east. Returns a
        ``JogHandle`` at once; ``renew()`` it while held, ``release()`` on
        release. Refused until DEC has moved (see ``ra_axis_defined``)."""
        return self._client.jog("ra", direction, **options)

    @_traced
    def jog_dec(self, direction: int, **options):
        """Hold-to-move DEC: +1 north, -1 south. Returns a ``JogHandle``."""
        return self._client.jog("dec", direction, **options)

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
