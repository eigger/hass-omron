"""Moving a measurement to another user afterwards (omron.reassign_measurement)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.omron.assignment import Assignment
from custom_components.omron.const import (
    CONF_ASSIGNMENT_STRATEGY,
    CONF_SYNC_STORED_RECORDS,
    DOMAIN,
    EVENT_MEASUREMENT_REASSIGNED,
    EVENT_UNASSIGNED_MEASUREMENT,
    MEASUREMENT_LOG_SIZE,
    SERVICE_REASSIGN_MEASUREMENT,
    STRATEGY_RULES,
    STRATEGY_WEIGHT,
)
from custom_components.omron.entity import user_device_identifier
from custom_components.omron.history import async_entry_device
from custom_components.omron.users import OmronUsers, _load_log

from .user_harness import (
    add_entry,
    bp_event,
    poll,
    scale_event,
    sensor_state,
    setup_entry,
    stored,
    user,
)

ALICE_BOB = [
    user("alice", "Alice", expected_weight=62.0, height_cm=170),
    user("bob", "Bob", expected_weight=80.0, height_cm=180),
]


# --- the rebuild, without Home Assistant ---------------------------------------------------


def _entry(
    entry_id: str, user_id: str | None, weight: float, *, learned: bool = True
) -> dict[str, Any]:
    return {
        "id": entry_id,
        "user": user_id,
        "reason": None if user_id else "ambiguous",
        "kind": "matched" if user_id else None,
        "learned": learned and user_id is not None,
        "slot": 1,
        "measured_at": f"2026-09-2{entry_id[-1]}T07:00:00+00:00",
        "device_time": f"2026-09-2{entry_id[-1]}T07:00:00",
        "sequence": None,
        "values": {"weight": weight},
    }


def _users(strategy: str = STRATEGY_WEIGHT) -> OmronUsers:
    users = OmronUsers(MagicMock(), "e1", strategy)
    users.log = [
        _entry("m1", "alice", 60.0),
        _entry("m2", "alice", 60.5),
        _entry("m3", "bob", 80.0),
    ]
    # Alice's list has two weights older than the log, which the log cannot give.
    users.recent = {"alice": [58.0, 59.0, 60.0, 60.5], "bob": [79.0, 80.0]}
    users.last = {
        "alice": {"weight": 60.5, "assignment": "matched", "measurement_id": "m2"},
        "bob": {"weight": 80.0, "assignment": "matched", "measurement_id": "m3"},
    }
    return users


def test_moving_a_measurement_rebuilds_both_users_weights_from_the_log() -> None:
    users = _users()
    affected = users.reassign(users.find_logged("m2"), "bob")
    assert affected == {"alice", "bob"}
    # The two older weights stay; the log's own part is rebuilt in log order.
    assert users.recent["alice"] == [58.0, 59.0, 60.0]
    assert users.recent["bob"] == [79.0, 60.5, 80.0]
    entry = users.find_logged("m2")
    assert entry["user"] == "bob" and entry["kind"] == "manual" and entry["learned"]
    assert entry["reason"] is None


def test_the_old_users_sensors_fall_back_to_their_newest_remaining_measurement() -> (
    None
):
    users = _users()
    users.reassign(users.find_logged("m2"), "bob")
    assert users.last["alice"]["weight"] == 60.0
    assert users.last["alice"]["measurement_id"] == "m1"
    # Bob's newest logged measurement is still m3, so Bob's sensors stay.
    assert users.last["bob"]["measurement_id"] == "m3"


def test_the_new_users_sensors_show_it_when_it_is_their_newest() -> None:
    users = _users()
    users.reassign(users.find_logged("m1"), "bob")
    assert users.last["bob"]["measurement_id"] == "m3"  # m3 is newer than m1
    assert users.last["alice"]["measurement_id"] == "m2"  # her newest is unchanged
    users = _users()
    users.log.append(_entry("m4", "alice", 61.0))
    users.last["alice"]["measurement_id"] = "m4"
    users.reassign(users.find_logged("m4"), "bob")
    assert users.last["bob"] == {
        "weight": 61.0,
        "measured_at": "2026-09-24T07:00:00+00:00",
        "assignment": "manual",
        "measurement_id": "m4",
    }
    assert users.last["alice"]["measurement_id"] == "m2"


def test_unassigning_the_only_measurement_leaves_the_sensors_unknown() -> None:
    users = _users()
    users.log = [_entry("m1", "alice", 60.0)]
    users.recent = {"alice": [60.0]}
    users.last = {"alice": {"weight": 60.0, "measurement_id": "m1"}}
    users.reassign(users.find_logged("m1"), None)
    assert "alice" not in users.last
    assert users.recent["alice"] == []
    entry = users.find_logged("m1")
    assert entry["user"] is None and entry["reason"] == "manual"
    assert entry["kind"] is None and not entry["learned"]


def test_a_measurement_nobody_got_can_be_given_to_a_user() -> None:
    users = _users()
    users.log.insert(0, _entry("m0", None, 70.0))
    users.reassign(users.find_logged("m0"), "alice")
    # m0 is the oldest in the log, so it takes the first place of the log part.
    assert users.recent["alice"] == [58.0, 59.0, 70.0, 60.0, 60.5][-5:]
    assert users.find_logged("m0")["learned"]
    # And Alice's sensors keep showing her newest.
    assert users.last["alice"]["measurement_id"] == "m2"


def test_the_weights_are_only_touched_under_the_weight_strategy() -> None:
    users = _users(STRATEGY_RULES)
    users.log = [_entry("m1", "alice", 60.0, learned=False)]
    users.recent = {}
    users.reassign(users.find_logged("m1"), "bob")
    assert users.recent == {}
    assert not users.find_logged("m1")["learned"]
    assert users.last["bob"]["measurement_id"] == "m1"


def test_the_log_keeps_the_newest_fifty() -> None:
    users = _users()
    users.log = []
    for number in range(MEASUREMENT_LOG_SIZE + 5):
        users.log_measurement(
            {"weight": 60.0 + number, "measured_at": None},
            1,
            Assignment("alice", kind="matched"),
            ("weight",),
            learned=True,
        )
    assert len(users.log) == MEASUREMENT_LOG_SIZE
    assert users.log[0]["values"]["weight"] == 65.0
    assert len({e["id"] for e in users.log}) == MEASUREMENT_LOG_SIZE


def test_a_log_is_loaded_tolerantly_and_forgets_removed_users_and_old_learning() -> (
    None
):
    raw = [
        _entry("m1", "alice", 60.0),
        _entry("m2", "gone", 61.0),
        {"id": 5},
        "x",
    ]
    log = _load_log(raw, {"alice"}, same_strategy=True)
    assert [e["id"] for e in log] == ["m1", "m2"]
    assert log[1]["user"] is None and log[1]["reason"] == "user_removed"
    assert not log[1]["learned"] and log[0]["learned"]
    assert not _load_log(raw, {"alice"}, same_strategy=False)[0]["learned"]
    assert _load_log(None, set(), True) == []


# --- through Home Assistant --------------------------------------------------------------------


def _device_of(hass: HomeAssistant, entry: MockConfigEntry, user_id: str) -> str:
    registry = dr.async_get(hass)
    identifier = user_device_identifier(entry.entry_id, user_id)
    if hasattr(registry, "async_get_device_by_identifier"):
        device = registry.async_get_device_by_identifier(identifier, entry.entry_id)
    else:
        device = registry.async_get_device(identifiers={identifier})
    assert device is not None
    return device.id


async def _call(hass: HomeAssistant, **data: Any) -> None:
    await hass.services.async_call(
        DOMAIN, SERVICE_REASSIGN_MEASUREMENT, data, blocking=True
    )
    await hass.async_block_till_done()


async def _two_weights(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Baseline, then Alice's 62.4 and Bob's 80.6."""
    await poll(hass, entry, scale_event("2026-09-20T07:00:00", 70.0))
    await poll(hass, entry, scale_event("2026-09-21T07:30:00", 62.4))
    await poll(hass, entry, scale_event("2026-09-22T19:00:00", 80.6))


