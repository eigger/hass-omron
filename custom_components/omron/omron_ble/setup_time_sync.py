"""GATT time synchronization (CTS / LTI / EEPROM) for Omron devices."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import TYPE_CHECKING

from bleak import BleakClient

from .const import CTS_CHARACTERISTIC_UUID, LOCAL_TIME_INFO_UUID
from .devices import get_device_config
from .omron_driver import GattTransport, OmronDeviceDriver, _bleak_refresh_services

if TYPE_CHECKING:
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


async def _sync_time_via_cts(client: BleakClient, model: str) -> bool:
    """Write Current Time Service (+ optional Local Time Information). Returns True if CTS write ran."""
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

    cts_success = False
    if char_cts is None:
        return False

    now = dt.datetime.now().astimezone()
    payload = build_cts_payload(now)
    cts_notify_ready = asyncio.Event()
    cts_notify_started = False
    cts_notify_payload: list[bytes | None] = [None]
    cts_snapshot_ok = False
    cts_notify_ok = False

    def _cts_callback(_: object, data: bytearray) -> None:
        cts_notify_payload[0] = bytes(data)
        cts_notify_ready.set()

    try:
        await client.start_notify(CTS_CHARACTERISTIC_UUID, _cts_callback)
        await asyncio.sleep(0.5)
        cts_notify_started = True

        try:
            cts_snapshot = await client.read_gatt_char(CTS_CHARACTERISTIC_UUID)
            if cts_snapshot:
                cts_snapshot_ok = True
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
                cts_notify_ok = True
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

        if not (cts_snapshot_ok and cts_notify_ok):
            _LOGGER.debug(
                "Skipping CTS write for %s: notify/get precondition not met "
                "(snapshot_ok=%s notify_ok=%s)",
                model,
                cts_snapshot_ok,
                cts_notify_ok,
            )
        else:
            await client.write_gatt_char(CTS_CHARACTERISTIC_UUID, payload, response=True)
            _LOGGER.debug(
                "Synced current time via CTS for %s: %s",
                model,
                now.isoformat(timespec="seconds"),
            )
            cts_success = True

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
                        model,
                        tz_offset_15m,
                        dst_byte,
                    )
            except Exception as exc:
                _LOGGER.debug("Local Time Info sync failed for %s: %s", model, exc)
    except Exception as exc:
        _LOGGER.warning("Failed to sync time via CTS for %s: %s", model, exc)
    finally:
        if cts_notify_started:
            try:
                await client.stop_notify(CTS_CHARACTERISTIC_UUID)
            except Exception as exc:
                _LOGGER.debug("CTS stop_notify failed for %s: %s", model, exc)

    return cts_success


async def _sync_time_via_eeprom(
    client: BleakClient,
    model: str,
    config: DeviceConfig,
    transport: GattTransport | None,
) -> bool:
    """EEPROM-based time sync when the profile supports it."""
    if not config.supports_eeprom_time_sync:
        return False
    _LOGGER.debug(
        "EEPROM time sync supported for %s, executing sync",
        model,
    )
    if transport is None:
        transport = GattTransport(client, config)
    try:
        driver = OmronDeviceDriver(config)
        eeprom_success = await driver.sync_eeprom_time(transport)
        if not eeprom_success:
            _LOGGER.warning("EEPROM time sync returned False for %s", model)
        return eeprom_success
    except Exception as exc:
        _LOGGER.warning("EEPROM time sync failed for %s: %s", model, exc)
        return False


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

    cts_success = await _sync_time_via_cts(client, model)

    if config is None:
        config = get_device_config(model)

    eeprom_success = await _sync_time_via_eeprom(client, model, config, transport)

    if not config.supports_eeprom_time_sync and not cts_success:
        _LOGGER.debug(
            "Skipping time sync for %s: "
            "CTS characteristic not found and EEPROM time sync not supported",
            model,
        )

    return cts_success or eeprom_success
