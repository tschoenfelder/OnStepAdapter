from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from onstep_adapter import OnStepClient
from smart_telescope.adapters.onstep.serial_bus import OnStepSerialBus


def _bus() -> MagicMock:
    bus = MagicMock(spec=OnStepSerialBus)
    type(bus).is_open = property(lambda self: True)
    return bus


def test_client_exposes_mount_and_focuser_on_same_bus() -> None:
    bus = _bus()
    client = OnStepClient("/dev/test", serial_bus=bus)

    assert client.mount.serial_bus is bus
    assert client.focuser._bus is bus


def test_connect_returns_structured_result() -> None:
    client = OnStepClient("/dev/test", serial_bus=_bus())
    with (
        patch.object(client.mount, "connect", return_value=True),
        patch.object(client.focuser, "connect", return_value=True),
        patch.object(
            type(client.focuser),
            "is_available",
            new_callable=PropertyMock,
            return_value=True,
        ),
    ):
        result = client.connect()

    assert result.connected is True
    assert result.mount_connected is True
    assert result.focuser_available is True
    assert result.port == "/dev/test"


def test_close_is_idempotent() -> None:
    bus = _bus()
    client = OnStepClient("/dev/test", serial_bus=bus)

    client.close()
    client.close()

    bus.close.assert_called_once()


def test_context_manager_closes_after_use() -> None:
    bus = _bus()
    client = OnStepClient("/dev/test", serial_bus=bus)
    with (
        patch.object(client, "connect", return_value=MagicMock(connected=True)),
        client as entered,
    ):
        assert entered is client

    bus.close.assert_called_once()


def test_context_manager_raises_when_mount_connection_fails() -> None:
    client = OnStepClient("/dev/test", serial_bus=_bus())
    with patch.object(client, "connect", return_value=MagicMock(connected=False)):
        with pytest.raises(ConnectionError):
            with client:
                pass
