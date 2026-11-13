"""Users of a single-slot device: the subentry flow, sub-devices and sensors (#301)."""

from __future__ import annotations

from typing import Any

import pytest
import voluptuous as vol
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.omron.config_flow import (
    OmronConfigFlow,
    OmronUserSubentryFlow,
)
from custom_components.omron.const import (
    CONF_ASSIGNMENT_STRATEGY,
    CONF_DEVICE_MODEL,
    CONF_WEIGHT_TOLERANCE_PCT,
    DOMAIN,
    STRATEGY_RULES,
    STRATEGY_WEIGHT,
    SUBENTRY_USER,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"
SCALE = "HN-300T2"
CUFF = "HEM-7600T"
MULTI_SLOT_CUFF = "HEM-7155T"


def _subentry(subentry_id: str, name: str, **data: Any) -> dict[str, Any]:
    return {
        "subentry_id": subentry_id,
        "subentry_type": SUBENTRY_USER,
        "title": name,
        "unique_id": None,
        "data": {"name": name, **data},
    }


def _entry(
    hass: HomeAssistant,
    model: str,
    *,
    users: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title=f"{model} EEFF",
        data={CONF_DEVICE_MODEL: model},
        options=options or {},
        subentries_data=users or [],
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    # A subentry change reloads the entry in the background; let it finish.
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _start(hass: HomeAssistant, entry: MockConfigEntry, source: str = "user"):
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_USER), context={"source": source}
    )


def _device(
    hass: HomeAssistant, entry: MockConfigEntry, *, identifier=None, connection=None
):
    """A device of the entry, by identifier or connection, on any supported HA."""
    registry = dr.async_get(hass)
    if hasattr(registry, "async_get_device_by_identifier"):
        if identifier is not None:
            return registry.async_get_device_by_identifier(identifier, entry.entry_id)
        return registry.async_get_device_by_connection(connection, entry.entry_id)
    if identifier is not None:
        return registry.async_get_device(identifiers={identifier})
    return registry.async_get_device(connections={connection})


def _subentries_of(device, entry: MockConfigEntry) -> set[str]:
    if hasattr(device, "config_subentry_id"):
        return {device.config_subentry_id}
    return device.config_entries_subentries[entry.entry_id]