def _attr(hass: HomeAssistant, entry: MockConfigEntry, who: str, name: str) -> Any:
    return sensor_state(hass, entry, who, "weight").attributes[name]


async def test_the_service_is_registered_once_for_the_domain(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, "HN-300T2", ALICE_BOB)
    await setup_entry(hass, entry)
    assert hass.services.has_service(DOMAIN, SERVICE_REASSIGN_MEASUREMENT)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert hass.services.has_service(DOMAIN, SERVICE_REASSIGN_MEASUREMENT)


async def test_the_id_is_in_the_sensor_attributes_and_the_unassigned_event(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    unassigned = async_capture_events(hass, EVENT_UNASSIGNED_MEASUREMENT)
    entry = add_entry(hass, "HN-300T2", ALICE_BOB)
    await setup_entry(hass, entry)
    await _two_weights(hass, entry)
    await poll(hass, entry, scale_event("2026-09-23T07:00:00", 71.0))  # in between

    alice_id = _attr(hass, entry, "alice", "measurement_id")
    bob_id = _attr(hass, entry, "bob", "measurement_id")
    assert alice_id and bob_id and alice_id != bob_id
    (event,) = unassigned
    assert event.data["measurement_id"] not in (alice_id, bob_id)
    logged = {e["id"]: e for e in entry.runtime_data.users.log}
    assert logged[event.data["measurement_id"]]["user"] is None
    assert logged[event.data["measurement_id"]]["reason"] == "out_of_range"
    assert logged[alice_id]["user"] == "alice"


async def test_a_measurement_moves_to_another_user_and_both_sensors_follow(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    moved = async_capture_events(hass, EVENT_MEASUREMENT_REASSIGNED)
    entry = add_entry(hass, "HN-300T2", ALICE_BOB)
    await setup_entry(hass, entry)
    await _two_weights(hass, entry)
    bob_id = _attr(hass, entry, "bob", "measurement_id")

    await _call(
        hass,
        measurement_id=bob_id,
        user=_device_of(hass, entry, "alice"),
    )

    # Alice's newest measurement is now the 80.6 (taken later than her 62.4).
    weight = sensor_state(hass, entry, "alice", "weight")
    assert weight.state == "80.6"
    assert weight.attributes["assignment"] == "manual"
    assert weight.attributes["measurement_id"] == bob_id
    assert weight.attributes["measured_at"] == "2026-09-22T19:00:00+00:00"
    # Bob had nothing else: unknown.
    assert sensor_state(hass, entry, "bob", "weight").state == "unknown"
    assert sensor_state(hass, entry, "bob", "bmi").state == "unknown"
    users = entry.runtime_data.users
    assert users.recent["alice"] == [62.4, 80.6] and users.recent["bob"] == []
    saved = stored(hass_storage, entry)
    assert saved["users"]["alice"]["recent"] == [62.4, 80.6]
    assert [e["user"] for e in saved["log"]] == ["alice", "alice"]
    (event,) = moved
    assert event.data["measurement_id"] == bob_id
    assert (
        event.data["previous_user_id"] == "bob" and event.data["previous_user"] == "Bob"
    )
    assert event.data["user_id"] == "alice" and event.data["user"] == "Alice"
    assert event.data["weight"] == 80.6
    assert event.data["measured_at"] == "2026-09-22T19:00:00+00:00"


async def test_a_measurement_can_be_taken_back_from_everyone(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    moved = async_capture_events(hass, EVENT_MEASUREMENT_REASSIGNED)
    entry = add_entry(hass, "HN-300T2", ALICE_BOB)
    await setup_entry(hass, entry)
    await _two_weights(hass, entry)
    alice_id = _attr(hass, entry, "alice", "measurement_id")

    await _call(hass, measurement_id=alice_id)  # no user: nobody

    assert sensor_state(hass, entry, "alice", "weight").state == "unknown"
    assert entry.runtime_data.users.recent["alice"] == []
    assert moved[0].data["user_id"] is None and moved[0].data["user"] is None
    entry_log = entry.runtime_data.users.find_logged(alice_id)
    assert entry_log["user"] is None and entry_log["reason"] == "manual"
    # An empty user is the same as none.
    bob_id = _attr(hass, entry, "bob", "measurement_id")
    await _call(hass, measurement_id=bob_id, user="")
    assert sensor_state(hass, entry, "bob", "weight").state == "unknown"


async def test_the_old_user_falls_back_to_the_measurement_before(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, "HN-300T2", ALICE_BOB)
    await setup_entry(hass, entry)
    await poll(hass, entry, scale_event("2026-09-20T07:00:00", 70.0))
    await poll(hass, entry, scale_event("2026-09-21T07:30:00", 62.4))
    await poll(hass, entry, scale_event("2026-09-22T07:30:00", 63.0))
    newest = _attr(hass, entry, "alice", "measurement_id")
    assert sensor_state(hass, entry, "alice", "weight").state == "63.0"

    await _call(hass, measurement_id=newest, user=_device_of(hass, entry, "bob"))

    assert sensor_state(hass, entry, "alice", "weight").state == "62.4"
    assert sensor_state(hass, entry, "bob", "weight").state == "63.0"
    assert entry.runtime_data.users.recent == {"alice": [62.4], "bob": [63.0]}


async def test_the_learned_weights_decide_the_next_assignment(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, "HN-300T2", [ALICE_BOB[0]])
    await setup_entry(hass, entry)
    await poll(hass, entry, scale_event("2026-09-20T07:00:00", 1.0))
    for day, kg in enumerate((63.9, 65.5), start=21):
        await poll(hass, entry, scale_event(f"2026-09-{day}T07:00:00", kg))
    assert entry.runtime_data.users.recent["alice"] == [63.9, 65.5]
    newest = _attr(hass, entry, "alice", "measurement_id")
    await _call(hass, measurement_id=newest)
    # With the 65.5 taken back her reference is 63.9 again, not the drifted one.
    assert entry.runtime_data.users.recent["alice"] == [63.9]


async def test_an_unknown_id_is_refused(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, "HN-300T2", ALICE_BOB)
    await setup_entry(hass, entry)
    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, measurement_id="nope")
    assert raised.value.translation_key == "unknown_measurement"
    assert raised.value.translation_placeholders == {"measurement_id": "nope"}


async def test_a_user_of_another_device_or_no_user_is_refused(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, "HN-300T2", ALICE_BOB)
    other = MockConfigEntry(
        domain=DOMAIN,
        unique_id="11:22:33:44:55:66",
        title="HEM-7600T 5566",
        data={"device_model": "HEM-7600T"},
        subentries_data=[user("carol", "Carol", time_windows="06:00-09:00")],
    )
    other.add_to_hass(hass)
    await setup_entry(hass, entry)
    assert other.state.value == "loaded"
    await _two_weights(hass, entry)
    alice_id = _attr(hass, entry, "alice", "measurement_id")

    with pytest.raises(ServiceValidationError) as raised:
        await _call(
            hass, measurement_id=alice_id, user=_device_of(hass, other, "carol")
        )
    assert raised.value.translation_key == "user_other_device"

    main = async_entry_device(hass, entry)
    assert main is not None
    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, measurement_id=alice_id, user=main.id)
    assert raised.value.translation_key == "not_a_user"
    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, measurement_id=alice_id, user="no-such-device")
    assert raised.value.translation_key == "not_a_user"
    # Nothing moved.
    assert sensor_state(hass, entry, "alice", "weight").state == "62.4"


