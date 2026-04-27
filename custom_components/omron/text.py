"""Text entity for a free-form device note (alias); does not change device name or IDs."""

from __future__ import annotations

from homeassistant.components.text import RestoreText
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .types import OmronConfigEntry

_ALIAS_MAX_LEN = 64


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OmronConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up text entity for optional device note."""
    async_add_entities([OmronDeviceAliasTextEntity(hass, entry.entry_id)])


class OmronDeviceAliasTextEntity(RestoreText):
    """Free-form text; state is restored by Home Assistant only (no config / registry side effects)."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self._address = hass.data[DOMAIN][entry_id]["address"]
        model = hass.data[DOMAIN][entry_id]["data"].device_model
        identifier = self._address.replace(":", "")[-4:].lower()
        model_slug = model.lower().replace("-", "_")
        self._attr_unique_id = f"{model_slug}_{identifier}_device_alias"
        self._attr_translation_key = "device_alias"
        self._attr_has_entity_name = True
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_native_max = _ALIAS_MAX_LEN
        self._attr_native_min = 0
        self._attr_mode = "text"
        self._attr_native_value = ""

    @property
    def available(self) -> bool:
        """Editor is always available."""
        return True

    @property
    def device_info(self) -> DeviceInfo:
        """Attach to the Omron BLE device."""
        return DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._address)},
        )

    async def async_added_to_hass(self) -> None:
        """Restore last text from recorder."""
        await super().async_added_to_hass()
        if (last_text := await self.async_get_last_text_data()) is None:
            return
        self._attr_native_max = last_text.native_max
        self._attr_native_min = last_text.native_min
        if last_text.native_value is not None:
            self._attr_native_value = str(last_text.native_value)

    async def async_set_value(self, value: str) -> None:
        """Update displayed text only (persisted via RestoreText / recorder)."""
        self._attr_native_value = (value or "").strip()[:_ALIAS_MAX_LEN]
        self.async_write_ha_state()
