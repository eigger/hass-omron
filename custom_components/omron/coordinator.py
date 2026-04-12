"""Bluetooth passive-update coordinator and data processor for Omron."""

from collections.abc import Callable, Coroutine
from logging import Logger
from typing import Any, Generic, TypeVar

from .omron_ble import OmronBluetoothDeviceData, SensorUpdate

from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataProcessor,
    PassiveBluetoothProcessorCoordinator,
)
from homeassistant.core import HomeAssistant

from .types import OmronConfigEntry

_T = TypeVar("_T")


class OmronBluetoothProcessorCoordinator(
    PassiveBluetoothProcessorCoordinator[SensorUpdate]
):
    """Coordinates passive BLE advertisements and forwards them to Omron device state."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: Logger,
        address: str,
        mode: BluetoothScanningMode,
        update_method: Callable[[BluetoothServiceInfoBleak], SensorUpdate],
        device_data: OmronBluetoothDeviceData,
        entry: OmronConfigEntry,
        connectable: bool = True,
    ) -> None:
        """Initialize the BLE advertisement coordinator for this device."""
        super().__init__(hass, logger, address, mode, update_method, connectable)
        self.device_data = device_data
        self.entry = entry


class OmronBluetoothDataProcessor(
    Generic[_T],
    PassiveBluetoothDataProcessor[_T, SensorUpdate],
):
    """Bridges Home Assistant passive BLE updates to Omron sensor payloads."""

    coordinator: OmronBluetoothProcessorCoordinator
