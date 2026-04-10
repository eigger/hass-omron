"""Support for Omron button entities."""

from __future__ import annotations

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

    description = ButtonEntityDescription(
        key=f"{model_slug}_{identifier}_sync_time",
        name=f"{model} {identifier.upper()} Sync Time",
        icon="mdi:clock-sync",
        entity_category=EntityCategory.CONFIG,
    )

    async_add_entities([OmronSyncTimeButtonEntity(hass, entry, description)])


class OmronSyncTimeButtonEntity(ButtonEntity):
    """Button entity to trigger manual time synchronization."""

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
        self._address = hass.data[DOMAIN][entry.entry_id]["address"]
        self._attr_unique_id = description.key

    @property
    def device_info(self) -> DeviceInfo:
        """Attach button to the same BLE device."""
        return DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._address)},
        )

    async def async_press(self) -> None:
        """Handle button press to sync local time to device."""
        ble_device = async_ble_device_from_address(self.hass, self._address)
        if ble_device is None:
            raise HomeAssistantError(f"BLE device not available: {self._address}")

        data = self.hass.data[DOMAIN][self._entry_id]["data"]
        synced = await data.async_sync_current_time(ble_device)
        if not synced:
            raise HomeAssistantError(
                "Device does not expose Current Time characteristic (CTS)"
            )
