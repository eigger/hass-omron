"""Setup helpers for Omron Bluetooth."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from bleak import BleakClient
from bleak_retry_connector import establish_connection

from .const import MODEL_NUMBER_UUID
from .omron_driver import GattTransport, _bleak_refresh_services
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
    try:
        client = await establish_connection(BleakClient, ble_device, ble_device.address)
        try:
            await _bleak_refresh_services(client)
            char_model = client.services.get_characteristic(MODEL_NUMBER_UUID)
            if char_model:
                model_bytes = await client.read_gatt_char(char_model)
                if model_bytes:
                    model_num = model_bytes.decode("utf-8").strip(" \x00")
                    _LOGGER.debug("Fetched Model Number during setup: %s", model_num)
                    return client, model_num
        except Exception as exc:
            _LOGGER.debug("Error reading Model Number: %s", exc)
            await client.disconnect()
            return None, None
    except Exception as exc:
        _LOGGER.debug("Could not connect to read Model Number: %s", exc)
        return None, None

    return client, None


async def async_pair_and_sync_device(
    client: BleakClient,
    ble_device: BLEDevice,
    model: str,
    config: DeviceConfig,
) -> None:
    """Perform pairing and initial time sync."""
    await _bleak_refresh_services(client)

    parent_uuid = config.parent_service_uuid
    service_found = False
    for attempt in range(5):
        if parent_uuid in [s.uuid for s in client.services]:
            service_found = True
            break
        if attempt < 4:
            await _bleak_refresh_services(client)
            await asyncio.sleep(0.35)

    if not service_found:
        # Fallback to standard standard BP service if parent not found yet
        _LOGGER.debug(
            "Parent service %s not found on %s, continuing anyway",
            parent_uuid,
            ble_device.address,
        )

    transport = GattTransport(client, config)
    await transport.pair()
    await async_sync_device_time(client, model, config, transport)
    _LOGGER.debug("Successfully paired and synced with %s (%s)", model, ble_device.address)
