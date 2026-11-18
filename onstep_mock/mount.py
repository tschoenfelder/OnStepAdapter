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
from datetime import datetime, timedelta, timezone
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
    #: False: the INDI device starts disconnected, as after an indiserver restart.
    start_device_connected: bool = True
    auto_track_on_unpark: bool = True
    auto_track_on_goto_arrival: bool = True
    arrival_tolerance_deg: float = 1e-4
    guard_minutes_east: float = 10.0
    guard_minutes_west: float = 10.0
    focuser_max: int = 100000
    focuser_steps_per_s: float = 2000.0
    status_period_s: float = 0.25
    tick_s: float = 0.02
    #: Manual-motion acceleration and braking (deg/s^2 at speed 1); 0 = instant (the default).
    #: Field incident 56b54f01: the real rig needed ~1.2 deg/s^2 to reach ~1.8 deg/s at the
    #: fastest rate, so a 0.5 s pass covered only 0.14 deg.
    manual_accel_deg_per_s2: float = 0.0
    #: Braking after a motion switch goes off (deg/s^2 at speed 1); 0 = the acceleration.
    manual_brake_deg_per_s2: float = 0.0
    #: Delay from a motion switch turning On / Off to the axis reacting (s at speed 1).
    manual_start_latency_s: float = 0.0
    manual_stop_latency_s: float = 0.0
    #: Cruise speed (deg/s at speed 1) per slew-rate index where it differs from
    #: ``RATE_MULTIPLIERS`` (rates 8/9 depend on the firmware's configured maximum).
    manual_cruise_deg_per_s: tuple[tuple[int, float], ...] = ()
    #: The real lx200_OnStep driver publishes no OnStep Status while a motion switch is On.
    status_silent_while_manual: bool = False
    #: The real :GU# never flags manual motion as slewing, not even while it brakes.
    manual_motion_flags_slewing: bool = True
    #: Publish period (status and coordinates) while no motion switch is On and no goto runs;
    #: None = ``status_period_s``. The real driver polls every ~1.5 s then, every ~0.26 s while
    #: a motion switch is On.
    idle_publish_period_s: float | None = None
    #: A published position is this old (s at speed 1): the driver reads it, then publishes.
    coordinate_lag_s: float = 0.0

    @classmethod
    def rig_2eb2a5cb(cls, **overrides: object) -> SimConfig:
        """The real rig as field capture 2eb2a5cb (2026-11-18) recorded it.

        Fitted from two DEC passes at slew rate 9 (OnStep Status, coordinates and switch
        replies of the INDI capture): the axis starts ~0.39 s after MOTION_NORTH On, speeds up
        at 0.54 deg/s^2 to 3.27 deg/s, reacts to Off at once and brakes at ~0.565 deg/s^2
        (pass 3 coasted from 67.1 to 77.7 deg); a published reading lagged ~0.1 s. No OnStep Status while the switch was On (7 s
        gaps), :GU# showed "not slewing" throughout, coordinates came every 0.26 s while the
        switch was On and every 1.5 s otherwise. Rate 8 was not exercised: half of rate 9, as
        OnStepX defines it.
        """
        values: dict[str, object] = dict(
            manual_accel_deg_per_s2=0.54, manual_brake_deg_per_s2=0.565,
            manual_start_latency_s=0.39, manual_stop_latency_s=0.02,
            manual_cruise_deg_per_s=((8, 1.635), (9, 3.27)),
            status_silent_while_manual=True, manual_motion_flags_slewing=False,
            status_period_s=0.26, idle_publish_period_s=1.5, coordinate_lag_s=0.1,
            auto_track_on_unpark=False,
        )
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]


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
    #: The direction last commanded per axis (`manual_ns`/`manual_we` follow it after latency).
    manual_ns_cmd: int = 0
    manual_we_cmd: int = 0
    #: How NORTH changes the published DEC after leaving the pole (None: from the pier side).
    ns_dec_sign: float | None = None
    manual_we: int = 0  # +1 west (HA up), -1 east
    #: Manual motion with acceleration: the current speed and the last direction per axis
    #: (kept while braking after the direction switched off).
    manual_ns_speed: float = 0.0  # signed: + is the NORTH / WEST jog
    manual_we_speed: float = 0.0
    #: Motion-switch changes waiting for `SimConfig.manual_*_latency_s`: (apply at, axis, dir).
    pending_manual: list[tuple[float, str, int]] = field(default_factory=list)
    manual_ns_last: int = 0
    manual_we_last: int = 0
    slew_rate: int = 6
    guiding_until: float = 0.0
    guide_ha_deg_per_s: float = 0.0
    guide_dec_deg_per_s: float = 0.0
    fault: str | None = None
    events: list[str] = field(default_factory=list)
    utc_origin: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    elapsed_s: float = 0.0

    @classmethod
    def create(cls, config: SimConfig) -> VirtualMount:
        pose = config.start_pose or config.park_pose
        mount = cls(config=config, ha_h=pose.ha_h, dec_deg=pose.dec_deg,
                    pier_side=pose.pier_side, parked=config.start_parked)
        mount.at_home = (pose == config.home_pose) and not config.start_parked
        return mount

    # ---- time -----------------------------------------------------------
    def utc_now(self) -> datetime:
        return self.utc_origin + timedelta(seconds=self.elapsed_s)

    def lst_h(self) -> float:
        return local_sidereal_hours(self.config.observer_lon, self.utc_now())

    def ra_h(self) -> float:
        return (self.lst_h() - self.ha_h) % 24.0

    def published_pose(self) -> tuple[float, float]:
        """(RA hours, DEC deg) as the driver publishes them: `SimConfig.coordinate_lag_s` old
        while a manual motion moves the axes."""
        lag = self.config.coordinate_lag_s / self.config.speed
        if not lag or self.motion != "MANUAL":
            return self.ra_h(), self.dec_deg
        ns_sign = self.ns_dec_sign
        if ns_sign is None:
            ns_sign = -1.0 if self.pier_side == "east" else 1.0
        dec = self.dec_deg - ns_sign * self.manual_ns_speed * lag
        ha = wrap_hours(self.ha_h - self.manual_we_speed * lag / 15.0)
        return (self.lst_h() - ha) % 24.0, dec

    # ---- derived state ----------------------------------------------------
    @property
    def slewing(self) -> bool:
        return self.motion != "IDLE"

    @property
    def flags_slewing(self) -> bool:
        """What :GU# reports (the real one never flags manual motion)."""
        if self.motion == "MANUAL" and not self.config.manual_motion_flags_slewing:
            return False
        return self.slewing

    @property
    def manual_switch_on(self) -> bool:
        """A motion switch is On (as the client commanded it, before any latency)."""
        return bool(self.manual_ns_cmd or self.manual_we_cmd)

    def guiding(self, now: float) -> bool:
        return now < self.guiding_until

    def raw_gu(self, now: float) -> str:
        flags = ""
        if not self.tracking:
            flags += "n"
        if not self.flags_slewing:
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
        self.ns_dec_sign = None
        if self.fault:
            return f"fault {self.fault}"
        if kind != "park" and self.parked:
            return "mount is parked"
        if kind == "park" and self.parked:
            return None
        if kind == "radec" and not -90.0 <= dec_deg <= 90.0:
            return "target DEC out of range"
        self.manual_ns = self.manual_we = 0
        self.manual_ns_cmd = self.manual_we_cmd = 0
        self.pending_manual.clear()
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
            self.manual_ns_cmd = direction
        else:
            self.manual_we_cmd = direction
        latency = (
            self.config.manual_start_latency_s if direction else self.config.manual_stop_latency_s
        ) / self.config.speed
        if latency > 0.0:
            # The controller handles the commands of an axis in order: an Off sent right
            # after an On (shorter stop latency) still lands after it.
            earlier = [at for at, pending_axis, _ in self.pending_manual if pending_axis == axis]
            self.pending_manual.append((max([now + latency, *earlier]), axis, direction))
            return None
        self._apply_manual(axis, direction)
        return None

    def _apply_manual(self, axis: str, direction: int) -> None:
        if axis == "ns":
            self.manual_ns = direction
            self.manual_ns_last = direction or self.manual_ns_last
        else:
            self.manual_we = direction
            self.manual_we_last = direction or self.manual_we_last
        if self.manual_ns or self.manual_we:
            if self.motion in {"IDLE", "MANUAL"}:
                self.motion = "MANUAL"
                self.at_home = False
        elif self.motion == "MANUAL" and not self.config.manual_accel_deg_per_s2:
            self.motion = "IDLE"

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
        self.manual_ns_cmd = self.manual_we_cmd = 0
        self.pending_manual.clear()
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
        self.elapsed_s += dt
        edge = False
        if self.pending_manual:
            due = [item for item in self.pending_manual if item[0] <= now]
            self.pending_manual = [item for item in self.pending_manual if item[0] > now]
            for _, axis, direction in sorted(due):
                if not self.parked and not self.fault:
                    # No publish edge: the real driver reports the start only at its next poll.
                    self._apply_manual(axis, direction)
        if self.tracking:
            self.ha_h = wrap_hours(self.ha_h + dt * SIDEREAL_RATE / 3600.0)
        if self.guiding(now):
            self.ha_h = wrap_hours(self.ha_h + self.guide_ha_deg_per_s * dt / 15.0)
            self.dec_deg = max(-90.0, min(90.0, self.dec_deg + self.guide_dec_deg_per_s * dt))
        if self.motion == "STOPPING" and now >= self.stop_at:
            self.motion = "IDLE"
            edge = True
        elif self.motion == "MANUAL" and self.config.manual_accel_deg_per_s2:
            edge = self._step_manual_with_acceleration(dt)
        elif self.motion == "MANUAL":
            step = RATE_MULTIPLIERS[self.slew_rate] * SIDEREAL_DEG_PER_S * dt * self.config.speed
            # OnStep's NORTH is a mechanical direction: on the real rig it lowers the published
            # DEC at pier side east and away from HOME (the pole), and raises it at pier side
            # west -- a mechanical axis 2 crossing the pole, without modelling the crossing.
            # Axis 2 alone never changes the pier side (that follows axis 1); leaving the pole,
            # either direction lowers DEC, and keeps doing so until the mount is placed or slews.
            if self.manual_ns and self.dec_deg >= 90.0:
                self.ns_dec_sign = -float(self.manual_ns)
            ns_sign = self.ns_dec_sign
            if ns_sign is None:
                ns_sign = -1.0 if self.pier_side == "east" else 1.0
            self.dec_deg = max(-90.0, min(90.0, self.dec_deg + ns_sign * self.manual_ns * step))
            self.ha_h = wrap_hours(self.ha_h + self.manual_we * step / 15.0)
        elif self.motion == "SLEWING":
            edge = self._step_goto(dt, now)
        return edge

    def _step_manual_with_acceleration(self, dt: float) -> bool:
        """Manual motion that ramps up to the selected rate and brakes after it is switched
        off (`SimConfig.manual_accel_deg_per_s2` / `manual_brake_deg_per_s2`); a reversal
        brakes to a stop before it speeds up the other way. IDLE once both axes stopped."""
        config = self.config
        cruise = dict(config.manual_cruise_deg_per_s).get(
            self.slew_rate, RATE_MULTIPLIERS[self.slew_rate] * SIDEREAL_DEG_PER_S
        ) * config.speed
        accel = config.manual_accel_deg_per_s2 * config.speed * config.speed * dt
        brake = (
            config.manual_brake_deg_per_s2 or config.manual_accel_deg_per_s2
        ) * config.speed * config.speed * dt

        def ramp(velocity: float, direction: int) -> float:
            target = direction * cruise
            if velocity * target < 0.0 or abs(target) < abs(velocity):  # braking
                if velocity > 0.0:
                    return max(velocity - brake, max(target, 0.0))
                return min(velocity + brake, min(target, 0.0))
            if target > velocity:
                return min(target, velocity + accel)
            return max(target, velocity - accel)

        self.manual_ns_speed = ramp(self.manual_ns_speed, self.manual_ns)
        self.manual_we_speed = ramp(self.manual_we_speed, self.manual_we)
        if self.manual_ns_speed and self.dec_deg >= 90.0:
            self.ns_dec_sign = -math.copysign(1.0, self.manual_ns_speed)
        ns_sign = self.ns_dec_sign
        if ns_sign is None:
            ns_sign = -1.0 if self.pier_side == "east" else 1.0
        self.dec_deg = max(-90.0, min(90.0, self.dec_deg + ns_sign * self.manual_ns_speed * dt))
        self.ha_h = wrap_hours(self.ha_h + self.manual_we_speed * dt / 15.0)
        if not (self.manual_ns or self.manual_we or self.pending_manual) and not (
            self.manual_ns_speed or self.manual_we_speed
        ):
            self.motion = "IDLE"
            return True
        return False

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
