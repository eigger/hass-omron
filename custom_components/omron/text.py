"""Text entity for a free-form device note (alias); does not change device name or IDs."""

from __future__ import annotations

from homeassistant.components.text import RestoreText
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import OmronEntity
from .types import OmronConfigEntry

_ALIAS_MAX_LEN = 64


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OmronConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up text entity for optional device note."""
    async_add_entities([OmronDeviceAliasTextEntity(entry)])


class OmronDeviceAliasTextEntity(OmronEntity, RestoreText):
    """Free-form text; state is restored by Home Assistant only (no config / registry side effects)."""

    def __init__(self, entry: OmronConfigEntry) -> None:
        self._bind(entry)
        self._default_alias = self._runtime.model
        self._attr_unique_id = self._runtime.entity_unique_id("device_alias")
        self._attr_translation_key = "device_alias"
        self._attr_has_entity_name = True
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_native_max = _ALIAS_MAX_LEN
        self._attr_native_min = 0
        self._attr_mode = "text"
        self._attr_native_value = self._default_alias

    @property
    def available(self) -> bool:
        """Editor is always available."""
        return True

    async def async_added_to_hass(self) -> None:
        """Restore last text from recorder."""
        await super().async_added_to_hass()
        if (last_text := await self.async_get_last_text_data()) is None:
            return
        self._attr_native_max = last_text.native_max
        self._attr_native_min = last_text.native_min
        if last_text.native_value is not None:
            restored = str(last_text.native_value).strip()
            self._attr_native_value = restored or self._default_alias

    async def async_set_value(self, value: str) -> None:
        """Update displayed text only (persisted via RestoreText / recorder)."""
        trimmed = (value or "").strip()[:_ALIAS_MAX_LEN]
        self._attr_native_value = trimmed or self._default_alias
        self.async_write_ha_state()
