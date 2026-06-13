"""OnStep serial bus — thread-safe serial I/O shared by mount and focuser."""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import serial

_log = logging.getLogger(__name__)


class OnStepSerialBus:
    """Thread-safe wrapper around the single serial connection used by OnStep.

    OnStep uses one serial port for all commands — both mount and focuser.
    This class owns the connection and serialises all I/O behind a lock so
    that the focuser never needs to reach into the mount's private members.

    Emergency-stop callers use write_bypass() to write without acquiring the
    lock so they can interrupt an in-progress command.
    """

    def __init__(self) -> None:
        self._serial: serial.Serial | None = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        serial_handle = self._serial
        return bool(serial_handle is not None and getattr(serial_handle, "is_open", True))

    def close(self) -> None:
        """Close the shared serial connection once; repeated calls are safe."""
        with self._lock:
            serial_handle = self._serial
            self._serial = None
            if serial_handle is not None:
                with contextlib.suppress(Exception):
                    serial_handle.close()

    def raw_send(self, cmd: str) -> bytes:
        """Send *cmd* and return the raw reply bytes.

        OnStep/LX200 replies are normally terminated by ``#`` rather than a
        newline.  Reading with readline() can therefore hold the shared serial
        lock until timeout even when the full reply already arrived.
        """
        if self._serial is None:
            return b""
        with self._lock:
            try:
                self._serial.write(cmd.encode())
                if hasattr(self._serial, "read_until"):
                    return bytes(self._serial.read_until(b"#", 128))
                return bytes(self._serial.read(128))
            except Exception:
                self._serial = None
                raise

    def send(self, cmd: str) -> str:
        """Send *cmd* and return the decoded, stripped reply string."""
        return self.raw_send(cmd).decode(errors="replace").rstrip("#\r\n")

    def write_no_reply(self, cmd: str, timeout: float = 0.5) -> None:
        """Send *cmd* without waiting for a reply.

        Use this for LX200 commands that are documented or observed as
        no-reply commands.  It still takes the normal serial lock so it cannot
        interleave with mount/focuser traffic.  Emergency stop remains the only
        lock-bypassing path.
        """
        if self._serial is None:
            return
        if not self._lock.acquire(timeout=timeout):
            raise TimeoutError(f"serial bus busy while sending {cmd}")
        try:
            with contextlib.suppress(Exception):
                self._serial.reset_input_buffer()
            self._serial.write(cmd.encode())
        except Exception:
            self._serial = None
            raise
        finally:
            self._lock.release()

    def send_fixed(self, cmd: str, size: int = 1, timeout: float = 0.5) -> str:
        """Send *cmd* and read a short fixed-size reply.

        Some OnStep commands, including park/unpark, reply with a single
        character and no trailing newline. readline() can wait for the full
        serial timeout in that case, so use a bounded fixed read.
        """
        if self._serial is None:
            return ""
        if not self._lock.acquire(timeout=timeout):
            raise TimeoutError(f"serial bus busy while sending {cmd}")
        try:
            old_timeout = getattr(self._serial, "timeout", None)
            with contextlib.suppress(Exception):
                self._serial.timeout = timeout
                self._serial.reset_input_buffer()
            self._serial.write(cmd.encode())
            reply = bytes(self._serial.read(size))
            with contextlib.suppress(Exception):
                self._serial.timeout = old_timeout
            return reply.decode(errors="replace").rstrip("#\r\n")
        except Exception:
            self._serial = None
            raise
        finally:
            self._lock.release()

    def write_bypass(self, data: bytes) -> None:
        """Write *data* without acquiring the lock.

        Intended only for emergency-stop commands (:Q#, :FQ#) that must be
        delivered immediately even when another command is in progress.
        """
        s = self._serial
        if s is not None:
            with contextlib.suppress(Exception):
                s.write(data)
