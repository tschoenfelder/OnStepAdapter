"""Virtual OnStepX German equatorial mount driven in real time.

The pose is stored in the Earth-fixed mount frame (hour angle, declination,
pier side). Reported RA is ``LST - HA``. With tracking OFF a stationary pose
therefore stays fixed in ALT/AZ while its RA advances with sidereal time;
with tracking ON the hour angle advances and RA stays fixed, as observed on
the real mount.

All methods are called with the owning server's lock held.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math

from onstep_adapter.indi_gu import local_sidereal_hours

SIDEREAL_RATE = 1.00273790935  # sidereal seconds per solar second
SIDEREAL_DEG_PER_S = 15.0 / 3600.0 * SIDEREAL_RATE
# Observed TELESCOPE_SLEW_RATE elements: 0 = 0.25x ... 6 = 20x sidereal.
RATE_MULTIPLIERS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 20.0, 48.0, 240.0, 600.0)
GUIDE_MULTIPLIER = 0.5


@dataclass(frozen=True)
class Pose:
    ha_h: float
    dec_deg: float
    pier_side: str = "west"


@dataclass(frozen=True)
class SimConfig:
    """Behavior of the virtual mount; defaults follow the measured rig."""

    observer_lat: float = 50.336
    observer_lon: float = 8.533
    # HOME is the fixed pole pose: DEC +90, RA = LST + 6 h (HA = -6 h).
    home_pose: Pose = Pose(ha_h=-6.0, dec_deg=90.0, pier_side="west")
    # The single stored PARK pose; it can never be changed through INDI.
    park_pose: Pose = Pose(ha_h=0.0, dec_deg=52.0, pier_side="west")
    start_pose: Pose | None = None  # None: start at PARK
    start_parked: bool = True
    slew_deg_per_s: float = 4.0
    speed: float = 1.0  # scales slew speed and settle times, not guide pulses
    abort_settle_s: float = 1.45
    auto_track_on_unpark: bool = True
    auto_track_on_goto_arrival: bool = True
    arrival_tolerance_deg: float = 1e-4
    guard_minutes_east: float = 10.0
    guard_minutes_west: float = 10.0
    focuser_max: int = 100000
    focuser_steps_per_s: float = 2000.0
    status_period_s: float = 0.25
    tick_s: float = 0.02


@dataclass(frozen=True)
class MountSnapshot:
    """Consistent view of the virtual mount for test assertions."""

    utc: datetime
    lst_h: float
    ha_h: float
    dec_deg: float
    ra_h: float
    alt_deg: float
    az_deg: float
    pier_side: str
    parked: bool
    at_home: bool
    tracking: bool
    tracking_on_seconds: float
    motion: str  # IDLE, SLEWING, STOPPING, MANUAL
    target: str | None
    guiding: bool
    slew_rate: int
    fault: str | None
    raw_gu: str
    home_pose: Pose
    park_pose: Pose


def wrap_hours(value: float) -> float:
    """Wrap an hour angle into [-12, 12)."""
    return (value + 12.0) % 24.0 - 12.0


def altaz(ha_h: float, dec_deg: float, lat_deg: float) -> tuple[float, float]:
    ha = math.radians(ha_h * 15.0)
    dec = math.radians(dec_deg)
    lat = math.radians(lat_deg)
    sin_alt = math.sin(dec) * math.sin(lat) + math.cos(dec) * math.cos(lat) * math.cos(ha)
    alt = math.asin(max(-1.0, min(1.0, sin_alt)))
    y = -math.cos(dec) * math.sin(ha)
    x = math.sin(dec) * math.cos(lat) - math.cos(dec) * math.sin(lat) * math.cos(ha)
    az = math.degrees(math.atan2(y, x)) % 360.0
    return math.degrees(alt), az


@dataclass
class VirtualMount:
    config: SimConfig
    ha_h: float = 0.0
    dec_deg: float = 0.0
    pier_side: str = "west"
    parked: bool = True
    at_home: bool = False
    tracking: bool = False
    tracking_since: float | None = None
    last_tracking_on_s: float = 0.0
    motion: str = "IDLE"
    target_kind: str | None = None  # radec | home | park
    target_ra_h: float = 0.0
    target_dec_deg: float = 0.0
    stop_at: float = 0.0
    manual_ns: int = 0  # +1 north, -1 south
    manual_we: int = 0  # +1 west (HA up), -1 east
    slew_rate: int = 6
    guiding_until: float = 0.0
    guide_ha_deg_per_s: float = 0.0
    guide_dec_deg_per_s: float = 0.0
    fault: str | None = None
    events: list[str] = field(default_factory=list)

    @classmethod
    def create(cls, config: SimConfig) -> VirtualMount:
        pose = config.start_pose or config.park_pose
        mount = cls(config=config, ha_h=pose.ha_h, dec_deg=pose.dec_deg,
                    pier_side=pose.pier_side, parked=config.start_parked)
        mount.at_home = (pose == config.home_pose) and not config.start_parked
        return mount

    # ---- time -----------------------------------------------------------
    @staticmethod
    def utc_now() -> datetime:
        return datetime.now(timezone.utc)

    def lst_h(self) -> float:
        return local_sidereal_hours(self.config.observer_lon, self.utc_now())

    def ra_h(self) -> float:
        return (self.lst_h() - self.ha_h) % 24.0

    # ---- derived state ----------------------------------------------------
    @property
    def slewing(self) -> bool:
        return self.motion != "IDLE"

    def guiding(self, now: float) -> bool:
        return now < self.guiding_until

    def raw_gu(self, now: float) -> str:
        flags = ""
        if not self.tracking:
            flags += "n"
        if not self.slewing:
            flags += "N"
        flags += "P" if self.parked else "p"
        if self.at_home:
            flags += "H"
        if self.guiding(now):
            flags += "G"
        if self.fault == "LIMIT":
            flags += "l"
        flags += "E"
        flags += "T" if self.pier_side == "east" else "W"
        return flags + "060"

    def snapshot(self, now: float) -> MountSnapshot:
        alt, az = altaz(self.ha_h, self.dec_deg, self.config.observer_lat)
        tracking_on = self.last_tracking_on_s
        if self.tracking_since is not None:
            tracking_on = now - self.tracking_since
        return MountSnapshot(
            utc=self.utc_now(), lst_h=self.lst_h(), ha_h=self.ha_h,
            dec_deg=self.dec_deg, ra_h=self.ra_h(), alt_deg=alt, az_deg=az,
            pier_side=self.pier_side, parked=self.parked, at_home=self.at_home,
            tracking=self.tracking, tracking_on_seconds=tracking_on,
            motion=self.motion, target=self.target_kind,
            guiding=self.guiding(now), slew_rate=self.slew_rate, fault=self.fault,
            raw_gu=self.raw_gu(now), home_pose=self.config.home_pose,
            park_pose=self.config.park_pose,
        )

    # ---- commands (return None on success, else a refusal reason) ---------
    def set_tracking(self, on: bool, now: float) -> str | None:
        if on and (self.parked or self.fault):
            return "tracking refused while parked or faulted"
        if on and not self.tracking:
            self.tracking = True
            self.tracking_since = now
            self.at_home = False
        elif not on and self.tracking:
            self.tracking = False
            if self.tracking_since is not None:
                self.last_tracking_on_s = now - self.tracking_since
            self.tracking_since = None
        return None

    def unpark(self, now: float) -> str | None:
        if self.fault:
            return f"fault {self.fault}"
        if not self.parked:
            return None
        self.parked = False
        self.events.append("unpark")
        if self.config.auto_track_on_unpark:
            self.set_tracking(True, now)
        return None

    def start_goto(self, kind: str, now: float, ra_h: float = 0.0, dec_deg: float = 0.0) -> str | None:
        if self.fault:
            return f"fault {self.fault}"
        if kind != "park" and self.parked:
            return "mount is parked"
        if kind == "park" and self.parked:
            return None
        if kind == "radec" and not -90.0 <= dec_deg <= 90.0:
            return "target DEC out of range"
        self.manual_ns = self.manual_we = 0
        self.target_kind = kind
        self.target_ra_h, self.target_dec_deg = ra_h % 24.0, dec_deg
        self.motion = "SLEWING"
        self.at_home = False
        self.set_tracking(False, now)
        self.events.append(f"goto:{kind}")
        return None

    def start_manual(self, axis: str, direction: int, now: float) -> str | None:
        if self.fault or self.parked:
            return "manual motion refused while parked or faulted"
        if axis == "ns":
            self.manual_ns = direction
        else:
            self.manual_we = direction
        if self.manual_ns or self.manual_we:
            if self.motion in {"IDLE", "MANUAL"}:
                self.motion = "MANUAL"
                self.at_home = False
        elif self.motion == "MANUAL":
            self.motion = "IDLE"
        return None

    def guide(self, axis: str, direction: int, ms: float, now: float) -> str | None:
        if self.fault or self.parked:
            return "guide refused while parked or faulted"
        rate = GUIDE_MULTIPLIER * SIDEREAL_DEG_PER_S * direction
        self.guide_ha_deg_per_s = rate if axis == "we" else 0.0
        self.guide_dec_deg_per_s = rate if axis == "ns" else 0.0
        self.guiding_until = now + ms / 1000.0
        return None

    def abort(self, now: float) -> None:
        self.manual_ns = self.manual_we = 0
        self.guiding_until = 0.0
        if self.motion != "IDLE":
            self.motion = "STOPPING"
            self.target_kind = None
            self.stop_at = now + self.config.abort_settle_s / self.config.speed
        self.events.append("abort")

    def set_fault(self, fault: str | None, now: float) -> None:
        self.fault = fault
        if fault:
            self.abort(now)
            self.set_tracking(False, now)

    # ---- motion -------------------------------------------------------------
    def tick(self, dt: float, now: float) -> bool:
        """Advance the pose by dt seconds; return True when a state edge occurred."""
        edge = False
        if self.tracking:
            self.ha_h = wrap_hours(self.ha_h + dt * SIDEREAL_RATE / 3600.0)
        if self.guiding(now):
            self.ha_h = wrap_hours(self.ha_h + self.guide_ha_deg_per_s * dt / 15.0)
            self.dec_deg = max(-90.0, min(90.0, self.dec_deg + self.guide_dec_deg_per_s * dt))
        if self.motion == "STOPPING" and now >= self.stop_at:
            self.motion = "IDLE"
            edge = True
        elif self.motion == "MANUAL":
            step = RATE_MULTIPLIERS[self.slew_rate] * SIDEREAL_DEG_PER_S * dt * self.config.speed
            self.dec_deg = max(-90.0, min(90.0, self.dec_deg + self.manual_ns * step))
            self.ha_h = wrap_hours(self.ha_h + self.manual_we * step / 15.0)
        elif self.motion == "SLEWING":
            edge = self._step_goto(dt, now)
        return edge

    def _goal(self) -> tuple[float, float, str]:
        if self.target_kind == "home":
            pose = self.config.home_pose
            return pose.ha_h, pose.dec_deg, pose.pier_side
        if self.target_kind == "park":
            pose = self.config.park_pose
            return pose.ha_h, pose.dec_deg, pose.pier_side
        # A sky target moves with LST; the mount does not flip piers.
        return wrap_hours(self.lst_h() - self.target_ra_h), self.target_dec_deg, self.pier_side

    def _step_goto(self, dt: float, now: float) -> bool:
        goal_ha, goal_dec, goal_pier = self._goal()
        step = self.config.slew_deg_per_s * self.config.speed * dt
        ha_err = wrap_hours(goal_ha - self.ha_h) * 15.0
        dec_err = goal_dec - self.dec_deg
        # Each axis moves independently, so they finish at different times.
        self.ha_h = wrap_hours(self.ha_h + math.copysign(min(step, abs(ha_err)), ha_err) / 15.0)
        self.dec_deg += math.copysign(min(step, abs(dec_err)), dec_err)
        tolerance = self.config.arrival_tolerance_deg
        if abs(wrap_hours(goal_ha - self.ha_h) * 15.0) > tolerance or abs(goal_dec - self.dec_deg) > tolerance:
            return False
        kind = self.target_kind
        self.ha_h, self.dec_deg, self.pier_side = goal_ha, goal_dec, goal_pier
        self.motion = "IDLE"
        self.target_kind = None
        self.events.append(f"arrived:{kind}")
        if kind == "park":
            self.parked = True
        elif kind == "home":
            self.at_home = True
        elif self.config.auto_track_on_goto_arrival:
            self.set_tracking(True, now)
        return True
