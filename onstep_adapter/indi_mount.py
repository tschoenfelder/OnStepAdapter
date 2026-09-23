"""Mount facade for the in-process INDI client."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .indi_axis_motion import AxisMotionMode

if TYPE_CHECKING:
    from .indi_client import OnStepIndiClient


class IndiMount:
    def __init__(self, client: OnStepIndiClient) -> None:
        self._client = client

    def get_status(self):
        return self._client.observe_mount()

    def unpark(self):
        return self._client.unpark()

    def go_home(self):
        return self._client.go_home()

    def park(self):
        return self._client.park()

    def stop(self):
        return self._client.emergency_stop()

    def meridian_status(self):
        return self._client.meridian_status()

    def move_ra_axis_deg(
        self, offset_deg: float, *, timeout_s: float = 30.0,
        mode: AxisMotionMode | str = AxisMotionMode.TERRESTRIAL,
    ):
        """Finite same-pier RA-axis target; positive increases hour angle."""
        return self._client.move_axis_deg(
            "ra", offset_deg, timeout_s=timeout_s, mode=mode
        )

    def move_dec_axis_deg(
        self, offset_deg: float, *, timeout_s: float = 30.0,
        mode: AxisMotionMode | str = AxisMotionMode.TERRESTRIAL,
    ):
        """Finite same-pier DEC-axis target; positive is northward."""
        return self._client.move_axis_deg(
            "dec", offset_deg, timeout_s=timeout_s, mode=mode
        )

    def move_ra(
        self, offset_arcsec: float, *, mode: str = "manual", timeout_s: float = 30.0
    ):
        """HOME-neutral local RA move in arcseconds, compatible with 0.3."""
        if mode != "manual":
            raise ValueError("INDI 0.4 axis-angle movement supports mode='manual' only")
        return self.move_ra_axis_deg(offset_arcsec / 3600.0, timeout_s=timeout_s)

    def move_dec(
        self, offset_arcsec: float, *, mode: str = "manual", timeout_s: float = 30.0
    ):
        """HOME-neutral local DEC move in arcseconds, compatible with 0.3."""
        if mode != "manual":
            raise ValueError("INDI 0.4 axis-angle movement supports mode='manual' only")
        return self.move_dec_axis_deg(offset_arcsec / 3600.0, timeout_s=timeout_s)

    def goto(self, ra_hours: float, dec_deg: float):
        raise NotImplementedError(
            "INDI goto is unavailable until the 0.4 safety and HOME gates are validated"
        )

    def enable_tracking(self):
        return self._client.enable_tracking()
