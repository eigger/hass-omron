"""The Omron Bluetooth integration."""

from __future__ import annotations

from functools import partial
from time import perf_counter
import asyncio
import logging
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

def process_service_info(
    entry: OmronConfigEntry,
    service_info: BluetoothServiceInfoBleak,
) -> SensorUpdate:
    """Process a BluetoothServiceInfoBleak, running side effects and returning sensor data."""
    coordinator = entry.runtime_data
    data = coordinator.device_data
    update = data.update(service_info)

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

    # Get device model from config entry data, default to HEM-7322T for backward compatibility
    device_model = entry.data.get(CONF_DEVICE_MODEL, DEFAULT_DEVICE_MODEL)

    slot_aliases = aliases_dict_from_entry(entry)
    data = OmronBluetoothDeviceData(
        device_model=device_model,
        user_aliases=slot_aliases,
    )
    hass.data[DOMAIN][entry.entry_id] = {}
    hass.data[DOMAIN][entry.entry_id]['address'] = address
    hass.data[DOMAIN][entry.entry_id]['data'] = data

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
        entry=entry,
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
        started = perf_counter()
        ticker_task: asyncio.Task[None] | None = None

        async def _duration_ticker() -> None:
            """Update elapsed duration once per second while connected."""
            while True:
                elapsed_tick = round(perf_counter() - started, 3)
                duration_coordinator.async_set_updated_data(elapsed_tick)
                await asyncio.sleep(1)

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
            connection_coordinator.async_set_updated_data(True)
            duration_coordinator.async_set_updated_data(0.0)
            ticker_task = asyncio.create_task(_duration_ticker())
            result = await coordinator.device_data.async_poll(device)
            return result
        except Exception as err:
            _LOGGER.debug("polling error; keeping last successful poll data: %s", err)
            if poll_coordinator.data is not None:
                return poll_coordinator.data
            return entry.runtime_data.device_data._finish_update()
        finally:
            if ticker_task is not None:
                ticker_task.cancel()
                try:
                    await ticker_task
                except asyncio.CancelledError:
                    pass
            elapsed = round(perf_counter() - started, 3)
            duration_coordinator.async_set_updated_data(elapsed)
            connection_coordinator.async_set_updated_data(False)

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

async def get_entry_id_from_device(hass, device_id: str) -> str:
    device_reg = dr.async_get(hass)
    device_entry = device_reg.async_get(device_id)
    if not device_entry:
        raise ValueError(f"Unknown device_id: {device_id}")
    if not device_entry.config_entries:
        raise ValueError(f"No config entries for device {device_id}")

    _LOGGER.debug(f"{device_id} to {device_entry.config_entries}")
    try:
        entry_id = next(iter(device_entry.config_entries))
    except StopIteration:
        _LOGGER.error("%s None", device_id)
        return None

    return entry_id