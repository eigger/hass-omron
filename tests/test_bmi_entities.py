"""Height input and BMI sensors on a weight scale (#236)."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.omron.const import CONF_DEVICE_MODEL, DOMAIN
from custom_components.omron.omron_ble.const import ExtendedSensorDeviceClass

ADDRESS = "AA:BB:CC:DD:EE:FF"
HEIGHT_UID = "hn_300t2_eeff_height"
BMI_UID = "hn_300t2_eeff_bmi"
CATEGORY_UID = "hn_300t2_eeff_bmi_category"
WEIGHT_UID = "hn_300t2_eeff_none_weight"

HEIGHT_ID = "number.scale_height"
BMI_ID = "sensor.scale_bmi"
CATEGORY_ID = "sensor.scale_bmi_category"


def _entry(hass: HomeAssistant, model: str) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title=f"{model} EEFF",
        data={CONF_DEVICE_MODEL: model},
    )
    entry.add_to_hass(hass)
    return entry


def _preregister(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Pin entity ids so the tests (and restore cache) can name them."""
    registry = er.async_get(hass)
    for domain, uid, object_id in (
        ("number", HEIGHT_UID, "scale_height"),
        ("sensor", BMI_UID, "scale_bmi"),
        ("sensor", CATEGORY_UID, "scale_bmi_category"),
    ):
        registry.async_get_or_create(
            domain, DOMAIN, uid, suggested_object_id=object_id, config_entry=entry
        )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _push_weight(hass: HomeAssistant, entry: MockConfigEntry, kg: float) -> None:
    runtime = entry.runtime_data
    runtime.device_data.update_sensor(
        "weight",
        "kg",
        kg,
        device_class=ExtendedSensorDeviceClass.WEIGHT,
        name="Weight",
    )
    runtime.poll_coordinator.async_set_updated_data(
        runtime.device_data._finish_update()
    )
    await hass.async_block_till_done()


async def _set_height(hass: HomeAssistant, value: float) -> None:
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": HEIGHT_ID, "value": value},
        blocking=True,
    )
    await hass.async_block_till_done()


