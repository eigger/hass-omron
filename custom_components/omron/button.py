"""Support for Omron button entities."""

from __future__ import annotations

import asyncio
from time import perf_counter

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .types import OmronConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OmronConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Omron button entities."""
    address = hass.data[DOMAIN][entry.entry_id]["address"]
    model = hass.data[DOMAIN][entry.entry_id]["data"].device_model
    identifier = address.replace(":", "")[-4:].lower()
    model_slug = model.lower().replace("-", "_")
    refresh_description = ButtonEntityDescription(
        key=f"{model_slug}_{identifier}_refresh_data",
        name=f"{model} {identifier.upper()} Refresh Data",
        icon="mdi:refresh",
        entity_category=EntityCategory.CONFIG,
    )
    pairing_retry_description = ButtonEntityDescription(
        key=f"{model_slug}_{identifier}_retry_pairing",
        name=f"{model} {identifier.upper()} Retry Pairing",
        icon="mdi:bluetooth-connect",
        entity_category=EntityCategory.CONFIG,
    )

    async_add_entities(
        [
            OmronRefreshDataButtonEntity(hass, entry, refresh_description),
            OmronRetryPairingButtonEntity(hass, entry, pairing_retry_description),
        ]
    )


class OmronRefreshDataButtonEntity(ButtonEntity):
    """Button entity to trigger an immediate data refresh poll."""

    entity_description: ButtonEntityDescription

    def __init__(
        self,
        hass: HomeAssistant,
        entry: OmronConfigEntry,
        description: ButtonEntityDescription,
    ) -> None:
        """Initialize entity."""
        self.hass = hass
        self.entity_description = description
        self._entry_id = entry.entry_id
        self._entry = entry
        self._address = hass.data[DOMAIN][entry.entry_id]["address"]
        self._attr_unique_id = description.key

    @property
    def device_info(self) -> DeviceInfo:
        """Attach button to the same BLE device."""
        return DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._address)},
        )

    async def async_press(self) -> None:
        """Handle button press to poll device and refresh sensor data."""
        poll_coordinator = self._entry.runtime_data.poll_coordinator
        try:
            await poll_coordinator.async_request_refresh()
        except Exception as err:
            raise HomeAssistantError(f"Failed to refresh data: {err}") from err


class OmronRetryPairingButtonEntity(ButtonEntity):
    """Button entity to retry BLE pairing/bonding on demand."""

    entity_description: ButtonEntityDescription

    def __init__(
        self,
        hass: HomeAssistant,
        entry: OmronConfigEntry,
        description: ButtonEntityDescription,
    ) -> None:
        """Initialize entity."""
        self.hass = hass
        self.entity_description = description
        self._entry_id = entry.entry_id
        self._entry = entry
        self._address = hass.data[DOMAIN][entry.entry_id]["address"]
        self._attr_unique_id = description.key

    @property
    def device_info(self) -> DeviceInfo:
        """Attach button to the same BLE device."""
        return DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._address)},
        )

    async def async_press(self) -> None:
        """Handle button press to retry pairing/bonding."""
        ble_device = async_ble_device_from_address(self.hass, self._address)
        if ble_device is None:
            raise HomeAssistantError(f"BLE device not available: {self._address}")

        connection_coordinator = self.hass.data[DOMAIN][self._entry_id]["connection_coordinator"]
        duration_coordinator = self.hass.data[DOMAIN][self._entry_id]["duration_coordinator"]
        started = perf_counter()
        ticker_task: asyncio.Task[None] | None = None

        async def _duration_ticker() -> None:
            """Update elapsed duration once per second while connected."""
            while True:
                elapsed_tick = round(perf_counter() - started, 3)
                duration_coordinator.async_set_updated_data(elapsed_tick)
                await asyncio.sleep(1)

        data = self.hass.data[DOMAIN][self._entry_id]["data"]
        try:
            connection_coordinator.async_set_updated_data(True)
            duration_coordinator.async_set_updated_data(0.0)
            ticker_task = asyncio.create_task(_duration_ticker())
            await data.async_retry_pairing(ble_device)
            # Mirror setup behavior: run an immediate poll after pairing so
            # protected GATT paths are exercised and bond/session state settles.
            poll_coordinator = self._entry.runtime_data.poll_coordinator
            await poll_coordinator.async_request_refresh()
        except Exception as err:
            raise HomeAssistantError(f"Failed to retry pairing: {err}") from err
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