def _unique_ids(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    return {
        e.unique_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }


# --- which devices offer users -----------------------------------------------------


@pytest.mark.parametrize(
    ("model", "offered"),
    [
        (SCALE, True),
        (CUFF, True),
        (MULTI_SLOT_CUFF, False),
        # A body-composition scale offers users by slot.
        ("HBF-222T", True),
    ],
)
async def test_only_these_devices_offer_users(
    hass: HomeAssistant, enable_bluetooth: None, model: str, offered: bool
) -> None:
    entry = _entry(hass, model)
    supported = OmronConfigFlow.async_get_supported_subentry_types(entry)
    assert supported == ({SUBENTRY_USER: OmronUserSubentryFlow} if offered else {})


# --- the flow: the fields follow the strategy ----------------------------------------


async def test_a_scale_by_weight_asks_for_a_name_and_an_expected_weight(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, SCALE)
    await _setup(hass, entry)

    result = await _start(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "weight"
    fields = {str(key): key for key in result["data_schema"].schema}
    assert set(fields) == {"name", "expected_weight", "height_cm"}
    assert isinstance(fields["expected_weight"], vol.Required)
    assert isinstance(fields["name"], vol.Required)
    assert isinstance(fields["height_cm"], vol.Optional)

    created = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Alice", "expected_weight": 62.5, "height_cm": 168},
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    (subentry,) = entry.subentries.values()
    assert subentry.subentry_type == SUBENTRY_USER
    assert subentry.title == "Alice"
    assert dict(subentry.data) == {
        "name": "Alice",
        "expected_weight": 62.5,
        "height_cm": 168,
    }
    await _unload(hass, entry)


async def test_a_scale_by_rules_asks_for_windows_and_a_weight_range(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, SCALE, options={CONF_ASSIGNMENT_STRATEGY: STRATEGY_RULES})
    await _setup(hass, entry)

    result = await _start(hass, entry)
    assert result["step_id"] == "rules"
    assert {str(key) for key in result["data_schema"].schema} == {
        "name",
        "height_cm",
        "time_windows",
        "weight_min",
        "weight_max",
    }
    await _unload(hass, entry)


async def test_a_cuff_asks_for_windows_and_pressure_ranges(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF)
    await _setup(hass, entry)

    result = await _start(hass, entry)
    assert result["step_id"] == "pressure"
    assert {str(key) for key in result["data_schema"].schema} == {
        "name",
        "time_windows",
        "sys_min",
        "sys_max",
        "dia_min",
        "dia_max",
    }
    created = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Bob", "time_windows": "06:00-09:00", "sys_min": 130},
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    (subentry,) = entry.subentries.values()
    assert dict(subentry.data) == {
        "name": "Bob",
        "time_windows": "06:00-09:00",
        "sys_min": 130,
    }
    await _unload(hass, entry)


# --- the flow: validation and warnings -----------------------------------------------


async def test_bad_input_is_reported_on_its_field(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF, users=[_subentry("u1", "Bob", sys_min=130)])
    await _setup(hass, entry)

    result = await _start(hass, entry)
    for user_input, errors in (
        ({"name": "bob", "sys_min": 100}, {"name": "invalid_name"}),
        ({"name": "  "}, {"name": "invalid_name"}),
        (
            {"name": "Eve", "time_windows": "morning"},
            {"time_windows": "invalid_time_windows"},
        ),
        ({"name": "Eve", "sys_min": 140, "sys_max": 120}, {"sys_max": "invalid_range"}),
    ):
        again = await hass.config_entries.subentries.async_configure(
            result["flow_id"], user_input
        )
        assert again["type"] is FlowResultType.FORM, user_input
        assert again["errors"] == errors, user_input
    await _unload(hass, entry)


async def test_a_user_with_no_rules_is_warned_but_can_still_be_saved(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF)
    await _setup(hass, entry)

    result = await _start(hass, entry)
    warned = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Bob"}
    )
    assert warned["type"] is FlowResultType.FORM
    assert warned["step_id"] == "confirm_no_rules"
    assert warned["description_placeholders"]["name"] == "Bob"
    assert not entry.subentries

    created = await hass.config_entries.subentries.async_configure(
        warned["flow_id"], {}
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert [s.title for s in entry.subentries.values()] == ["Bob"]
    await _unload(hass, entry)


async def test_overlapping_time_windows_warn_without_blocking(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(
        hass, CUFF, users=[_subentry("u1", "Bob", time_windows="06:00-09:00")]
    )
    await _setup(hass, entry)

    result = await _start(hass, entry)
    warned = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Eve", "time_windows": "08:00-10:00"}
    )
    assert warned["step_id"] == "confirm_overlap"
    assert warned["description_placeholders"] == {"name": "Eve", "others": "Bob"}

    created = await hass.config_entries.subentries.async_configure(
        warned["flow_id"], {}
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert sorted(s.title for s in entry.subentries.values()) == ["Bob", "Eve"]
    await _unload(hass, entry)


async def test_touching_windows_and_overlapping_values_do_not_warn(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(
        hass,
        CUFF,
        users=[_subentry("u1", "Bob", time_windows="06:00-09:00", sys_min=120)],
    )
    await _setup(hass, entry)

    result = await _start(hass, entry)
    created = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Eve", "time_windows": "09:00-10:00", "sys_min": 125},
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await _unload(hass, entry)


async def test_a_weight_user_gets_no_rule_warnings(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, SCALE)
    await _setup(hass, entry)
    result = await _start(hass, entry)
    created = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Alice", "expected_weight": 60}
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await _unload(hass, entry)


async def test_a_user_can_be_edited(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(
        hass,
        SCALE,
        users=[_subentry("u1", "Alice", expected_weight=62.0, height_cm=168)],
    )
    await _setup(hass, entry)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_USER),
        context={"source": "reconfigure", "subentry_id": "u1"},
    )
    assert result["step_id"] == "weight"
    suggested = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description
    }
    assert suggested == {"name": "Alice", "expected_weight": 62.0, "height_cm": 168}

    # The name is her own, so keeping it is fine; clearing the height drops it.
    done = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Alice", "expected_weight": 63.5}
    )
    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    subentry = entry.subentries["u1"]
    assert dict(subentry.data) == {"name": "Alice", "expected_weight": 63.5}
    await _unload(hass, entry)


# --- subentry changes reload the entry ---------------------------------------------


async def test_adding_a_user_reloads_the_entry_and_adds_the_sensors(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, SCALE)
    await _setup(hass, entry)
    before = _unique_ids(hass, entry)
    runtime = entry.runtime_data

    result = await _start(hass, entry)
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Alice", "expected_weight": 62.5}
    )
    await hass.async_block_till_done()

    assert entry.runtime_data is not runtime
    added = _unique_ids(hass, entry) - before
    (subentry_id,) = entry.subentries
    assert added == {
        f"{entry.entry_id}_{subentry_id}_weight",
        f"{entry.entry_id}_{subentry_id}_bmi",
    }
    await _unload(hass, entry)


# --- sub-devices and sensors -------------------------------------------------------------