def _unique_ids(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    return {
        e.unique_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }


async def test_a_weight_scale_gets_height_and_bmi(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, "HN-300T2")
    await _setup(hass, entry)

    unique_ids = _unique_ids(hass, entry)
    assert {HEIGHT_UID, BMI_UID, CATEGORY_UID} <= unique_ids

    registry = er.async_get(hass)
    height_entry = registry.async_get(
        registry.async_get_entity_id("number", DOMAIN, HEIGHT_UID)
    )
    assert height_entry.entity_category == "config"

    await _unload(hass, entry)


async def test_a_blood_pressure_monitor_gets_neither(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, "HEM-7155T")
    await _setup(hass, entry)

    unique_ids = _unique_ids(hass, entry)
    assert not any(
        uid.endswith(("_height", "_bmi", "_bmi_category")) for uid in unique_ids
    )
    assert not hass.states.async_entity_ids("number")

    await _unload(hass, entry)


async def test_bmi_follows_weight_and_height(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, "HN-300T2")
    _preregister(hass, entry)
    await _setup(hass, entry)

    # No inputs yet: unknown, not unavailable.
    assert hass.states.get(HEIGHT_ID).state == "unknown"
    assert hass.states.get(BMI_ID).state == "unknown"
    assert hass.states.get(CATEGORY_ID).state == "unknown"

    await _set_height(hass, 175)
    assert hass.states.get(HEIGHT_ID).state == "175.0"
    assert hass.states.get(BMI_ID).state == "unknown"

    await _push_weight(hass, entry, 70.0)
    bmi = hass.states.get(BMI_ID)
    assert bmi.state == "22.9"
    assert bmi.attributes["unit_of_measurement"] == "kg/m²"
    assert bmi.attributes["weight_kg"] == 70.0
    assert bmi.attributes["height_cm"] == 175.0
    assert "device_class" not in bmi.attributes
    category = hass.states.get(CATEGORY_ID)
    assert category.state == "normal"
    assert category.attributes["options"] == [
        "underweight",
        "normal",
        "overweight",
        "obesity_class_1",
        "obesity_class_2",
        "obesity_class_3",
    ]

    # A new weight recalculates.
    await _push_weight(hass, entry, 92.0)
    assert hass.states.get(BMI_ID).state == "30.0"
    assert hass.states.get(CATEGORY_ID).state == "obesity_class_1"

    # A new height recalculates.
    await _set_height(hass, 192.0)
    assert hass.states.get(BMI_ID).state == "25.0"
    assert hass.states.get(CATEGORY_ID).state == "overweight"

    # 0 clears the height and the BMI with it.
    await _set_height(hass, 0)
    assert hass.states.get(HEIGHT_ID).state == "unknown"
    assert hass.states.get(BMI_ID).state == "unknown"
    assert hass.states.get(CATEGORY_ID).state == "unknown"

    await _unload(hass, entry)


async def test_a_height_below_100_cm_is_rejected(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, "HN-300T2")
    _preregister(hass, entry)
    await _setup(hass, entry)
    await _set_height(hass, 170)

    with pytest.raises(ServiceValidationError) as err:
        await _set_height(hass, 50)
    assert err.value.translation_key == "height_out_of_range"
    assert err.value.translation_placeholders == {"min": "100", "max": "220"}
    assert hass.states.get(HEIGHT_ID).state == "170.0"

    await _unload(hass, entry)


async def test_us_customary_units_leave_bmi_alone(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry = _entry(hass, "HN-300T2")
    _preregister(hass, entry)
    await _setup(hass, entry)

    await _set_height(hass, 175)
    await _push_weight(hass, entry, 70.0)
    bmi = hass.states.get(BMI_ID)
    assert bmi.state == "22.9"
    assert bmi.attributes["unit_of_measurement"] == "kg/m²"
    assert bmi.attributes["weight_kg"] == 70.0

    await _unload(hass, entry)


async def test_bmi_still_works_with_the_weight_sensor_disabled(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, "HN-300T2")
    _preregister(hass, entry)
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        WEIGHT_UID,
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    await _setup(hass, entry)

    await _set_height(hass, 175)
    await _push_weight(hass, entry, 70.0)
    assert hass.states.get(BMI_ID).state == "22.9"

    await _unload(hass, entry)


async def test_bmi_and_height_are_restored(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(HEIGHT_ID, "175.0"),
                {
                    "native_max_value": 220,
                    "native_min_value": 0,
                    "native_step": 0.1,
                    "native_unit_of_measurement": "cm",
                    "native_value": 175.0,
                },
            ),
            (State(BMI_ID, "22.9"), {"weight_kg": 70.0, "height_cm": 175.0}),
            (State(CATEGORY_ID, "normal"), {"weight_kg": 70.0, "height_cm": 175.0}),
        ],
    )
    entry = _entry(hass, "HN-300T2")
    _preregister(hass, entry)
    await _setup(hass, entry)

    assert hass.states.get(HEIGHT_ID).state == "175.0"
    assert entry.runtime_data.heights_cm == {1: 175.0}
    assert hass.states.get(BMI_ID).state == "22.9"
    assert hass.states.get(CATEGORY_ID).state == "normal"

    # A fresh weight replaces the restored one.
    await _push_weight(hass, entry, 61.3)
    assert hass.states.get(BMI_ID).state == "20.0"

    await _unload(hass, entry)


async def test_restored_height_is_ignored_when_the_height_entity_is_disabled(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    mock_restore_cache_with_extra_data(
        hass,
        [(State(BMI_ID, "22.9"), {"weight_kg": 70.0, "height_cm": 175.0})],
    )
    entry = _entry(hass, "HN-300T2")
    _preregister(hass, entry)
    er.async_get(hass).async_update_entity(
        HEIGHT_ID, disabled_by=er.RegistryEntryDisabler.USER
    )
    await _setup(hass, entry)

    assert hass.states.get(HEIGHT_ID) is None
    bmi = hass.states.get(BMI_ID)
    assert bmi.state == "unknown"
    assert bmi.attributes["weight_kg"] == 70.0
    assert bmi.attributes["height_cm"] is None

    await _unload(hass, entry)


async def test_an_out_of_range_restored_height_is_dropped(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(HEIGHT_ID, "50.0"),
                {
                    "native_max_value": 220,
                    "native_min_value": 0,
                    "native_step": 0.1,
                    "native_unit_of_measurement": "cm",
                    "native_value": 50.0,
                },
            ),
        ],
    )
    entry = _entry(hass, "HN-300T2")
    _preregister(hass, entry)
    await _setup(hass, entry)

    assert hass.states.get(HEIGHT_ID).state == "unknown"
    assert entry.runtime_data.heights_cm == {1: None}

    await _unload(hass, entry)


async def test_disabling_the_height_entity_clears_bmi(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, "HN-300T2")
    _preregister(hass, entry)
    await _setup(hass, entry)
    await _set_height(hass, 175)
    await _push_weight(hass, entry, 70.0)
    assert hass.states.get(BMI_ID).state == "22.9"

    er.async_get(hass).async_update_entity(
        HEIGHT_ID, disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done()
    assert hass.states.get(BMI_ID).state == "unknown"

    await _unload(hass, entry)
