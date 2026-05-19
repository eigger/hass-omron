"""The Omron Bluetooth integration."""

from __future__ import annotations

from functools import partial
import asyncio
import logging
import time

from sensor_state_data import BinarySensorDeviceClass as SSDBinarySensorDeviceClass
from sensor_state_data import SensorDeviceClass as SSDSensorDeviceClass

from .ble_session import omron_poll_ble_telemetry
from .omron_ble import OmronBluetoothDeviceData, SensorUpdate
from .omron_ble.const import DEFAULT_DEVICE_MODEL
from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
)
from homeassistant.const import Platform, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from datetime import timedelta
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from .const import (
    CONF_DEVICE_MODEL,
    DOMAIN,
)
from .util import aliases_dict_from_entry
from .coordinator import OmronBluetoothProcessorCoordinator
from .types import OmronConfigEntry

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.TEXT,
]

_LOGGER = logging.getLogger(__name__)

# BLE advertisement trigger control constants
POLL_COOLDOWN_SECONDS = 60
SETTLE_DELAY_SECONDS = 0.5

# When a poll fails mid-flight, keep measurement history but drop stale RSSI/battery
# unless this poll refreshed those keys (avoids showing outdated diagnostics).
_STALE_DROP_SENSOR_DEVICE_CLASSES: frozenset = frozenset({
    SSDSensorDeviceClass.BATTERY,
    SSDSensorDeviceClass.SIGNAL_STRENGTH,
})
_STALE_DROP_BINARY_DEVICE_CLASSES: frozenset = frozenset({
    SSDBinarySensorDeviceClass.BATTERY,
})


def _merge_poll_sensor_update(prev: SensorUpdate, new: SensorUpdate) -> SensorUpdate:
    """Overlay the latest poll delta on the previous coordinator snapshot.

    ``SensorData._finish_update`` returns only keys touched during that poll. The
    poll ``DataUpdateCoordinator`` assigns ``data`` from that return value alone,
    so a failed or partial poll would otherwise erase measurements still valid
    on the device.
    """
    merged_descriptions = {**prev.entity_descriptions, **new.entity_descriptions}
    merged_values = {**prev.entity_values, **new.entity_values}
    merged_b_descriptions = {
        **prev.binary_entity_descriptions,
        **new.binary_entity_descriptions,
    }
    merged_b_values = {**prev.binary_entity_values, **new.binary_entity_values}
    merged_events = {**prev.events, **new.events}

    for device_key in list(merged_values.keys()):
        desc = merged_descriptions.get(device_key)
        if desc is None or desc.device_class is None:
            continue
        if (
            desc.device_class in _STALE_DROP_SENSOR_DEVICE_CLASSES
            and device_key not in new.entity_values
        ):
            merged_values.pop(device_key, None)
            merged_descriptions.pop(device_key, None)

    for device_key in list(merged_b_values.keys()):
        desc = merged_b_descriptions.get(device_key)
        if desc is None or desc.device_class is None:
            continue
        if (
            desc.device_class in _STALE_DROP_BINARY_DEVICE_CLASSES
            and device_key not in new.binary_entity_values
        ):
            merged_b_values.pop(device_key, None)
            merged_b_descriptions.pop(device_key, None)

    return SensorUpdate(
        title=new.title if new.title is not None else prev.title,
        devices=new.devices or prev.devices,
        entity_descriptions=merged_descriptions,
        entity_values=merged_values,
        binary_entity_descriptions=merged_b_descriptions,
        binary_entity_values=merged_b_values,
        events=merged_events,
    )


