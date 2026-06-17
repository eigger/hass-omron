"""Setup helpers for Omron Bluetooth."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bleak import BleakClient

from .const import DEFAULT_DEVICE_MODEL
from .devices import get_device_config
from .omron_driver import (
    OmronDeviceSession,
    _bleak_refresh_services,
)
from .setup_time_sync import async_sync_device_time, build_cts_payload

if TYPE_CHECKING:
    from homeassistant.components.bluetooth import BLEDevice
    from .devices import DeviceConfig

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "async_fetch_device_model_number",
    "async_pair_and_sync_device",
    "async_sync_device_time",
    "build_cts_payload",
]


async def async_fetch_device_model_number(
    ble_device: BLEDevice,
) -> tuple[BleakClient | None, str | None]:
    """Connect to the device and read the model number."""
    # Model is unknown here; a placeholder profile is enough because
    # read_model_number() only uses the standard DIS characteristic.
    session = OmronDeviceSession(ble_device, get_device_config(DEFAULT_DEVICE_MODEL))
    try:
        await session.connect()
    except Exception as exc:
        _LOGGER.debug("Could not connect to read Model Number: %s", exc)
        return None, None

    try:
        model_num = await session.read_model_number()
        if model_num:
            _LOGGER.debug("Fetched Model Number during setup: %s", model_num)
        return session.release_client(), model_num
    except Exception as exc:
        _LOGGER.debug("Error reading Model Number: %s", exc)
        await session.aclose()
        return None, None


async def async_pair_and_sync_device(
    client: BleakClient,
    ble_device: BLEDevice,
    model: str,
    config: DeviceConfig,
) -> None:
    """Perform pairing and initial time sync."""
    await _bleak_refresh_services(client)

    parent_uuid = config.parent_service_uuid
    session = OmronDeviceSession.adopt(client, config)
    if not await session.verify_parent_service():
        # Fallback to standard standard BP service if parent not found yet
        _LOGGER.debug(
            "Parent service %s not found on %s, continuing anyway",
            parent_uuid,
            ble_device.address,
        )

    await session.pair()
    await async_sync_device_time(client, model, config, session)
    _LOGGER.debug("Successfully paired and synced with %s (%s)", model, ble_device.address)
