"""Setup helpers for Omron Bluetooth."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import TYPE_CHECKING

from bleak import BleakClient
from bleak_retry_connector import establish_connection

from .const import (
    CTS_CHARACTERISTIC_UUID,
    LOCAL_TIME_INFO_UUID,
    MODEL_NUMBER_UUID,
)
from .devices import get_device_config
from .omron_driver import GattTransport, OmronDeviceDriver, _bleak_refresh_services

if TYPE_CHECKING:
    from homeassistant.components.bluetooth import BLEDevice
    from .devices import DeviceConfig

_LOGGER = logging.getLogger(__name__)


def build_cts_payload(now: dt.datetime) -> bytearray:
    """Build Bluetooth CTS payload (10 bytes) from timezone-aware datetime."""
    payload = bytearray()
    payload += int(now.year).to_bytes(2, "little")
    payload += bytes(
        [
            now.month,
            now.day,
            now.hour,
            now.minute,
            now.second,
            now.isoweekday(),  # Monday=1 ... Sunday=7 (CTS format)
            0x00,  # Fractions256
            0x00,  # Adjust reason: 0x00 (Unknown)
        ]
    )
    return payload


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


async def async_sync_device_time(
    client: BleakClient,
    model: str,
    config: DeviceConfig | None = None,
    transport: GattTransport | None = None,
) -> bool:
    """Sync current local time via CTS or EEPROM fallback."""
    if not client.is_connected:
        _LOGGER.debug(
            "Skipping time sync for %s: client is not connected",
            model,
        )
        return False

    try:
        await _bleak_refresh_services(client)
        services = client.services
        if services is None:
            _LOGGER.debug(
                "Skipping time sync for %s: GATT services unavailable",
                model,
            )
            return False
        
        char_cts = services.get_characteristic(CTS_CHARACTERISTIC_UUID)
    except Exception as exc:
        _LOGGER.debug(
            "Skipping time sync for %s: service discovery unavailable (%r)",
            model,
            exc,
        )
        return False

    # Try CTS first
    if char_cts is not None:
        now = dt.datetime.now().astimezone()
        payload = build_cts_payload(now)
        cts_notify_ready = asyncio.Event()
        cts_notify_started = False
        cts_notify_payload: list[bytes | None] = [None]

        def _cts_callback(_: object, data: bytearray) -> None:
            cts_notify_payload[0] = bytes(data)
            cts_notify_ready.set()

        try:
            # Align with OGSC flow: enable CTS notify/get before set.
            await client.start_notify(CTS_CHARACTERISTIC_UUID, _cts_callback)
            await asyncio.sleep(0.5)
            cts_notify_started = True

            try:
                cts_snapshot = await client.read_gatt_char(CTS_CHARACTERISTIC_UUID)
                if cts_snapshot:
                    _LOGGER.debug(
                        "CTS current-time snapshot for %s: %s",
                        model,
                        bytes(cts_snapshot).hex(),
                    )
            except Exception as exc:
                _LOGGER.debug(
                    "CTS snapshot read failed for %s (continuing): %s",
                    model,
                    exc,
                )

            try:
                await asyncio.wait_for(cts_notify_ready.wait(), timeout=1.0)
                if cts_notify_payload[0] is not None:
                    _LOGGER.debug(
                        "CTS notify received for %s before sync: %s",
                        model,
                        cts_notify_payload[0].hex(),
                    )
            except asyncio.TimeoutError:
                _LOGGER.debug(
                    "CTS notify not received before sync for %s (continuing)",
                    model,
                )

            await client.write_gatt_char(CTS_CHARACTERISTIC_UUID, payload, response=True)
            _LOGGER.info(
                "Synced current time via CTS for %s: %s",
                model,
                now.isoformat(timespec="seconds"),
            )
            
            # Try Local Time Information (0x2A0F)
            char_lti = services.get_characteristic(LOCAL_TIME_INFO_UUID)
            if char_lti:
                try:
                    utcoffset = now.utcoffset()
                    if utcoffset is not None:
                        offset_mins = int(utcoffset.total_seconds() // 60)
                        tz_offset_15m = int(offset_mins // 15)
                        tz_byte = tz_offset_15m & 0xFF
                        
                        dst_byte = 0x00
                        if now.dst() and now.dst().total_seconds() > 0:
                            dst_byte = 0x04
                        
                        lti_payload = bytes([tz_byte, dst_byte])
                        await client.write_gatt_char(LOCAL_TIME_INFO_UUID, lti_payload, response=True)
                        _LOGGER.debug(
                            "Local Time Info sync success for %s (tz_offset_15m=%d, dst=%d)",
                            model, tz_offset_15m, dst_byte
                        )
                except Exception as exc:
                    _LOGGER.debug("Local Time Info sync failed for %s: %s", model, exc)
            
            return True
        except Exception as exc:
            _LOGGER.warning("Failed to sync time via CTS for %s: %s", model, exc)
        finally:
            if cts_notify_started:
                try:
                    await client.stop_notify(CTS_CHARACTERISTIC_UUID)
                except Exception as exc:
                    _LOGGER.debug("CTS stop_notify failed for %s: %s", model, exc)

    # CTS not available — try EEPROM-based time sync for legacy devices
    if config is None:
        config = get_device_config(model)
    
    if config.supports_eeprom_time_sync:
        _LOGGER.debug(
            "CTS not found for %s, trying EEPROM time sync",
            model,
        )
        if transport is None:
            transport = GattTransport(client, config)
        try:
            driver = OmronDeviceDriver(config)
            synced = await driver.sync_eeprom_time(transport)
            if synced:
                return True
            _LOGGER.warning("EEPROM time sync returned False for %s", model)
        except Exception as exc:
            _LOGGER.warning("EEPROM time sync failed for %s: %s", model, exc)
    else:
        _LOGGER.debug(
            "Skipping time sync for %s: "
            "CTS characteristic not found and EEPROM time sync not supported",
            model,
        )

    return False


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
            await asyncio.sleep(0.25)
            
    if not service_found:
        # Fallback to standard standard BP service if parent not found yet
        _LOGGER.debug("Parent service %s not found on %s, continuing anyway", parent_uuid, ble_device.address)

    transport = GattTransport(client, config)
    await transport.pair()
    await async_sync_device_time(client, model, config, transport)
    _LOGGER.info("Successfully paired and synced with %s (%s)", model, ble_device.address)