def process_service_info(
    entry: OmronConfigEntry,
    service_info: BluetoothServiceInfoBleak,
) -> SensorUpdate:
    """Process a BluetoothServiceInfoBleak, running side effects and returning sensor data."""
    coordinator = entry.runtime_data
    data = coordinator.device_data
    update = data.update(service_info)

    # 1. Only attempt active sessions when the device is connectable
    if not service_info.connectable:
        return update

    entry_data = coordinator.hass.data[DOMAIN][entry.entry_id]

    is_pairing = getattr(data, "pairing_mode", False)
    is_invalid_time = getattr(data, "invalid_time", False)
    is_forced_transfer = getattr(data, "forced_transfer", False)

    is_sync_needed = (
        is_pairing
        or is_invalid_time
        or (is_forced_transfer and coordinator.poll_coordinator is not None)
    )
    if not is_sync_needed:
        return update

    _LOGGER.debug(
        "Advertisement flags for %s: pairing_mode=%s invalid_time=%s forced_transfer=%s",
        service_info.address,
        is_pairing,
        is_invalid_time,
        is_forced_transfer,
    )

    # 2. Fail fast if a GATT session is already running — try-acquire only, no queueing.
    # The device rejects a second concurrent BLE connection with SMP auth fail
    # (reasons 97/102 on ESP32 proxies), so we drop the trigger and rely on the
    # next advertisement (devices keep emitting the flag bits for several seconds)
    # to retry once the session lock is free.
    session_lock: asyncio.Lock = entry_data["session_lock"]
    if session_lock.locked():
        _LOGGER.debug(
            "BLE session lock held; skipping advertisement trigger for %s",
            service_info.address,
        )
        return update

    # 3. Enforce a shared cooldown between GATT session attempts
    now = time.time()
    last_attempt = entry_data.get("last_attempt_time", 0.0)
    if now - last_attempt < POLL_COOLDOWN_SECONDS:
        _LOGGER.debug(
            "Skipping advertisement trigger for %s (cooldown active, last attempt %ds ago)",
            service_info.address,
            int(now - last_attempt),
        )
        return update

    async def _run_auto_session() -> None:
        # forced_transfer-only path has no direct BLE op here — it just kicks
        # the poll coordinator, which goes through _async_poll_data and handles
        # its own lock acquisition. Don't hold the lock during request_refresh,
        # otherwise the child poll would see lock locked and return cached data.
        if not is_pairing and not is_invalid_time:
            entry_data["last_attempt_time"] = time.time()
            _LOGGER.debug(
                "Triggering scheduled poll via forced-transfer flag for %s",
                service_info.address,
            )
            try:
                await coordinator.poll_coordinator.async_request_refresh()
            except Exception as err:
                _LOGGER.error("Auto polling failed: %s", err)
            return

        # Pair / time-sync paths own a direct BLE op — hold the lock for that.
        if session_lock.locked():
            _LOGGER.debug(
                "BLE session lock held when auto-session task started; aborting for %s",
                service_info.address,
            )
            return
        await session_lock.acquire()  # fast-path synchronous return since we just checked
        entry_data["last_attempt_time"] = time.time()
        action = "auto-pairing" if is_pairing else "time-sync"
        _LOGGER.debug(
            "Starting %s session for %s (lock acquired)",
            action,
            service_info.address,
        )
        pair_succeeded = False
        try:
            await asyncio.sleep(SETTLE_DELAY_SECONDS)
            ble_device = service_info.device
            if is_pairing:
                async with omron_poll_ble_telemetry(entry_data):
                    await data.async_retry_pairing(ble_device)
                pair_succeeded = True
            else:  # is_invalid_time and not is_forced_transfer
                async with omron_poll_ble_telemetry(entry_data):
                    await data.async_sync_time(ble_device)
        except Exception as err:
            if is_pairing:
                _LOGGER.error("Auto pairing failed: %s", err)
            else:
                _LOGGER.error("Auto time sync failed: %s", err)
        finally:
            session_lock.release()

        # Post-pairing refresh runs AFTER the lock release so _async_poll_data
        # can acquire it for the follow-up data fetch.
        if pair_succeeded and coordinator.poll_coordinator:
            try:
                await coordinator.poll_coordinator.async_request_refresh()
            except Exception as err:
                _LOGGER.error("Post-pairing refresh failed: %s", err)

    coordinator.hass.async_create_task(_run_auto_session())

    return update


