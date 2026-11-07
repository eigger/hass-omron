"""A user's entities follow the user when the alias changes."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.omron.alias_migration import async_migrate_alias_unique_ids
from custom_components.omron.const import CONF_DEVICE_MODEL, DOMAIN

ADDRESS = "AA:BB:CC:DD:EE:FF"
PREFIX = "hem_7155t_eeff_none_"


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        data={CONF_DEVICE_MODEL: "HEM-7155T"},
    )
    entry.add_to_hass(hass)
    return entry


def _add(
    hass: HomeAssistant, entry: MockConfigEntry, domain: str, unique_id: str
) -> str:
    return (
        er.async_get(hass)
        .async_get_or_create(
            domain, DOMAIN, unique_id, config_entry=entry, suggested_object_id=unique_id
        )
        .entity_id
    )


def _unique_id(hass: HomeAssistant, entity_id: str) -> str:
    entry = er.async_get(hass).async_get(entity_id)
    assert entry is not None
    return entry.unique_id


def test_a_renamed_user_keeps_sensors_and_flags_under_the_same_entity_ids(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    sensor = _add(hass, entry, "sensor", f"{PREFIX}blood_pressure_systolic_user1")
    flag = _add(hass, entry, "binary_sensor", f"{PREFIX}body_movement_user1")
    other_user = _add(hass, entry, "sensor", f"{PREFIX}blood_pressure_systolic_user2")
    rssi = _add(hass, entry, "sensor", f"{PREFIX}signal_strength")
    connection = _add(hass, entry, "binary_sensor", "hem_7155t_eeff_connection")

    moved = async_migrate_alias_unique_ids(hass, entry, {}, {1: "Dad"}, 2)

    assert moved == 2
    assert _unique_id(hass, sensor) == f"{PREFIX}blood_pressure_systolic_dad"
    assert _unique_id(hass, flag) == f"{PREFIX}body_movement_dad"
    assert _unique_id(hass, other_user) == f"{PREFIX}blood_pressure_systolic_user2"
    assert _unique_id(hass, rssi) == f"{PREFIX}signal_strength"
    assert _unique_id(hass, connection) == "hem_7155t_eeff_connection"


def test_two_users_can_swap_aliases(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    dad = _add(hass, entry, "sensor", f"{PREFIX}heart_rate_dad")
    mom = _add(hass, entry, "sensor", f"{PREFIX}heart_rate_mom")

    moved = async_migrate_alias_unique_ids(
        hass, entry, {1: "Dad", 2: "Mom"}, {1: "Mom", 2: "Dad"}, 2
    )

    assert moved == 2
    assert _unique_id(hass, dad) == f"{PREFIX}heart_rate_mom"
    assert _unique_id(hass, mom) == f"{PREFIX}heart_rate_dad"


def test_clearing_an_alias_goes_back_to_the_default_slot_name(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    sensor = _add(hass, entry, "sensor", f"{PREFIX}weight_dad")

    assert async_migrate_alias_unique_ids(hass, entry, {1: "Dad"}, {1: "user1"}, 2) == 1

    assert _unique_id(hass, sensor) == f"{PREFIX}weight_user1"


def test_unchanged_aliases_move_nothing(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    sensor = _add(hass, entry, "sensor", f"{PREFIX}weight_dad")

    assert async_migrate_alias_unique_ids(hass, entry, {1: "Dad"}, {1: "Dad"}, 2) == 0
    assert _unique_id(hass, sensor) == f"{PREFIX}weight_dad"


def test_an_entity_already_using_the_new_slug_is_left_alone(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    old = _add(hass, entry, "sensor", f"{PREFIX}weight_user1")
    taken = _add(hass, entry, "sensor", f"{PREFIX}weight_dad")

    moved = async_migrate_alias_unique_ids(hass, entry, {}, {1: "Dad"}, 2)

    assert moved == 0
    assert _unique_id(hass, old) == f"{PREFIX}weight_user1"
    assert _unique_id(hass, taken) == f"{PREFIX}weight_dad"


def test_entities_of_another_device_are_not_touched(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    other = _add(hass, entry, "sensor", "hem_7155t_aaaa_none_weight_user1")

    assert async_migrate_alias_unique_ids(hass, entry, {}, {1: "Dad"}, 2) == 0

    assert _unique_id(hass, other) == "hem_7155t_aaaa_none_weight_user1"


async def test_saving_new_aliases_in_the_options_flow_moves_the_entities(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass)
    sensor = _add(hass, entry, "sensor", f"{PREFIX}blood_pressure_systolic_user1")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    done = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "scan_interval": 0,
            "sync_stored_records": False,
            "user_alias_1": "Dad",
            "user_alias_2": "Mom",
        },
    )

    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert _unique_id(hass, sensor) == f"{PREFIX}blood_pressure_systolic_dad"


async def test_the_last_value_is_still_restored_under_the_new_alias(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        data={CONF_DEVICE_MODEL: "HEM-7155T"},
    )
    entry.add_to_hass(hass)
    sensor = _add(hass, entry, "sensor", f"{PREFIX}blood_pressure_systolic_user1")
    mock_restore_cache(hass, [State(sensor, "127")])

    async_migrate_alias_unique_ids(hass, entry, {}, {1: "Dad"}, 2)
    hass.config_entries.async_update_entry(
        entry, options={"user_aliases": {"1": "Dad", "2": "Mom"}}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(sensor).state == "127"
    uids = {
        e.unique_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }
    assert f"{PREFIX}blood_pressure_systolic_dad" in uids
    assert f"{PREFIX}blood_pressure_systolic_user1" not in uids
    await hass.config_entries.async_unload(entry.entry_id)


def test_a_skipped_move_does_not_leave_another_entity_half_moved(
    hass: HomeAssistant,
) -> None:
    """Dad -> Mom is blocked once Mom -> Kid is, so neither moves."""
    entry = _entry(hass)
    dad = _add(hass, entry, "sensor", f"{PREFIX}weight_dad")
    mom = _add(hass, entry, "sensor", f"{PREFIX}weight_mom")
    leftover = _add(hass, entry, "sensor", f"{PREFIX}weight_kid")

    moved = async_migrate_alias_unique_ids(
        hass, entry, {1: "Dad", 2: "Mom"}, {1: "Mom", 2: "Kid"}, 2
    )

    assert moved == 0
    assert _unique_id(hass, dad) == f"{PREFIX}weight_dad"
    assert _unique_id(hass, mom) == f"{PREFIX}weight_mom"
    assert _unique_id(hass, leftover) == f"{PREFIX}weight_kid"


def test_a_free_move_still_happens_beside_a_blocked_one(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    blocked = _add(hass, entry, "sensor", f"{PREFIX}weight_dad")
    leftover = _add(hass, entry, "sensor", f"{PREFIX}weight_grandpa")
    free = _add(hass, entry, "sensor", f"{PREFIX}heart_rate_mom")

    moved = async_migrate_alias_unique_ids(
        hass, entry, {1: "Dad", 2: "Mom"}, {1: "Grandpa", 2: "Nana"}, 2
    )

    assert moved == 1
    assert _unique_id(hass, blocked) == f"{PREFIX}weight_dad"
    assert _unique_id(hass, leftover) == f"{PREFIX}weight_grandpa"
    assert _unique_id(hass, free) == f"{PREFIX}heart_rate_nana"


async def test_a_second_alias_change_starts_from_the_saved_aliases(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        data={CONF_DEVICE_MODEL: "HEM-7155T"},
        options={"user_aliases": {"1": "Dad", "2": "Mom"}},
    )
    entry.add_to_hass(hass)
    sensor = _add(hass, entry, "sensor", f"{PREFIX}heart_rate_dad")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "scan_interval": 0,
            "sync_stored_records": False,
            "user_alias_1": "Grandpa",
            "user_alias_2": "Mom",
        },
    )

    assert _unique_id(hass, sensor) == f"{PREFIX}heart_rate_grandpa"


def test_only_this_integrations_measurement_entities_are_moved(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    registry = er.async_get(hass)
    uid = f"{PREFIX}weight_user1"
    foreign = registry.async_get_or_create(
        "sensor", "other", uid, config_entry=entry, suggested_object_id="foreign"
    ).entity_id
    other_domain = registry.async_get_or_create(
        "number", DOMAIN, uid, config_entry=entry, suggested_object_id="number"
    ).entity_id

    assert async_migrate_alias_unique_ids(hass, entry, {}, {1: "Dad"}, 2) == 0
    assert _unique_id(hass, foreign) == uid
    assert _unique_id(hass, other_domain) == uid


def test_an_entry_without_an_address_moves_nothing(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_DEVICE_MODEL: "HEM-7155T"})
    entry.add_to_hass(hass)
    sensor = _add(hass, entry, "sensor", f"{PREFIX}weight_user1")

    assert async_migrate_alias_unique_ids(hass, entry, {}, {1: "Dad"}, 2) == 0
    assert _unique_id(hass, sensor) == f"{PREFIX}weight_user1"


def test_a_user_is_never_split_across_old_and_new_entities(
    hass: HomeAssistant,
) -> None:
    """One blocked sensor keeps the whole user where it is."""
    entry = _entry(hass)
    weight = _add(hass, entry, "sensor", f"{PREFIX}weight_dad")
    body_fat = _add(hass, entry, "sensor", f"{PREFIX}body_fat_dad")
    leftover = _add(hass, entry, "sensor", f"{PREFIX}weight_grandpa")

    moved = async_migrate_alias_unique_ids(hass, entry, {1: "Dad"}, {1: "Grandpa"}, 4)

    assert moved == 0
    assert _unique_id(hass, weight) == f"{PREFIX}weight_dad"
    assert _unique_id(hass, body_fat) == f"{PREFIX}body_fat_dad"
    assert _unique_id(hass, leftover) == f"{PREFIX}weight_grandpa"


def test_four_users_can_rotate_aliases(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    names = ["a", "b", "c", "d"]
    ids = {n: _add(hass, entry, "sensor", f"{PREFIX}weight_{n}") for n in names}

    moved = async_migrate_alias_unique_ids(
        hass,
        entry,
        dict(enumerate(names, 1)),
        dict(enumerate(names[1:] + names[:1], 1)),
        4,
    )

    assert moved == 4
    for n, m in zip(names, names[1:] + names[:1]):
        assert _unique_id(hass, ids[n]) == f"{PREFIX}weight_{m}"