async def test_moving_to_the_same_user_does_nothing(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    moved = async_capture_events(hass, EVENT_MEASUREMENT_REASSIGNED)
    entry = add_entry(hass, "HN-300T2", ALICE_BOB)
    await setup_entry(hass, entry)
    await _two_weights(hass, entry)
    alice_id = _attr(hass, entry, "alice", "measurement_id")
    await _call(hass, measurement_id=alice_id, user=_device_of(hass, entry, "alice"))
    assert not moved
    assert _attr(hass, entry, "alice", "assignment") == "matched"


async def test_the_log_survives_a_reload_and_a_reassignment_after_it(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = add_entry(hass, "HN-300T2", ALICE_BOB)
    await setup_entry(hass, entry)
    await _two_weights(hass, entry)
    bob_id = _attr(hass, entry, "bob", "measurement_id")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert _attr(hass, entry, "bob", "measurement_id") == bob_id
    assert len(entry.runtime_data.users.log) == 2
    await _call(hass, measurement_id=bob_id, user=_device_of(hass, entry, "alice"))
    assert sensor_state(hass, entry, "alice", "weight").state == "80.6"


async def test_the_log_holds_fifty_across_one_big_sync(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(
        hass,
        "HN-300T2",
        [ALICE_BOB[0]],
        {CONF_SYNC_STORED_RECORDS: True},
    )
    await setup_entry(hass, entry)
    await poll(
        hass,
        entry,
        scale_event("2026-09-01T07:00:00", 62.0, seq=100),
        events=[],
        next_seq=100,
    )
    events = [
        scale_event(f"2026-09-02T07:{minute:02d}:00", 62.0, seq=101 + minute)
        for minute in range(55)
    ]
    await poll(hass, entry, events[-1], events=events, next_seq=155)
    log = entry.runtime_data.users.log
    assert len(log) == MEASUREMENT_LOG_SIZE
    assert log[-1]["sequence"] == 155 and log[0]["sequence"] == 106


async def test_a_blood_pressure_measurement_can_be_moved_too(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    users = [
        user("bob", "Bob", time_windows="06:00-09:00"),
        user("eve", "Eve", time_windows="18:00-21:00"),
    ]
    entry = add_entry(hass, "HEM-7600T", users)
    await setup_entry(hass, entry)
    await poll(hass, entry, bp_event("2026-09-20T07:00:00"))
    await poll(hass, entry, bp_event("2026-09-21T07:30:00", 131, 84))
    state = sensor_state(hass, entry, "bob", "systolic")
    assert state.state == "131"
    await _call(
        hass,
        measurement_id=state.attributes["measurement_id"],
        user=_device_of(hass, entry, "eve"),
    )
    assert sensor_state(hass, entry, "bob", "systolic").state == "unknown"
    eve = sensor_state(hass, entry, "eve", "systolic")
    assert eve.state == "131" and eve.attributes["assignment"] == "manual"
    assert eve.attributes["measured_at"] == "2026-09-21T07:30:00+00:00"


async def test_changing_the_strategy_stops_the_old_learning_from_coming_back(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, "HN-300T2", ALICE_BOB)
    await setup_entry(hass, entry)
    await _two_weights(hass, entry)
    hass.config_entries.async_update_entry(
        entry, options={CONF_ASSIGNMENT_STRATEGY: STRATEGY_RULES}
    )
    await hass.async_block_till_done()
    assert entry.runtime_data.users.recent == {}
    assert not any(e["learned"] for e in entry.runtime_data.users.log)
