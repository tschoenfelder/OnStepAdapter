"""
FakeOnStepSerial — stateful LX200 serial simulator for integration testing.

Mimics the pyserial Serial interface (write / read_until / read / readline / close / is_open).
Maintains a mount state machine so tests exercise real command sequences
without any hardware or mocker patching.

State transitions:
  parked  → unpark (:hR#)    → unparked
  unparked → track (:Te#)    → tracking
  any      → goto  (:MS#)    → slewing
  slewing  → settle()        → tracking   (helper for tests)
  slewing  → stop  (:Q#)     → unparked
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone

from smart_telescope.adapters.onstep.mount import (
    _format_dec,
    _format_ra,
    _lst_hours,
    _parse_dec,
    _parse_degrees,
    _parse_ra,
)

_GU_RESPONSES: dict[str, bytes] = {
    "parked":   b"P|N|0|0|0|0|0|0|0|0|0|0|0|0|0#",
    "unparked": b"n|N|0|0|0|0|0|0|0|0|0|0|0|0|0#",
    "home":     b"H|N|p|0|0|0|0|0|0|0|0|0|0|0|0#",
    "tracking": b"n|T|0|0|0|0|0|0|0|0|0|0|0|0|0#",
    "slewing":  b"n|S|0|0|0|0|0|0|0|0|0|0|0|0|0#",
    "at_limit": b"n|l|N|0|0|0|0|0|0|0|0|0|0|0|0#",
}


class FakeOnStepSerial:
    """Drop-in replacement for serial.Serial, simulating an OnStep V4 mount."""

    def __init__(
        self,
        initial_state: str = "parked",
        initial_ra: float = 0.0,
        initial_dec: float = 0.0,
        horizon_limit: float = 0.0,
        overhead_limit: float = 88.0,
        local_datetime: datetime | None = None,
        utc_offset_hours: int = 0,
        latitude: float = 50.0,
        longitude_east: float = 8.0,
        sidereal_hours: float | None = None,
        tracking_step_seconds: float = 0.0,
        firmware_west_limit_h: float | None = None,
        firmware_stops_at_limit: bool = False,
        auto_settle_status_reads: int | None = None,
        meridian_east_minutes: int = 20,
        meridian_west_minutes: int = 20,
        axis1_min_deg: int = -90,
        axis1_max_deg: int = 90,
        axis2_min_deg: int = -90,
        axis2_max_deg: int = 90,
        auto_meridian_flip: bool = False,
        flip_on_positive_ha_goto: bool = False,
        dual_pier_west_ha_stop: bool = False,
        firmware_version: str = "10.19d",
        set_park_reply: str = "1",
    ) -> None:
        self._state = initial_state
        self._ra = initial_ra
        self._dec = initial_dec
        self._target_ra = 0.0
        self._target_dec = 0.0
        self._alt = 45.0
        self._az = 180.0
        self._pier_side = "E"
        self._horizon_limit = horizon_limit
        self._overhead_limit = overhead_limit
        self._local_datetime = local_datetime or datetime.now()
        self._utc_offset_hours = int(utc_offset_hours)
        self._latitude = float(latitude)
        self._longitude_west_positive = -float(longitude_east)
        self._sidereal_hours = (
            float(sidereal_hours) % 24.0
            if sidereal_hours is not None
            else _lst_hours(8.0, datetime.now(timezone.utc))
        )
        self._tracking_step_seconds = max(0.0, tracking_step_seconds)
        self._firmware_west_limit_h = firmware_west_limit_h
        self._firmware_stops_at_limit = firmware_stops_at_limit
        self._auto_settle_status_reads = auto_settle_status_reads
        self._slew_status_reads = 0
        self._meridian_east_minutes = meridian_east_minutes
        self._meridian_west_minutes = meridian_west_minutes
        self._axis1_min_deg = axis1_min_deg
        self._axis1_max_deg = axis1_max_deg
        self._axis2_min_deg = axis2_min_deg
        self._axis2_max_deg = axis2_max_deg
        self._auto_meridian_flip = bool(auto_meridian_flip)
        self._flip_on_positive_ha_goto = bool(flip_on_positive_ha_goto)
        self._dual_pier_west_ha_stop = bool(dual_pier_west_ha_stop)
        self._firmware_version = firmware_version
        self._set_park_reply = set_park_reply
        self._selected_rate = "guide"
        self._preferred_pier_policy = "B"
        self._pending_opposite_pier_goto = False
        self._pending_forced_pier_side: str | None = None
        self._pending_target_pier_side: str | None = None
        self._last_response: bytes = b""
        self.is_open = True
        self.commands_received: list[bytes] = []

    # ── pyserial interface ─────────────────────────────────────────────────────

    def write(self, data: bytes) -> None:
        self.commands_received.append(data)
        cmd = data.decode(errors="replace")
        self._last_response = self._process(cmd)

    def readline(self) -> bytes:
        r = self._last_response
        self._last_response = b""
        return r

    def read_until(self, expected: bytes = b"#", size: int | None = None) -> bytes:
        r = self._last_response
        self._last_response = b""
        return r[:size] if size is not None else r

    def read(self, size: int = 1) -> bytes:
        r = self._last_response[:size]
        self._last_response = self._last_response[size:]
        return r

    def close(self) -> None:
        self.is_open = False

    # ── test helper ───────────────────────────────────────────────────────────

    def settle(self) -> None:
        """Simulate slew completion — advance position to target and stop slewing."""
        if self._state == "slewing":
            self._ra = self._target_ra
            self._dec = self._target_dec
            target_ha = ((self._sidereal_hours - self._target_ra + 12.0) % 24.0) - 12.0
            if (
                self._pending_opposite_pier_goto
                and self._flip_on_positive_ha_goto
                and target_ha >= 0.0
            ):
                self._pier_side = "W" if self._pier_side == "E" else "E"
            if self._pending_forced_pier_side is not None:
                self._pier_side = self._pending_forced_pier_side
            if self._pending_target_pier_side is not None:
                self._pier_side = self._pending_target_pier_side
            self._pending_opposite_pier_goto = False
            self._pending_forced_pier_side = None
            self._pending_target_pier_side = None
            self._state = "tracking"

    def advance(self, seconds: float) -> None:
        self._sidereal_hours = (self._sidereal_hours + max(0.0, seconds) / 3600.0) % 24.0
        if self._state != "tracking" or self._firmware_west_limit_h is None:
            return
        ha = ((self._sidereal_hours - self._ra + 12.0) % 24.0) - 12.0
        if self._firmware_stops_at_limit and ha >= self._firmware_west_limit_h:
            self._state = "at_limit"

    def _refresh_sidereal_from_clock(self) -> None:
        utc_dt = (
            self._local_datetime + timedelta(hours=self._utc_offset_hours)
        ).replace(tzinfo=timezone.utc)
        self._sidereal_hours = _lst_hours(-self._longitude_west_positive, utc_dt)

    # ── LX200 command dispatcher ──────────────────────────────────────────────

    def _process(self, cmd: str) -> bytes:
        if cmd == ":GU#":
            if self._state == "slewing" and self._auto_settle_status_reads is not None:
                self._slew_status_reads += 1
                if self._slew_status_reads >= self._auto_settle_status_reads:
                    self.settle()
            return _GU_RESPONSES.get(self._state, b"#")

        if cmd == ":Gh#":
            sign = "+" if self._horizon_limit >= 0 else "-"
            return f"{sign}{abs(int(round(self._horizon_limit))):02d}#".encode()

        if cmd == ":Go#":
            return f"{int(round(self._overhead_limit)):02d}#".encode()

        extended_limits = {
            ":GXE9#": self._meridian_east_minutes,
            ":GXEA#": self._meridian_west_minutes,
            ":GXEe#": self._axis1_min_deg,
            ":GXEw#": self._axis1_max_deg,
            ":GXEB#": round(self._axis1_max_deg / 15.0),
            ":GXEC#": self._axis2_min_deg,
            ":GXED#": self._axis2_max_deg,
            ":GXEG#": 1 if self._dual_pier_west_ha_stop else 0,
        }
        if cmd in extended_limits:
            return f"{extended_limits[cmd]}#".encode()

        if cmd.startswith(":SXEA,"):
            try:
                self._meridian_west_minutes = int(cmd[6:].rstrip("#"))
            except ValueError:
                return b"0"
            return b"1"

        if cmd == ":GX95#":
            return b"1#" if self._auto_meridian_flip else b"0#"

        if cmd == ":GVP#":
            return b"On-Step#"

        if cmd == ":GVN#":
            return f"{self._firmware_version}#".encode()

        if cmd == ":GVD#":
            return b"Feb 29 2024#"

        if cmd == ":GX42#":
            axis1 = self._ra * 15.0
            if self._pier_side == "W":
                axis1 += 180.0
            return f"{axis1:.6f}#".encode()

        if cmd == ":GX43#":
            axis2 = 180.0 - self._dec if self._pier_side == "W" else self._dec
            return f"{axis2:.6f}#".encode()

        if cmd == ":GC#":
            return self._local_datetime.strftime("%m/%d/%y#").encode()

        if cmd == ":GL#":
            return self._local_datetime.strftime("%H:%M:%S#").encode()

        if cmd == ":GS#":
            if self._state == "tracking" and self._tracking_step_seconds:
                self.advance(self._tracking_step_seconds)
            return (_format_ra(self._sidereal_hours) + "#").encode()

        if cmd == ":Gt#":
            return (_format_dec(self._latitude).split(":")[0] + "#").encode()

        if cmd == ":Gg#":
            sign = "+" if self._longitude_west_positive >= 0 else "-"
            value = abs(self._longitude_west_positive)
            deg = int(value)
            minutes = int(round((value - deg) * 60.0))
            return f"{sign}{deg:03d}*{minutes:02d}#".encode()

        if cmd.startswith(":SC"):
            try:
                self._local_datetime = datetime.strptime(cmd[3:].rstrip("#"), "%m/%d/%y").replace(
                    hour=self._local_datetime.hour,
                    minute=self._local_datetime.minute,
                    second=self._local_datetime.second,
                )
            except ValueError:
                return b"0"
            self._refresh_sidereal_from_clock()
            return b"1"

        if cmd.startswith(":SL"):
            try:
                new_time = datetime.strptime(cmd[3:].rstrip("#"), "%H:%M:%S").time()
                self._local_datetime = self._local_datetime.replace(
                    hour=new_time.hour,
                    minute=new_time.minute,
                    second=new_time.second,
                )
            except ValueError:
                return b"0"
            self._refresh_sidereal_from_clock()
            return b"1"

        if cmd == ":GG#":
            sign = "+" if self._utc_offset_hours >= 0 else "-"
            return f"{sign}{abs(self._utc_offset_hours):02d}:00#".encode()

        if cmd.startswith(":SG"):
            try:
                self._utc_offset_hours = int(cmd[3:].rstrip("#"))
            except ValueError:
                return b"0"
            self._refresh_sidereal_from_clock()
            return b"1"

        if cmd.startswith(":St"):
            try:
                self._latitude = _parse_degrees(cmd[3:].rstrip("#"))
            except ValueError:
                return b"0"
            return b"1"

        if cmd.startswith(":Sg"):
            try:
                self._longitude_west_positive = _parse_degrees(cmd[3:].rstrip("#"))
            except ValueError:
                return b"0"
            self._refresh_sidereal_from_clock()
            return b"1"

        if cmd.startswith(":Sh"):
            self._horizon_limit = float(cmd[3:].rstrip("#"))
            return b"1"

        if cmd.startswith(":So"):
            self._overhead_limit = float(cmd[3:].rstrip("#"))
            return b"1"

        if cmd == ":hU#":
            return b"0"

        if cmd == ":hR#":
            if self._state == "parked":
                self._state = "unparked"
            return b"1"

        if cmd == ":hP#":
            self._state = "parked"
            return b"1"

        if cmd == ":hQ#":
            return self._set_park_reply.encode()

        if cmd == ":hC#":
            self._state = "home"
            return b""

        if cmd == ":Te#":
            if self._state in ("unparked", "parked", "tracking"):
                self._state = "tracking"
                return b"1"
            return b"0"

        if cmd == ":Td#":
            if self._state in ("unparked", "tracking"):
                self._state = "unparked"
                return b"1"
            return b"0"

        if cmd.startswith(":Sr"):
            ra_str = cmd[3:].rstrip("#")
            with contextlib.suppress(ValueError, IndexError):
                self._target_ra = _parse_ra(ra_str)
            return b"1"

        if cmd.startswith(":Sd"):
            dec_str = cmd[3:].rstrip("#")
            with contextlib.suppress(ValueError, IndexError):
                self._target_dec = _parse_dec(dec_str)
            return b"1"

        if cmd == ":MS#":
            if self._preferred_pier_policy in {"E", "W"}:
                self._pending_target_pier_side = self._preferred_pier_policy
            self._state = "slewing"
            self._slew_status_reads = 0
            return b"0"

        if cmd == ":MN#":
            self._target_ra = self._ra
            self._target_dec = self._dec
            self._pending_opposite_pier_goto = True
            self._state = "slewing"
            self._slew_status_reads = 0
            return b"0"

        if cmd in {":MNe#", ":MNw#"}:
            self._pending_forced_pier_side = "E" if cmd == ":MNe#" else "W"
            self._state = "slewing"
            self._slew_status_reads = 0
            return b"0"

        if cmd == ":GX96#":
            return f"{self._preferred_pier_policy}#".encode()

        if cmd.startswith(":SX96,") and cmd.endswith("#"):
            value = cmd[6:-1].upper()
            if value in {"E", "W", "B", "A"}:
                self._preferred_pier_policy = value
                return b"1"
            return b"0"

        if cmd == ":CM#":
            self._ra = self._target_ra
            self._dec = self._target_dec
            return b"Synchronized#"

        if cmd == ":GR#":
            return (_format_ra(self._ra) + "#").encode()

        if cmd == ":GD#":
            return (_format_dec(self._dec) + "#").encode()

        if cmd == ":GA#":
            sign = "+" if self._alt >= 0 else "-"
            return f"{sign}{abs(int(round(self._alt))):02d}*00:00#".encode()

        if cmd == ":GZ#":
            return f"{int(round(self._az)) % 360:03d}*00:00#".encode()

        if cmd == ":Gm#":
            return f"{self._pier_side}#".encode()

        if cmd == ":D#":
            return b"|#" if self._state == "slewing" else b"#"

        if cmd == ":Q#":
            if self._state in ("slewing", "tracking"):
                self._state = "unparked"
            return b""

        if cmd == ":RG#":
            self._selected_rate = "guide"
            return b""

        if cmd == ":RC#":
            self._selected_rate = "center"
            return b""

        if cmd in {":Me#", ":Mw#", ":Mn#", ":Ms#"}:
            direction = cmd[2:3].lower()
            if direction == "n":
                self._dec += 0.002
            elif direction == "s":
                self._dec -= 0.002
            elif direction == "e":
                self._ra = (self._ra + 0.0006) % 24.0
            elif direction == "w":
                self._ra = (self._ra - 0.0006) % 24.0
            return b""

        if cmd in {":Qe#", ":Qw#", ":Qn#", ":Qs#"}:
            return b""

        if cmd.startswith(":Mg"):
            direction = cmd[3:4].lower()
            if direction == "n":
                self._dec += 0.001
            elif direction == "s":
                self._dec -= 0.001
            elif direction == "e":
                self._ra = (self._ra + 0.0003) % 24.0
            elif direction == "w":
                self._ra = (self._ra - 0.0003) % 24.0
            return b""

        return b""
