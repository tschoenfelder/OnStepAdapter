from __future__ import annotations

import threading
import time

from smart_telescope.adapters.onstep.serial_bus import OnStepSerialBus


class _BlockingSerial:
    def __init__(self) -> None:
        self.is_open = True
        self.timeout = 1.0
        self.writes: list[bytes] = []
        self.first_read_started = threading.Event()
        self.release_first_read = threading.Event()
        self._read_count = 0

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def read_until(self, terminator: bytes, size: int) -> bytes:
        self._read_count += 1
        if self._read_count == 1:
            self.first_read_started.set()
            self.release_first_read.wait(timeout=2.0)
        return b"1#"

    def reset_input_buffer(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False


def test_mount_and_focuser_commands_are_serialized_on_one_bus() -> None:
    serial = _BlockingSerial()
    bus = OnStepSerialBus()
    bus._serial = serial
    results: list[str] = []

    first = threading.Thread(target=lambda: results.append(bus.send(":GU#")))
    second = threading.Thread(target=lambda: results.append(bus.send(":FG#")))
    first.start()
    assert serial.first_read_started.wait(timeout=1.0)
    second.start()
    time.sleep(0.05)

    assert serial.writes == [b":GU#"]

    serial.release_first_read.set()
    first.join(timeout=1.0)
    second.join(timeout=1.0)

    assert serial.writes == [b":GU#", b":FG#"]
    assert results == ["1", "1"]


def test_emergency_focuser_stop_bypasses_busy_bus_lock() -> None:
    serial = _BlockingSerial()
    bus = OnStepSerialBus()
    bus._serial = serial

    command = threading.Thread(target=lambda: bus.send(":GU#"))
    command.start()
    assert serial.first_read_started.wait(timeout=1.0)

    bus.write_bypass(b":FQ#")
    assert serial.writes == [b":GU#", b":FQ#"]

    serial.release_first_read.set()
    command.join(timeout=1.0)