async def test_a_scale_user_has_a_sub_device_with_weight_and_bmi(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(
        hass,
        SCALE,
        users=[_subentry("u1", "Alice", expected_weight=62.0, height_cm=170)],
    )
    await _setup(hass, entry)

    user_device = _device(hass, entry, identifier=(DOMAIN, f"{entry.entry_id}_u1"))
    assert user_device is not None
    assert user_device.name == "Alice"
    assert user_device.manufacturer == "Omron"
    assert _subentries_of(user_device, entry) == {"u1"}
    main = _device(hass, entry, connection=("bluetooth", ADDRESS))
    assert main is not None and main.id != user_device.id
    assert user_device.via_device_id == main.id

    registry = er.async_get(hass)
    weight = registry.async_get(
        registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_u1_weight")
    )
    assert weight.device_id == user_device.id
    assert weight.config_subentry_id == "u1"
    assert weight.translation_key == "user_weight"
    assert hass.states.get(weight.entity_id).state == "unknown"

    # The scale's own entities are still there, on the scale's own device.
    own = er.async_entries_for_device(registry, main.id)
    assert any(e.translation_key == "bmi" for e in own)
    assert not any(e.config_subentry_id for e in own)
    await _unload(hass, entry)


async def test_a_cuff_user_has_the_four_reading_sensors(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF, users=[_subentry("u1", "Bob", sys_min=130)])
    await _setup(hass, entry)

    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_u1_"
    own = {
        e.unique_id.removeprefix(prefix): e
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.unique_id.startswith(prefix)
    }
    assert set(own) == {"systolic", "diastolic", "pulse", "measured_at"}
    assert own["systolic"].original_device_class is None
    assert own["measured_at"].original_device_class == "timestamp"
    await _unload(hass, entry)


async def test_each_user_gets_a_device_of_their_own(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(
        hass,
        CUFF,
        users=[
            _subentry("u1", "Bob", time_windows="06:00-09:00"),
            _subentry("u2", "Eve", time_windows="18:00-21:00"),
        ],
    )
    await _setup(hass, entry)

    ids = {
        _device(hass, entry, identifier=(DOMAIN, f"{entry.entry_id}_{sub}")).id
        for sub in ("u1", "u2")
    }
    assert len(ids) == 2
    await _unload(hass, entry)


async def test_without_users_nothing_changes(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict[str, Any]
) -> None:
    entry = _entry(hass, SCALE)
    await _setup(hass, entry)

    assert entry.runtime_data.users is None
    assert not any(
        e.config_subentry_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    )
    main = _device(hass, entry, connection=("bluetooth", ADDRESS))
    assert main.identifiers == set()
    assert not [key for key in hass_storage if key.startswith("omron.users_")]
    await _unload(hass, entry)


async def test_a_multi_slot_cuff_ignores_stray_user_subentries(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, MULTI_SLOT_CUFF, users=[_subentry("u1", "Bob")])
    await _setup(hass, entry)
    assert entry.runtime_data.users is None
    assert not any(
        e.config_subentry_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    )
    await _unload(hass, entry)


# --- the options ----------------------------------------------------------------------------


async def test_a_scale_offers_the_strategy_and_the_tolerance(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, SCALE)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = {str(key): key for key in result["data_schema"].schema}
    assert CONF_ASSIGNMENT_STRATEGY in schema
    assert CONF_WEIGHT_TOLERANCE_PCT in schema
    assert schema[CONF_ASSIGNMENT_STRATEGY].default() == STRATEGY_WEIGHT

    done = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: 300,
            CONF_ASSIGNMENT_STRATEGY: STRATEGY_RULES,
            CONF_WEIGHT_TOLERANCE_PCT: 4,
        },
    )
    assert done["type"] is FlowResultType.CREATE_ENTRY
    # Both survive the options flow's rebuilt dict.
    assert done["data"][CONF_ASSIGNMENT_STRATEGY] == STRATEGY_RULES
    assert done["data"][CONF_WEIGHT_TOLERANCE_PCT] == 4.0
    assert entry.options[CONF_ASSIGNMENT_STRATEGY] == STRATEGY_RULES


async def test_the_options_are_kept_when_saved_again(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(
        hass,
        SCALE,
        options={
            CONF_SCAN_INTERVAL: 300,
            CONF_ASSIGNMENT_STRATEGY: STRATEGY_RULES,
            CONF_WEIGHT_TOLERANCE_PCT: 4.5,
        },
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = {str(key): key for key in result["data_schema"].schema}
    # The form shows what is saved, so submitting it as is changes nothing.
    assert schema[CONF_ASSIGNMENT_STRATEGY].default() == STRATEGY_RULES
    assert schema[CONF_WEIGHT_TOLERANCE_PCT].default() == 4.5
    done = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 300}
    )
    assert done["data"][CONF_ASSIGNMENT_STRATEGY] == STRATEGY_RULES
    assert done["data"][CONF_WEIGHT_TOLERANCE_PCT] == 4.5


async def test_the_tolerance_is_limited_to_one_to_five_percent(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, SCALE)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    for pct in (0.5, 6):
        with pytest.raises(vol.Invalid):
            await hass.config_entries.options.async_configure(
                result["flow_id"],
                {CONF_SCAN_INTERVAL: 300, CONF_WEIGHT_TOLERANCE_PCT: pct},
            )


@pytest.mark.parametrize("model", [CUFF, MULTI_SLOT_CUFF])
async def test_a_cuff_has_no_strategy_to_choose(
    hass: HomeAssistant, enable_bluetooth: None, model: str
) -> None:
    entry = _entry(hass, model)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    names = {str(key) for key in result["data_schema"].schema}
    assert CONF_ASSIGNMENT_STRATEGY not in names
    assert CONF_WEIGHT_TOLERANCE_PCT not in names
    done = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 300}
    )
    assert CONF_ASSIGNMENT_STRATEGY not in done["data"]
