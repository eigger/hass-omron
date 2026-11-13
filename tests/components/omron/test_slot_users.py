"""Users of a multi-slot body-composition scale: each one names a device slot.

The scale itself knows whose measurement it is, so there is nothing to guess:
a record of a slot goes to the user who claimed it, and a slot nobody claimed
is left alone. Both input modes are covered. Nothing here ran on a scale.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from unittest.mock import AsyncMock, MagicMock

from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.omron.assignment import (
    KIND_SLOT,
    REASON_AMBIGUOUS,
    REASON_SLOT_UNCLAIMED,
    SlotUser,
    assign_by_slot,
)
from custom_components.omron.const import (
    CONF_DEVICE_MODEL,
    CONF_SYNC_STORED_RECORDS,
    EVENT_UNASSIGNED_MEASUREMENT,
    STRATEGY_SLOT,
    SUBENTRY_USER,
)
from custom_components.omron.omron_ble import OmronBluetoothDeviceData
from custom_components.omron.users import (
    OmronUsers,
    strategy_for,
    supports_users,
)

from .user_harness import (
    ADDRESS,
    add_entry,
    poll,
    scale_event,
    sensor_state,
    setup_entry,
    stored,
    user,
)

HBF = "HBF-222T"
BODY = {
    "body_fat": 22.5,
    "skeletal_muscle": 31.0,
    "visceral_fat": 7,
    "basal_metabolic_rate": 1500,
    "bmi": 21.7,
}
ALICE_BOB = [
    user("alice", "Alice", slot=1, height_cm=170),
    user("bob", "Bob", slot=2, height_cm=180),
]
SYNC = {CONF_SYNC_STORED_RECORDS: True}


# --- the strategy -----------------------------------------------------------------------


def test_a_claimed_slot_goes_to_its_user() -> None:
    users = [SlotUser("a", "Alice", 1), SlotUser("b", "Bob", 3)]
    assignment = assign_by_slot(3, users)
    assert assignment.user_id == "b" and assignment.kind == KIND_SLOT


def test_a_slot_nobody_claimed_is_left_alone() -> None:
    assignment = assign_by_slot(2, [SlotUser("a", "Alice", 1)])
    assert assignment.user_id is None
    assert assignment.reason == REASON_SLOT_UNCLAIMED
    assert assign_by_slot(1, []).reason == REASON_SLOT_UNCLAIMED


def test_two_users_on_one_slot_are_given_nobody() -> None:
    users = [SlotUser("a", "Alice", 1), SlotUser("b", "Bob", 1)]
    assignment = assign_by_slot(1, users)
    assert assignment.user_id is None and assignment.reason == REASON_AMBIGUOUS
    assert [c["user_id"] for c in assignment.candidates] == ["a", "b"]


def test_a_body_composition_scale_has_the_slot_strategy_and_only_that() -> None:
    entry = MagicMock()
    entry.data = {CONF_DEVICE_MODEL: HBF}
    entry.options = {"assignment_strategy": "weight"}
    assert supports_users(entry)
    assert strategy_for(entry, is_scale=True) == STRATEGY_SLOT


# --- the subentry flow -------------------------------------------------------------------


async def _start(hass: HomeAssistant, entry: MockConfigEntry, source: str = "user"):
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_USER), context={"source": source}
    )


async def test_the_form_asks_for_a_name_a_slot_and_a_height(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, HBF, [])
    await setup_entry(hass, entry)

    result = await _start(hass, entry)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "slot"
    fields = {str(key): key for key in result["data_schema"].schema}
    assert set(fields) == {"name", "slot", "height_cm"}
    selector = result["data_schema"].schema[fields["slot"]]
    assert selector.config["options"] == ["1", "2", "3", "4"]

    created = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Alice", "slot": "3", "height_cm": 168}
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    (subentry,) = entry.subentries.values()
    assert dict(subentry.data) == {"name": "Alice", "slot": 3, "height_cm": 168}
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_slot_can_have_one_user_only(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, HBF, [user("alice", "Alice", slot=1)])
    await setup_entry(hass, entry)

    result = await _start(hass, entry)
    refused = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Bob", "slot": "1"}
    )
    assert refused["type"] is FlowResultType.FORM
    assert refused["errors"] == {"slot": "slot_taken"}
    accepted = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Bob", "slot": "2"}
    )
    assert accepted["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_editing_a_user_keeps_its_own_slot_free(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, HBF, [user("alice", "Alice", slot=1, height_cm=170)])
    await setup_entry(hass, entry)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_USER),
        context={"source": "reconfigure", "subentry_id": "alice"},
    )
    # The stored number is offered as the selector's text.
    key = next(k for k in result["data_schema"].schema if str(k) == "slot")
    assert key.description["suggested_value"] == "1"
    done = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Alice", "slot": "1", "height_cm": 171}
    )
    assert done["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()
    assert dict(entry.subentries["alice"].data)["height_cm"] == 171
    assert entry.subentries["alice"].data["slot"] == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_options_of_a_body_composition_scale_have_no_strategy(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, HBF, [])
    form = await hass.config_entries.options.async_init(entry.entry_id)
    names = {str(k) for k in form["data_schema"].schema}
    assert "assignment_strategy" not in names
    assert "weight_tolerance_pct" not in names
    assert CONF_SYNC_STORED_RECORDS in names
    done = await hass.config_entries.options.async_configure(
        form["flow_id"],
        user_input={
            CONF_SCAN_INTERVAL: 300,
            "user_alias_1": "a",
            "user_alias_2": "b",
            "user_alias_3": "c",
            "user_alias_4": "d",
        },
    )
    assert "assignment_strategy" not in done["data"]


# --- the sensors -----------------------------------------------------------------------


async def test_each_user_gets_the_weight_bmi_and_body_composition_sensors(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, HBF, ALICE_BOB)
    await setup_entry(hass, entry)
    for key in (
        "weight",
        "bmi",
        "body_fat",
        "skeletal_muscle",
        "visceral_fat",
        "basal_metabolic_rate",
        "body_composition_bmi",
    ):
        assert sensor_state(hass, entry, "alice", key).state == "unknown", key
        assert sensor_state(hass, entry, "bob", key).state == "unknown", key


def _records(*slots: int, weight: float = 60.0, time: str = "2026-09-20T07:30:00"):
    return {slot: scale_event(time, weight + slot, slot=slot, **BODY) for slot in slots}


async def test_latest_per_slot_the_first_poll_is_a_baseline_for_every_slot(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = add_entry(hass, HBF, ALICE_BOB)
    await setup_entry(hass, entry)

    await poll(hass, entry, records=_records(1, 2, 3))

    assert sensor_state(hass, entry, "alice", "weight").state == "unknown"
    data = stored(hass_storage, entry)
    assert set(data["fingerprints"]) == {"1", "2", "3"}
    assert data["last_seq"] == {}
    assert data["strategy"] == STRATEGY_SLOT


async def test_latest_per_slot_a_new_record_goes_to_the_user_of_its_slot(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    unassigned = async_capture_events(hass, EVENT_UNASSIGNED_MEASUREMENT)
    entry = add_entry(hass, HBF, ALICE_BOB)
    await setup_entry(hass, entry)
    await poll(hass, entry, records=_records(1, 2, 3))

    newer = _records(1, 2, 3, weight=70.0, time="2026-09-21T07:30:00")
    # Slot 1 and 3 have something new; slot 2 does not.
    newer[2] = _records(2)[2]
    await poll(hass, entry, records=newer)

    weight = sensor_state(hass, entry, "alice", "weight")
    assert weight.state == "71.0"
    assert weight.attributes["assignment"] == "slot"
    assert weight.attributes["measured_at"] == "2026-09-21T07:30:00+00:00"
    assert sensor_state(hass, entry, "alice", "body_fat").state == "22.5"
    assert sensor_state(hass, entry, "alice", "skeletal_muscle").state == "31.0"
    assert sensor_state(hass, entry, "alice", "visceral_fat").state == "7.0"
    assert sensor_state(hass, entry, "alice", "basal_metabolic_rate").state == "1500.0"
    assert sensor_state(hass, entry, "alice", "body_composition_bmi").state == "21.7"
    bmi = sensor_state(hass, entry, "alice", "bmi")
    assert bmi.state == "24.6"
    assert bmi.attributes["height_cm"] == 170
    # Bob's slot did not change; slot 3 nobody claimed: no event, no sensor.
    assert sensor_state(hass, entry, "bob", "weight").state == "unknown"
    assert not unassigned
    # Fingerprints are per slot.
    fingerprints = stored(hass_storage, entry)["fingerprints"]
    assert fingerprints["1"] == ["2026-09-21T07:30:00", 71.0]
    assert fingerprints["2"] == ["2026-09-20T07:30:00", 62.0]
    assert fingerprints["3"] == ["2026-09-21T07:30:00", 73.0]


async def test_latest_per_slot_two_slots_in_one_poll_go_to_two_users(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, HBF, ALICE_BOB)
    await setup_entry(hass, entry)
    await poll(hass, entry, records=_records(1, 2))
    await poll(
        hass,
        entry,
        records=_records(1, 2, weight=80.0, time="2026-09-22T07:30:00"),
    )
    assert sensor_state(hass, entry, "alice", "weight").state == "81.0"
    assert sensor_state(hass, entry, "bob", "weight").state == "82.0"


async def test_a_slot_that_appears_later_has_its_own_baseline(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, HBF, ALICE_BOB)
    await setup_entry(hass, entry)
    await poll(hass, entry, records=_records(1))
    # Slot 2 shows up with an old record: that is its baseline, not a new one.
    await poll(hass, entry, records=_records(1, 2))
    assert sensor_state(hass, entry, "bob", "weight").state == "unknown"
    await poll(
        hass, entry, records=_records(1, 2, weight=70.0, time="2026-09-21T07:30:00")
    )
    assert sensor_state(hass, entry, "bob", "weight").state == "72.0"


async def test_a_user_added_for_an_unclaimed_slot_gets_the_next_record_only(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, HBF, [ALICE_BOB[0]])
    await setup_entry(hass, entry)
    await poll(hass, entry, records=_records(1, 2))
    await poll(hass, entry, records=_records(1, 2, time="2026-09-20T07:30:00"))
    assert entry.runtime_data.users.fingerprints.keys() == {1, 2}
    assert "bob" not in entry.runtime_data.users.last


# --- sync on ------------------------------------------------------------------------------


async def test_synced_records_are_given_per_slot_by_each_slots_sequence(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    unassigned = async_capture_events(hass, EVENT_UNASSIGNED_MEASUREMENT)
    entry = add_entry(hass, HBF, ALICE_BOB, dict(SYNC))
    await setup_entry(hass, entry)
    await poll(
        hass,
        entry,
        records=_records(1, 2, 3),
        events=[],
        next_seq={1: 100, 2: 200, 3: 300},
        rebased={1: False, 2: False, 3: False},
    )
    assert stored(hass_storage, entry)["last_seq"] == {"1": 100, "2": 200, "3": 300}

    a1 = scale_event("2026-09-21T07:00:00", 61.0, seq=101, slot=1, **BODY)
    a2 = scale_event("2026-09-21T08:00:00", 61.4, seq=102, slot=1, **BODY)
    b1 = scale_event("2026-09-21T07:30:00", 81.0, seq=201, slot=2, **BODY)
    ghost = scale_event("2026-09-21T07:45:00", 55.0, seq=301, slot=3, **BODY)
    await poll(
        hass,
        entry,
        records={
            1: a2,
            2: b1,
            3: ghost,
        },
        events=[a2, ghost, b1, a1],
        next_seq={1: 102, 2: 201, 3: 301},
        rebased={1: False, 2: False, 3: False},
    )

    assert sensor_state(hass, entry, "alice", "weight").state == "61.4"
    assert sensor_state(hass, entry, "bob", "weight").state == "81.0"
    assert not unassigned
    assert stored(hass_storage, entry)["last_seq"] == {"1": 102, "2": 201, "3": 301}

    # A replayed event is not given twice.
    await poll(
        hass,
        entry,
        records={1: a2, 2: b1, 3: ghost},
        events=[a1, a2],
        next_seq={1: 102, 2: 201, 3: 301},
        rebased={1: False, 2: False, 3: False},
    )
    assert entry.runtime_data.users.last_seq == {1: 102, 2: 201, 3: 301}


async def test_a_rebase_of_one_slot_resets_only_that_slots_sequence(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, HBF, ALICE_BOB, dict(SYNC))
    await setup_entry(hass, entry)
    await poll(
        hass,
        entry,
        records=_records(1, 2),
        events=[],
        next_seq={1: 60000, 2: 500},
        rebased={1: False, 2: False},
    )
    await poll(
        hass,
        entry,
        records=_records(1, 2),
        events=[],
        next_seq={1: 3, 2: 500},
        rebased={1: True, 2: False},
    )
    assert entry.runtime_data.users.last_seq == {1: 3, 2: 500}
    after = scale_event("2026-09-22T07:00:00", 61.0, seq=4, slot=1, **BODY)
    await poll(
        hass,
        entry,
        records={1: after, 2: _records(2)[2]},
        events=[after],
        next_seq={1: 4, 2: 500},
        rebased={1: False, 2: False},
    )
    assert sensor_state(hass, entry, "alice", "weight").state == "61.0"


async def test_switching_the_sync_off_forgets_the_sequences_but_not_the_baselines(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = add_entry(hass, HBF, ALICE_BOB, dict(SYNC))
    await setup_entry(hass, entry)
    await poll(
        hass,
        entry,
        records=_records(1, 2),
        events=[],
        next_seq={1: 5, 2: 9},
        rebased={1: False, 2: False},
    )
    hass.config_entries.async_update_entry(
        entry, options={CONF_SCAN_INTERVAL: 300, CONF_SYNC_STORED_RECORDS: False}
    )
    await hass.async_block_till_done()
    await poll(hass, entry, records=_records(1, 2))
    data = stored(hass_storage, entry)
    assert data["last_seq"] == {}
    assert set(data["fingerprints"]) == {"1", "2"}
    assert sensor_state(hass, entry, "alice", "weight").state == "unknown"


# --- the parser keeps the newest record of each slot ---------------------------------------


def test_the_parser_exposes_the_newest_record_of_each_slot() -> None:
    data = OmronBluetoothDeviceData(device_model=HBF)
    latest = {
        slot: {
            "user": slot,
            "weight": 60.0 + slot,
            "datetime": dt.datetime(2026, 9, 20, 7, slot, 0),
            "body_fat": 20.0 + slot,
            "skeletal_muscle": 30.0,
            "visceral_fat": 5,
            "basal_metabolic_rate": 1400,
            "bmi": 21.0,
        }
        for slot in (1, 3)
    }
    data._driver.get_latest_records_per_user = AsyncMock(return_value=latest)
    data._async_sync_current_time_with_client = AsyncMock()
    data._driver.complete_measurement_readout = AsyncMock()
    from custom_components.omron.omron_ble.session_trace import SessionTrace

    session = MagicMock()
    session.trace = SessionTrace()
    session.memory.commit_pairing_registration = AsyncMock()
    client = MagicMock()
    client.services.get_characteristic.return_value = None
    asyncio.run(
        data._poll_device_readout(
            session,
            client,
            MagicMock(address=ADDRESS),
            memory_session_active=False,
        )
    )
    assert set(data.last_records) == {1, 3}
    third = data.last_records[3]
    assert third["user"] == 3 and third["sequence"] is None
    assert third["weight"] == 63.0 and third["body_fat"] == 23.0
    assert third["device_time"] == "2026-09-20T07:03:00"
    assert data.last_record is None


# --- the Store: version 1 -> 2 --------------------------------------------------------------


async def test_a_version_1_store_is_migrated_keeping_what_was_learned(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = add_entry(
        hass,
        "HN-300T2",
        [
            user("alice", "Alice", expected_weight=62.0, height_cm=170),
            user("bob", "Bob", expected_weight=80.0),
        ],
    )
    hass_storage[f"omron.users_{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"omron.users_{entry.entry_id}",
        "data": {
            "strategy": "weight",
            "fingerprint": ["2026-09-21T07:30:00", 62.4],
            "last_seq": 17,
            "users": {
                "alice": {
                    "recent": [62.0, 62.4],
                    "last": {
                        "weight": 62.4,
                        "measured_at": "2026-09-21T07:30:00+00:00",
                        "assignment": "matched",
                    },
                },
            },
        },
    }
    await setup_entry(hass, entry)

    users: OmronUsers = entry.runtime_data.users
    assert users.fingerprints == {1: ["2026-09-21T07:30:00", 62.4]}
    assert users.last_seq == {1: 17}
    assert users.recent == {"alice": [62.0, 62.4]}
    assert sensor_state(hass, entry, "alice", "weight").state == "62.4"

    # The next write is in the new format; the old keys are gone.
    await poll(hass, entry, scale_event("2026-09-22T07:30:00", 62.0))
    await hass.async_block_till_done()
    saved = hass_storage[f"omron.users_{entry.entry_id}"]
    assert saved["version"] == 2
    assert "fingerprint" not in saved["data"]
    assert saved["data"]["fingerprints"] == {"1": ["2026-09-22T07:30:00", 62.0]}
    assert saved["data"]["last_seq"] == {}
    assert saved["data"]["users"]["alice"]["recent"] == [62.0, 62.4, 62.0]


async def test_a_version_1_store_without_a_position_migrates_to_none(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = add_entry(
        hass, "HEM-7600T", [user("bob", "Bob", time_windows="06:00-09:00")]
    )
    hass_storage[f"omron.users_{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"omron.users_{entry.entry_id}",
        "data": {
            "strategy": "rules",
            "fingerprint": None,
            "last_seq": None,
            "users": {},
        },
    }
    await setup_entry(hass, entry)
    users: OmronUsers = entry.runtime_data.users
    assert users.fingerprints == {} and users.last_seq == {}
