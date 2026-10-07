"""Height input for scales; the BMI sensors read it.

The value lives in Home Assistant only (RestoreNumber). Nothing is written to
the scale.
"""

from __future__ import annotations

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import EntityCategory, UnitOfLength
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .data import signal_height_updated
from .entity import OmronEntity
from .omron_ble.body_metrics import HEIGHT_MAX_CM, HEIGHT_MIN_CM
from .types import OmronConfigEntry

# Scales served today keep one user; the slot keys runtime.heights_cm.
_SLOT = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OmronConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the Height entity for scales; blood-pressure monitors get none."""
    if not entry.runtime_data.is_scale:
        return
    async_add_entities([OmronHeightNumberEntity(entry, _SLOT)])


def _valid_height(value: float | None) -> float | None:
    """A stored height in range, else None (0 or anything outside clears it)."""
    if value is None:
        return None
    try:
        height = float(value)
    except (TypeError, ValueError):
        return None
    if HEIGHT_MIN_CM <= height <= HEIGHT_MAX_CM:
        return height
    return None


class OmronHeightNumberEntity(OmronEntity, RestoreNumber):
    """Height in cm for one user slot; 0 clears it."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "height"
    _attr_device_class = NumberDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = UnitOfLength.CENTIMETERS
    _attr_native_min_value = 0
    _attr_native_max_value = HEIGHT_MAX_CM
    _attr_native_step = 0.1
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:human-male-height"

    def __init__(self, entry: OmronConfigEntry, slot: int) -> None:
        self._bind(entry)
        self._slot = slot
        self._attr_unique_id = self._runtime.entity_unique_id("height")
        self._attr_native_value = None

    @property
    def available(self) -> bool:
        """Editor is always available."""
        return True

    async def async_added_to_hass(self) -> None:
        """Restore the last height and hand it to the BMI sensors."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None:
            self._attr_native_value = _valid_height(last.native_value)
        self._publish()

    async def async_will_remove_from_hass(self) -> None:
        """Tell the BMI sensors the height is gone (disabled or deleted).

        An entry unload takes the BMI sensors down too; clearing the height
        then would store an empty height for their restore.
        """
        await super().async_will_remove_from_hass()
        if self._runtime.unloading:
            return
        self._runtime.heights_cm[self._slot] = None
        async_dispatcher_send(self.hass, signal_height_updated(self._entry.entry_id))

    async def async_set_native_value(self, value: float) -> None:
        """Store a height in cm; 0 clears it."""
        if value == 0:
            height: float | None = None
        elif value < HEIGHT_MIN_CM or value > HEIGHT_MAX_CM:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="height_out_of_range",
                translation_placeholders={
                    "min": f"{HEIGHT_MIN_CM:g}",
                    "max": f"{HEIGHT_MAX_CM:g}",
                },
            )
        else:
            height = round(float(value), 1)
        self._attr_native_value = height
        self.async_write_ha_state()
        self._publish()

    def _publish(self) -> None:
        self._runtime.heights_cm[self._slot] = self._attr_native_value
        async_dispatcher_send(self.hass, signal_height_updated(self._entry.entry_id))
