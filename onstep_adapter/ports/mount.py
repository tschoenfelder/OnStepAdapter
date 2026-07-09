from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, Literal


class MountState(Enum):
    UNKNOWN = auto()
    PARKED = auto()
    UNPARKED = auto()
    SLEWING = auto()
    TRACKING = auto()
    AT_LIMIT = auto()


@dataclass
class MountPosition:
    ra: float   # hours
    dec: float  # degrees


class MountPort(ABC):
    @abstractmethod
    def connect(self) -> bool: ...

    @abstractmethod
    def get_state(self) -> MountState: ...

    @abstractmethod
    def unpark(self) -> bool: ...

    def recovery_unpark_stop_tracking(self) -> dict[str, object]:
        """Unpark for supervised recovery and leave tracking disabled if possible."""
        accepted = self.unpark()
        state_after_unpark = self.get_state()
        tracking_disable_sent = False
        tracking_disable_ok = None
        final_state = state_after_unpark
        if state_after_unpark == MountState.TRACKING:
            tracking_disable_sent = True
            tracking_disable_ok = self.disable_tracking()
            final_state = self.get_state()
        return {
            "accepted": accepted,
            "state_after_unpark": state_after_unpark.name.lower(),
            "tracking_disable_sent": tracking_disable_sent,
            "tracking_disable_ok": tracking_disable_ok,
            "final_state": final_state.name.lower(),
            "ok": bool(accepted and final_state != MountState.PARKED and final_state != MountState.TRACKING),
        }

    def recovery_offset(
        self,
        *,
        ra_offset_h: float = 0.0,
        dec_offset_deg: float = 0.0,
        max_ra_offset_h: float = 1.0,
        max_dec_offset_deg: float = 5.0,
    ) -> dict[str, object]:
        """Perform a bounded watched recovery offset. Adapters may override."""
        raise NotImplementedError("recovery_offset is not supported by this mount adapter")

    @abstractmethod
    def enable_tracking(self) -> bool: ...

    @abstractmethod
    def get_position(self) -> MountPosition: ...

    @abstractmethod
    def sync(self, ra: float, dec: float) -> bool: ...

    @abstractmethod
    def goto(self, ra: float, dec: float) -> bool: ...

    def controlled_meridian_flip(
        self,
        *,
        timeout_s: float = 120.0,
        poll_s: float = 1.0,
        ra_tolerance_h: float = 0.02,
        dec_tolerance_deg: float = 0.25,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> dict[str, object]:
        """Flip to the opposite pier side and reacquire the current target."""
        raise NotImplementedError("controlled meridian flip is not supported by this mount adapter")

    @abstractmethod
    def is_slewing(self) -> bool: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def park(self) -> bool: ...

    @abstractmethod
    def disable_tracking(self) -> bool: ...

    @abstractmethod
    def guide(self, direction: str, duration_ms: int) -> bool:
        """Send a fixed-duration guide pulse.

        direction: 'n' | 's' | 'e' | 'w'
        duration_ms: pulse length in milliseconds (1–9999)
        """
        ...

    @abstractmethod
    def start_alignment(self, num_stars: int) -> bool:
        """Initialise n-star alignment sequence (num_stars: 1–9)."""
        ...

    @abstractmethod
    def accept_alignment_star(self) -> bool:
        """Record the current pointing direction as an alignment star."""
        ...

    @abstractmethod
    def save_alignment(self) -> bool:
        """Write the computed pointing model to EEPROM."""
        ...

    def get_park_position(self) -> MountPosition | None:
        """Return the stored park position, or None if the adapter doesn't support it."""
        return None

    def get_stored_park_position(self) -> object | None:
        """Return the adapter's provenance-bearing PARK record, if available."""
        return None

    def set_park_position_from_current(
        self,
        *,
        confirmed_safe: bool,
        allow_at_home: bool = False,
    ) -> object:
        raise NotImplementedError("setting PARK from current position is not supported")

    def move_ra_timed(
        self,
        direction: Literal["east", "west", "e", "w"],
        duration_ms: int,
        *,
        mode: Literal["guide", "center"] = "center",
    ) -> object:
        raise NotImplementedError("bounded RA motion is not supported")

    def move_dec_timed(
        self,
        direction: Literal["north", "south", "n", "s"],
        duration_ms: int,
        *,
        mode: Literal["guide", "center"] = "center",
    ) -> object:
        raise NotImplementedError("bounded DEC motion is not supported")

    def move_ra(
        self,
        offset_arcsec: float,
        *,
        mode: Literal["guide", "center"] = "center",
    ) -> object:
        raise NotImplementedError("calibrated RA motion is not supported")

    def move_dec(
        self,
        offset_arcsec: float,
        *,
        mode: Literal["guide", "center"] = "center",
    ) -> object:
        raise NotImplementedError("calibrated DEC motion is not supported")

    @abstractmethod
    def disconnect(self) -> None: ...