async def async_setup_entry(hass: HomeAssistant, entry: OmronConfigEntry) -> bool:
    """Set up Omron Bluetooth from a config entry."""
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    address = entry.unique_id
    assert address is not None
    if not async_ble_device_from_address(hass, address):
        _LOGGER.debug(
            "Could not find Omron device with address %s during setup; continuing without initial data",
            address,
        )

    # Get device model from config entry data (see DEFAULT_DEVICE_MODEL for fallback)
    device_model = entry.data.get(CONF_DEVICE_MODEL, DEFAULT_DEVICE_MODEL)

    slot_aliases = aliases_dict_from_entry(entry)
    data = OmronBluetoothDeviceData(
        device_model=device_model,
        user_aliases=slot_aliases,
    )
    hass.data[DOMAIN][entry.entry_id] = {}
    hass.data[DOMAIN][entry.entry_id]['address'] = address
    hass.data[DOMAIN][entry.entry_id]['data'] = data
    # Seed the advertisement-trigger cooldown so a lingering pairing-mode
    # advertisement arriving moments after the config-flow finishes does not
    # cause process_service_info to fire another auto-pairing session against
    # a device that was just paired.
    hass.data[DOMAIN][entry.entry_id]['last_attempt_time'] = time.time()
    # Per-entry serialization lock for BLE GATT sessions. All paths that open
    # a BLE link (scheduled poll, advertisement-triggered auto-session, deferred
    # pairing) try-acquire this lock and bail out immediately if it is held —
    # never queue. Two concurrent BLE connections to the same Omron device
    # cause SMP auth failures (proxy log: "auth fail reason=97/102").
    hass.data[DOMAIN][entry.entry_id]['session_lock'] = asyncio.Lock()

    # Ensure device registry entry exists even before first successful poll.
    device_registry = dr.async_get(hass)
    identifier = address.replace(":", "")[-4:].upper()
    device_name = f"{device_model} {identifier}"
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(CONNECTION_BLUETOOTH, address)},
        manufacturer="Omron",
        model=device_model,
        name=device_name,
    )

    bt_coordinator = OmronBluetoothProcessorCoordinator(
        hass,
        _LOGGER,
        address=address,
        mode=BluetoothScanningMode.PASSIVE,
        update_method=partial(process_service_info, entry),
        device_data=data,
        connectable=True,
    )
    connection_coordinator = DataUpdateCoordinator[bool](
        hass,
        _LOGGER,
        name=f"{DOMAIN}_connection_{address}",
    )
    duration_coordinator = DataUpdateCoordinator[float | None](
        hass,
        _LOGGER,
        name=f"{DOMAIN}_duration_{address}",
    )
    connection_coordinator.async_set_updated_data(False)
    duration_coordinator.async_set_updated_data(None)
    hass.data[DOMAIN][entry.entry_id]["connection_coordinator"] = connection_coordinator
    hass.data[DOMAIN][entry.entry_id]["duration_coordinator"] = duration_coordinator

    async def _async_poll_data(hass: HomeAssistant, entry: OmronConfigEntry) -> SensorUpdate:
        try:
            device = async_ble_device_from_address(hass, hass.data[DOMAIN][entry.entry_id]['address'])
            if not device:
                _LOGGER.debug("BLE device not found; keeping last successful poll data")
                if poll_coordinator.data is not None:
                    return poll_coordinator.data
                _LOGGER.debug(
                    "BLE device not found and no cached poll data exists yet; "
                    "returning empty update until device is discovered again"
                )
                return entry.runtime_data.device_data._finish_update()
            coordinator = entry.runtime_data
            entry_data = hass.data[DOMAIN][entry.entry_id]
            session_lock: asyncio.Lock = entry_data["session_lock"]

            # Try-acquire only — if another BLE session is in flight (e.g. an
            # advertisement-triggered auto-pairing started moments ago), skip
            # this scheduled poll and serve cached data. The next interval (or
            # a request_refresh from the active session) will retry once the
            # lock frees. Two concurrent connections to the same Omron device
            # provoke SMP auth failures, so we never queue here.
            if session_lock.locked():
                _LOGGER.debug(
                    "Skipping scheduled poll: BLE session lock held for %s",
                    entry_data["address"],
                )
                if poll_coordinator.data is not None:
                    return poll_coordinator.data
                return entry.runtime_data.device_data._finish_update()

            await session_lock.acquire()  # synchronous fast-path since we just checked
            try:
                async with omron_poll_ble_telemetry(entry_data):
                    result = await coordinator.device_data.async_poll(device)
                prev_data = poll_coordinator.data
                if prev_data is not None:
                    result = _merge_poll_sensor_update(prev_data, result)
                return result
            finally:
                session_lock.release()
        except Exception as err:
            _LOGGER.debug("polling error; keeping last successful poll data: %s", err)
            if poll_coordinator.data is not None:
                return poll_coordinator.data
            return entry.runtime_data.device_data._finish_update()

    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, 300)
    )

    poll_coordinator = DataUpdateCoordinator[SensorUpdate](
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=partial(_async_poll_data, hass, entry),
        update_interval=timedelta(seconds=scan_interval),
    )
    
    entry.runtime_data = bt_coordinator
    entry.runtime_data.poll_coordinator = poll_coordinator
    # Give the radio a moment in case a setup-flow BLE link was just torn down
    # — initial registration triggers async_setup_entry within ~20 ms of the
    # config-flow disconnect, before the device is ready to accept a new
    # connection. 0.5 s is cheap insurance on reloads/restarts too.
    await asyncio.sleep(0.5)
    await poll_coordinator.async_refresh()
    if not poll_coordinator.last_update_success:
        _LOGGER.warning(
            "Initial poll update failed for %s; entities will use cached/empty state: %s",
            address,
            poll_coordinator.last_exception,
        )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # only start after all platforms have had a chance to subscribe
    entry.async_on_unload(bt_coordinator.async_start())
    entry.async_on_unload(entry.add_update_listener(update_listener))
    return True

async def update_listener(hass: HomeAssistant, entry: OmronConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: OmronConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)