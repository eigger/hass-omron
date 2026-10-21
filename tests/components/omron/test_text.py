"""Device alias text service behavior through a real loaded config entry."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.omron.const import DOMAIN
from tests.components.omron.test_sensor import BP_MODEL, _entry, _setup, _unload


async def test_alias_text_trims_and_falls_back_to_the_model(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(BP_MODEL)
    await _setup(hass, entry)
    entity_id = er.async_get(hass).async_get_entity_id(
        "text", DOMAIN, "hem_7155t_eeff_device_alias"
    )

    await hass.services.async_call(
        "text",
        "set_value",
        {"entity_id": entity_id, "value": "  Dad's cuff  "},
        blocking=True,
    )
    assert hass.states.get(entity_id).state == "Dad's cuff"

    await hass.services.async_call(
        "text", "set_value", {"entity_id": entity_id, "value": "   "}, blocking=True
    )
    assert hass.states.get(entity_id).state == BP_MODEL

    await _unload(hass, entry)
