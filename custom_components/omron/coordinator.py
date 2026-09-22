"""Bluetooth advertisement coordinator for Omron."""

from __future__ import annotations

from collections.abc import Callable
from logging import Logger
from typing import TYPE_CHECKING, TypeVar

from sensor_state_data import SensorUpdate

from .omron_ble import OmronBluetoothDeviceData

from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataProcessor,
    PassiveBluetoothProcessorCoordinator,
)
from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

_T = TypeVar("_T")


class OmronBluetoothProcessorCoordinator(
    PassiveBluetoothProcessorCoordinator[SensorUpdate]
):
    """Coordinates passive BLE advertisements and forwards them to Omron device state."""

    poll_coordinator: DataUpdateCoordinator[SensorUpdate] | None

    def __init__(
        self,
        hass: HomeAssistant,
        logger: Logger,
        address: str,
        mode: BluetoothScanningMode,
        update_method: Callable[[BluetoothServiceInfoBleak], SensorUpdate],
        device_data: OmronBluetoothDeviceData,
        connectable: bool = True,
    ) -> None:
        """Initialize the BLE advertisement coordinator for this device."""
        super().__init__(hass, logger, address, mode, update_method, connectable)
        self.device_data = device_data
        self.poll_coordinator = None


class OmronPassiveBluetoothDataProcessor(
    PassiveBluetoothDataProcessor[_T, SensorUpdate]
):
    """Dispatches advertisement SensorUpdates to PassiveBluetooth entities.

    Same role as ``BleEslPassiveBluetoothDataProcessor``: poll-backed
    measurements stay on ``DataUpdateCoordinator``; advertisement flags
    (Data Pending, Pairing Mode, …) and RSSI update here immediately.
    """

    coordinator: OmronBluetoothProcessorCoordinator
