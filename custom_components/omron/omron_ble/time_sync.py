"""GATT time synchronization (CTS / LTI / EEPROM) for Omron devices."""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

from bleak import BleakClient
from blesession import NotificationTimeout, Notifications, guarded_write

from .connection import _bleak_refresh_services
from .const import CTS_CHARACTERISTIC_UUID, LOCAL_TIME_INFO_UUID
from .devices import get_device_config
from .driver import OmronDeviceDriver
from .session import OmronDeviceSession

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


_CTS_SETTLE_S = 0.5
_CTS_NOTIFY_TIMEOUT_S = 1.0


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
    cts_snapshot_ok = False
    cts_notify_ok = False

    try:
        async with Notifications(client, CTS_CHARACTERISTIC_UUID, settle=_CTS_SETTLE_S) as cts_notifications:
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
                cts_notify_payload = await cts_notifications.next(_CTS_NOTIFY_TIMEOUT_S, step="cts_notify")
                cts_notify_ok = True
                _LOGGER.debug(
                    "CTS notify received for %s before sync: %s",
                    model,
                    cts_notify_payload.hex(),
                )
            except NotificationTimeout:
                _LOGGER.debug(
                    "CTS notify not received before sync for %s (continuing)",
                    model,
                )

            if not cts_snapshot_ok:
                # Only require a successful read (snapshot) to confirm the CTS characteristic
                # is accessible.  Some devices never send a CTS notification before the write,
                # so requiring cts_notify_ok would incorrectly skip time sync on those devices.
                _LOGGER.debug(
                    "Skipping CTS write for %s: snapshot read failed "
                    "(snapshot_ok=%s notify_ok=%s)",
                    model,
                    cts_snapshot_ok,
                    cts_notify_ok,
                )
            else:
                await guarded_write(
                    client, CTS_CHARACTERISTIC_UUID, payload, step="time_sync", response=True
                )
                _LOGGER.debug(
                    "Synced current time via CTS for %s: %s (notify_ok=%s)",
                    model,
                    now.isoformat(timespec="seconds"),
                    cts_notify_ok,
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
                        await guarded_write(
                            client, LOCAL_TIME_INFO_UUID, lti_payload,
                            step="time_sync", response=True,
                        )
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

    return cts_success


async def _sync_time_via_eeprom(
    client: BleakClient,
    model: str,
    config: DeviceConfig,
    transport: OmronDeviceSession,
) -> bool:
    """EEPROM-based time sync; caller must hold an open memory readout session."""
    if not config.supports_eeprom_time_sync:
        return False
    _LOGGER.debug(
        "EEPROM time sync supported for %s, executing sync",
        model,
    )
    try:
        driver = OmronDeviceDriver(config)
        eeprom_success = await driver.sync_eeprom_time(transport)
        if not eeprom_success:
            _LOGGER.warning("EEPROM time sync returned False for %s", model)
        return eeprom_success
    except Exception as exc:
        _LOGGER.warning("EEPROM time sync failed for %s: %s", model, exc)
        return False


async def _sync_eeprom_with_session(
    client: BleakClient,
    model: str,
    config: DeviceConfig,
    transport: OmronDeviceSession | None,
    *,
    leave_memory_session_open: bool = False,
) -> bool:
    """EEPROM time sync, opening a memory session when one is not already held."""
    if not config.supports_eeprom_time_sync:
        return False
    if transport is None:
        transport = OmronDeviceSession.adopt(client, config)
    if transport.memory.memory_session_active:
        return await _sync_time_via_eeprom(client, model, config, transport)
    if leave_memory_session_open:
        await transport.unlock()
        await transport.memory.open_memory_session()
        return await _sync_time_via_eeprom(client, model, config, transport)
    async with transport.memory_session_after_unlock():
        return await _sync_time_via_eeprom(client, model, config, transport)


async def async_sync_eeprom_time(
    client: BleakClient,
    model: str,
    config: DeviceConfig | None = None,
    transport: OmronDeviceSession | None = None,
) -> bool:
    """EEPROM-only time sync (uses or opens a memory readout session)."""
    if not client.is_connected:
        _LOGGER.debug(
            "Skipping EEPROM time sync for %s: client is not connected",
            model,
        )
        return False
    if config is None:
        config = get_device_config(model)
    return await _sync_eeprom_with_session(client, model, config, transport)


async def async_sync_device_time(
    client: BleakClient,
    model: str,
    config: DeviceConfig | None = None,
    transport: OmronDeviceSession | None = None,
    *,
    leave_memory_session_open: bool = False,
) -> bool:
    """Sync current local time via EEPROM (memory session) then CTS.

    EEPROM runs inside a memory readout session. CTS always runs only after that
    session is closed so notify/write on 0x1801 does not overlap the Omron
    memory protocol. During an active poll memory session, use
    :func:`async_sync_eeprom_time` instead so CTS is not attempted mid-readout.
    """
    if not client.is_connected:
        _LOGGER.debug(
            "Skipping time sync for %s: client is not connected",
            model,
        )
        return False

    if config is None:
        config = get_device_config(model)

    eeprom_success = False

    if config.supports_eeprom_time_sync:
        eeprom_success = await _sync_eeprom_with_session(
            client, model, config, transport,
            leave_memory_session_open=leave_memory_session_open,
        )
        if eeprom_success:
            return True

        # Pairing keeps the newly opened memory session alive for the poll
        # that immediately follows. Do not enter CTS while that protocol
        # session is active; the peripheral may reject overlapping GATT work.
        if leave_memory_session_open:
            return False

    cts_success = await _sync_time_via_cts(client, model)

    if config.supports_eeprom_time_sync and not eeprom_success:
        eeprom_success = await _sync_eeprom_with_session(
            client, model, config, transport,
            leave_memory_session_open=leave_memory_session_open,
        )

    if not config.supports_eeprom_time_sync and not cts_success:
        _LOGGER.debug(
            "Skipping time sync for %s: "
            "CTS characteristic not found and EEPROM time sync not supported",
            model,
        )

    return cts_success or eeprom_success
