"""Giving each new measurement to a user, through the real poll path (#301).

The three real combinations are covered: stored-record sync on with rules,
latest record only with rules (a cuff without a sequence counter), and latest
record only with weight (the scale). Only the BLE boundary is replaced.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.omron import async_poll_data
from custom_components.omron.const import (
    CONF_ASSIGNMENT_STRATEGY,
    CONF_DEVICE_MODEL,
    CONF_SYNC_STORED_RECORDS,
    CONF_WEIGHT_TOLERANCE_PCT,
    DOMAIN,
    EVENT_UNASSIGNED_MEASUREMENT,
    STRATEGY_RULES,
    SUBENTRY_USER,
)
from custom_components.omron.omron_ble.const import ExtendedSensorDeviceClass
from custom_components.omron.users import order_events, record_fingerprint

ADDRESS = "AA:BB:CC:DD:EE:FF"
SCALE = "HN-300T2"
CUFF = "HEM-7600T"
SYNC_CUFF = "HEM-7530T"


def _user(subentry_id: str, name: str, **data: Any) -> dict[str, Any]:
    return {
        "subentry_id": subentry_id,
        "subentry_type": SUBENTRY_USER,
        "title": name,
        "unique_id": None,
        "data": {"name": name, **data},
    }


MORNING_EVENING = [
    _user("bob", "Bob", time_windows="06:00-09:00"),
    _user("eve", "Eve", time_windows="18:00-21:00"),
]
ALICE_BOB = [
    _user("alice", "Alice", expected_weight=62.0, height_cm=170),
    _user("bob", "Bob", expected_weight=80.0, height_cm=180),
]


def _bp(
    time: str | None, sys: int = 120, dia: int = 80, seq: int | None = None
) -> dict:
    return {
        "user": 1,
        "user_alias": None,
        "sequence": seq,
        "measured_at": f"{time}+00:00" if time else None,
        "device_time": time,
        "sys": sys,
        "dia": dia,
        "bpm": 60,
        "irregular_pulse": False,
        "body_movement": True,
        "cuff_loose": False,
        "improper_position": False,
    }


def _scale(time: str | None, weight: float) -> dict:
    return {
        "user": 1,
        "user_alias": None,
        "sequence": None,
        "measured_at": f"{time}+00:00" if time else None,
        "device_time": time,
        "weight": weight,
        "body_fat": None,
        "skeletal_muscle": None,
        "visceral_fat": None,
        "basal_metabolic_rate": None,
        "bmi": None,
    }


def _entry(
    hass: HomeAssistant,
    model: str,
    users: list[dict[str, Any]],
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title=f"{model} EEFF",
        data={CONF_DEVICE_MODEL: model},
        options=options or {},
        subentries_data=users,
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _poll(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    record: dict | None,
    *,
    events: list[dict] | None = None,
    next_seq: int | None = None,
    rebased: bool = False,
) -> Any:
    """One poll that reads ``record`` (and the synced ``events``)."""
    data = entry.runtime_data.device_data

    async def poll(device, preconnected_session=None):
        data.last_record = record
        if record is not None and record.get("weight") is not None:
            # What the parser publishes for the device's own weight sensor.
            data.update_sensor(
                "weight",
                "kg",
                record["weight"],
                device_class=ExtendedSensorDeviceClass.WEIGHT,
                name="Weight",
            )
        if events is not None or rebased:
            data.pending_history = {
                "next_seq": {1: next_seq} if next_seq is not None else {},
                "events": events or [],
                "shown": {},
                "rebased": {1: rebased},
            }
        return data._finish_update()

    with (
        patch(
            "custom_components.omron.async_ble_device_from_address",
            return_value=BLEDevice(ADDRESS, "device", {}),
        ),
        patch.object(data, "async_poll", poll),
    ):
        result = await async_poll_data(hass, entry)
    await hass.async_block_till_done()
    return result


def _sensor(hass: HomeAssistant, entry: MockConfigEntry, user: str, key: str):
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{user}_{key}"
    )
    assert entity_id is not None, key
    return hass.states.get(entity_id)


def _stored(hass_storage: dict[str, Any], entry: MockConfigEntry) -> dict[str, Any]:
    return hass_storage[f"omron.users_{entry.entry_id}"]["data"]


@pytest.fixture
def unassigned(hass: HomeAssistant) -> list:
    return async_capture_events(hass, EVENT_UNASSIGNED_MEASUREMENT)


@pytest.fixture
def measurements(hass: HomeAssistant) -> list:
    return async_capture_events(hass, "omron_measurement")


# --- sequence order and the wrap ----------------------------------------------------


def _ev(seq: int) -> dict:
    return {"sequence": seq}


def test_events_come_out_in_sequence_order_not_clock_order() -> None:
    events = [_ev(7), _ev(5), _ev(6)]
    assert [e["sequence"] for e in order_events(events, None)] == [5, 6, 7]


def test_events_across_the_counter_wrap_stay_in_order() -> None:
    events = [_ev(2), _ev(0xFFFF), _ev(0), _ev(0xFFFE), _ev(1)]
    assert [e["sequence"] for e in order_events(events, None)] == [
        0xFFFE,
        0xFFFF,
        0,
        1,
        2,
    ]
    assert [e["sequence"] for e in order_events(events, 0xFFFF)] == [0, 1, 2]


def test_events_already_handled_are_dropped() -> None:
    events = [_ev(4), _ev(5), _ev(6)]
    assert [e["sequence"] for e in order_events(events, 5)] == [6]
    assert order_events(events, 6) == []
    # Far behind is "old" too: the counter only moves forward by less than half.
    assert order_events([_ev(40000)], 10) == []


def test_events_without_a_sequence_are_ignored() -> None:
    assert order_events([{"sequence": None}, {}], None) == []


def test_the_fingerprint_is_the_time_and_the_values() -> None:
    assert record_fingerprint(_bp("2026-09-20T07:30:00", 120, 80)) == [
        "2026-09-20T07:30:00",
        120,
        80,
        60,
    ]
    assert record_fingerprint(_scale("2026-09-20T07:30:00", 62.4)) == [
        "2026-09-20T07:30:00",
        62.4,
    ]
    assert record_fingerprint(_scale(None, 62.4)) == [None, 62.4]
    assert record_fingerprint(_scale("2026-09-20T07:30:00", None)) is None  # type: ignore[arg-type]


# --- latest record x weight (the scale) -------------------------------------------------


async def test_latest_one_by_weight_first_poll_only_sets_the_baseline(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict, unassigned: list
) -> None:
    entry = _entry(hass, SCALE, ALICE_BOB)
    await _setup(hass, entry)

    await _poll(hass, entry, _scale("2026-09-20T07:30:00", 62.4))

    assert _sensor(hass, entry, "alice", "weight").state == "unknown"
    assert not unassigned
    stored = _stored(hass_storage, entry)
    assert stored["fingerprint"] == ["2026-09-20T07:30:00", 62.4]
    assert stored["last_seq"] is None
    assert all(not user["recent"] for user in stored["users"].values())

    # The same record again is still not new.
    await _poll(hass, entry, _scale("2026-09-20T07:30:00", 62.4))
    assert _sensor(hass, entry, "alice", "weight").state == "unknown"


async def test_latest_one_by_weight_assigns_the_nearest_user(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict, unassigned: list
) -> None:
    entry = _entry(hass, SCALE, ALICE_BOB)
    await _setup(hass, entry)
    await _poll(hass, entry, _scale("2026-09-20T07:00:00", 70.0))  # baseline

    await _poll(hass, entry, _scale("2026-09-21T07:30:00", 62.4))

    weight = _sensor(hass, entry, "alice", "weight")
    assert weight.state == "62.4"
    assert weight.attributes["assignment"] == "matched"
    assert weight.attributes["measured_at"] == "2026-09-21T07:30:00+00:00"
    assert weight.attributes["unit_of_measurement"] == "kg"
    bmi = _sensor(hass, entry, "alice", "bmi")
    assert bmi.state == "21.6"
    assert bmi.attributes["height_cm"] == 170
    assert bmi.attributes["weight_kg"] == 62.4
    # Bob is untouched, and nothing was announced.
    assert _sensor(hass, entry, "bob", "weight").state == "unknown"
    assert not unassigned
    stored = _stored(hass_storage, entry)
    assert stored["users"]["alice"]["recent"] == [62.4]
    assert stored["fingerprint"] == ["2026-09-21T07:30:00", 62.4]

    await _poll(hass, entry, _scale("2026-09-22T19:00:00", 80.6))
    assert _sensor(hass, entry, "bob", "weight").state == "80.6"
    assert _sensor(hass, entry, "alice", "weight").state == "62.4"


async def test_latest_one_by_weight_learns_a_drifting_weight(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict, unassigned: list
) -> None:
    entry = _entry(hass, SCALE, [ALICE_BOB[0]])
    await _setup(hass, entry)
    await _poll(hass, entry, _scale("2026-09-01T07:00:00", 1.0))  # baseline
    # 62 -> 63.9 (inside), then 65.8 ... each within reach of the learned weight.
    for day, kg in enumerate((63.9, 65.5, 67.0), start=2):
        await _poll(hass, entry, _scale(f"2026-09-{day:02d}T07:00:00", kg))
    assert not unassigned
    assert _sensor(hass, entry, "alice", "weight").state == "67.0"
    assert _stored(hass_storage, entry)["users"]["alice"]["recent"] == [
        63.9,
        65.5,
        67.0,
    ]


async def test_a_guest_is_announced_and_changes_nobody(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict, unassigned: list
) -> None:
    entry = _entry(hass, SCALE, ALICE_BOB)
    await _setup(hass, entry)
    await _poll(hass, entry, _scale("2026-09-20T07:00:00", 70.0))  # baseline
    await _poll(hass, entry, _scale("2026-09-21T07:30:00", 62.4))

    await _poll(hass, entry, _scale("2026-09-22T07:30:00", 110.0))

    (event,) = unassigned
    assert event.data["reason"] == "out_of_range"
    assert event.data["weight"] == 110.0
    assert event.data["measured_at"] == "2026-09-22T07:30:00+00:00"
    assert event.data["address"] == ADDRESS
    assert event.data["device_id"] is not None
    assert {c["user_id"] for c in event.data["candidates"]} == {"alice", "bob"}
    alice = next(c for c in event.data["candidates"] if c["user_id"] == "alice")
    assert alice["difference_kg"] == pytest.approx(110.0 - 62.4)
    assert alice["reference_kg"] == pytest.approx(62.4)
    assert _sensor(hass, entry, "alice", "weight").state == "62.4"
    assert _stored(hass_storage, entry)["users"]["alice"]["recent"] == [62.4]
    # The unassigned one is the newest record all the same: not announced twice.
    await _poll(hass, entry, _scale("2026-09-22T07:30:00", 110.0))
    assert len(unassigned) == 1


async def test_the_scales_own_weight_still_follows_every_measurement(
    hass: HomeAssistant, enable_bluetooth: None, unassigned: list
) -> None:
    entry = _entry(hass, SCALE, ALICE_BOB)
    await _setup(hass, entry)
    runtime = entry.runtime_data
    await _poll(hass, entry, _scale("2026-09-20T07:00:00", 70.0))  # baseline
    result = await _poll(hass, entry, _scale("2026-09-21T07:00:00", 110.0))  # a guest

    # Assignment takes nothing from the device's own sensors.
    own = result.entity_values[runtime.weight_device_key]
    assert own.native_value == 110.0
    assert len(unassigned) == 1


# --- latest record x rules (a cuff without a counter) -----------------------------------


async def test_latest_one_by_rules_assigns_by_the_time_on_the_device(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict, unassigned: list
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    await _poll(hass, entry, _bp("2026-09-20T07:00:00"))  # baseline

    await _poll(hass, entry, _bp("2026-09-21T07:30:00", 131, 84))
    systolic = _sensor(hass, entry, "bob", "systolic")
    assert systolic.state == "131"
    assert systolic.attributes["assignment"] == "rule"
    assert systolic.attributes["irregular_pulse"] is False
    assert systolic.attributes["body_movement"] is True
    assert systolic.attributes["measured_at"] == "2026-09-21T07:30:00+00:00"
    assert _sensor(hass, entry, "bob", "diastolic").state == "84"
    assert _sensor(hass, entry, "bob", "pulse").state == "60"
    assert _sensor(hass, entry, "bob", "measured_at").state == (
        "2026-09-21T07:30:00+00:00"
    )
    assert _sensor(hass, entry, "eve", "systolic").state == "unknown"

    await _poll(hass, entry, _bp("2026-09-21T19:30:00", 118, 76))
    assert _sensor(hass, entry, "eve", "systolic").state == "118"
    assert _sensor(hass, entry, "bob", "systolic").state == "131"
    assert not unassigned
    # A rule-based strategy learns nothing about values.
    assert all(not u["recent"] for u in _stored(hass_storage, entry)["users"].values())


async def test_latest_one_by_rules_announces_why_it_could_not_decide(
    hass: HomeAssistant, enable_bluetooth: None, unassigned: list
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    await _poll(hass, entry, _bp("2026-09-20T07:00:00"))  # baseline

    await _poll(hass, entry, _bp("2026-09-21T13:00:00", 140, 90))
    await _poll(hass, entry, _bp(None, 141, 91))

    midday, untimed = unassigned
    assert midday.data["reason"] == "no_match"
    assert (midday.data["sys"], midday.data["dia"], midday.data["bpm"]) == (140, 90, 60)
    assert midday.data["device_time"] == "2026-09-21T13:00:00"
    assert [c["user_id"] for c in midday.data["candidates"]] == ["bob", "eve"]
    assert midday.data["candidates"][0]["failed"] == ["time"]
    assert untimed.data["reason"] == "no_time"
    assert untimed.data["measured_at"] is None
    assert _sensor(hass, entry, "bob", "systolic").state == "unknown"


async def test_an_unreadable_poll_leaves_the_baseline_alone(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict, unassigned: list
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    await _poll(hass, entry, _bp("2026-09-20T07:00:00"))  # baseline
    await _poll(hass, entry, _bp("2026-09-21T07:30:00", 131, 84))

    await _poll(hass, entry, None)

    stored = _stored(hass_storage, entry)
    assert stored["fingerprint"] == ["2026-09-21T07:30:00", 131, 84, 60]
    # And the same record read next time is still not new.
    await _poll(hass, entry, _bp("2026-09-21T07:30:00", 131, 84))
    assert _sensor(hass, entry, "bob", "systolic").state == "131"
    assert not unassigned


async def test_a_record_without_values_is_skipped(
    hass: HomeAssistant, enable_bluetooth: None, unassigned: list
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    await _poll(hass, entry, _bp("2026-09-20T07:00:00"))  # baseline

    empty = _bp("2026-09-21T07:30:00")
    empty.update(sys=None, dia=None, bpm=None)
    await _poll(hass, entry, empty)
    assert not unassigned
    assert _sensor(hass, entry, "bob", "systolic").state == "unknown"


# --- sync on x rules ---------------------------------------------------------------------


def _sync_options(**extra: Any) -> dict[str, Any]:
    return {CONF_SYNC_STORED_RECORDS: True, **extra}


async def test_synced_events_are_assigned_oldest_first_by_sequence(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict, unassigned: list
) -> None:
    entry = _entry(hass, SYNC_CUFF, MORNING_EVENING, _sync_options())
    await _setup(hass, entry)
    newest = _bp("2026-09-21T19:30:00", 118, 76, seq=7)
    await _poll(
        hass, entry, _bp("2026-09-20T07:00:00", seq=None), events=[], next_seq=5
    )  # baseline

    # The cuff clock is wrong here: by time the order is 7, 6; by sequence 6, 7.
    older = _bp("2026-09-21T07:30:00", 131, 84, seq=6)
    states: list[str] = []
    hass.bus.async_listen(
        "state_changed",
        lambda ev: (
            states.append(ev.data["new_state"].state)
            if ev.data["entity_id"] == _sensor(hass, entry, "bob", "systolic").entity_id
            else None
        ),
    )
    await _poll(hass, entry, newest, events=[newest, older], next_seq=7)

    assert _sensor(hass, entry, "bob", "systolic").state == "131"
    assert _sensor(hass, entry, "eve", "systolic").state == "118"
    stored = _stored(hass_storage, entry)
    assert stored["last_seq"] == 7
    assert stored["fingerprint"] == ["2026-09-21T19:30:00", 118, 76, 60]
    assert not unassigned


async def test_several_records_for_one_user_each_get_their_own_state(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, SYNC_CUFF, MORNING_EVENING, _sync_options())
    await _setup(hass, entry)
    await _poll(hass, entry, _bp("2026-09-20T07:00:00"), events=[], next_seq=5)
    entity_id = _sensor(hass, entry, "bob", "systolic").entity_id
    seen: list[str] = []
    hass.bus.async_listen(
        "state_changed",
        lambda ev: (
            seen.append(ev.data["new_state"].state)
            if ev.data["entity_id"] == entity_id
            else None
        ),
    )

    first = _bp("2026-09-21T07:00:00", 130, 80, seq=6)
    second = _bp("2026-09-21T08:00:00", 135, 85, seq=7)
    third = _bp("2026-09-21T08:30:00", 140, 90, seq=8)
    await _poll(hass, entry, third, events=[first, second, third], next_seq=8)

    assert seen == ["130", "135", "140"]


async def test_sync_mode_does_not_assign_the_latest_record_a_second_time(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, SYNC_CUFF, MORNING_EVENING, _sync_options())
    await _setup(hass, entry)
    await _poll(hass, entry, _bp("2026-09-20T07:00:00"), events=[], next_seq=5)

    newest = _bp("2026-09-21T07:30:00", 131, 84, seq=6)
    with patch(
        "custom_components.omron.users.assign_by_rules",
        wraps=__import__(
            "custom_components.omron.users", fromlist=["assign_by_rules"]
        ).assign_by_rules,
    ) as spy:
        # last_record is the same measurement as the event (sequence None).
        await _poll(
            hass, entry, {**newest, "sequence": None}, events=[newest], next_seq=6
        )
    assert spy.call_count == 1


async def test_a_replayed_event_is_not_assigned_twice(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = _entry(hass, SYNC_CUFF, MORNING_EVENING, _sync_options())
    await _setup(hass, entry)
    await _poll(hass, entry, _bp("2026-09-20T07:00:00"), events=[], next_seq=5)
    first = _bp("2026-09-21T07:30:00", 131, 84, seq=6)
    await _poll(hass, entry, first, events=[first], next_seq=6)

    # A restart between the two Stores replays the event from the cuff.
    later = _bp("2026-09-21T07:45:00", 150, 90, seq=7)
    replay = [first, later]
    await _poll(hass, entry, later, events=replay, next_seq=7)

    assert _sensor(hass, entry, "bob", "systolic").state == "150"
    assert _stored(hass_storage, entry)["last_seq"] == 7
    with patch("custom_components.omron.users.assign_by_rules") as spy:
        await _poll(hass, entry, later, events=replay, next_seq=7)
    spy.assert_not_called()


async def test_the_first_sync_poll_assigns_nothing(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict, unassigned: list
) -> None:
    entry = _entry(hass, SYNC_CUFF, MORNING_EVENING, _sync_options())
    await _setup(hass, entry)

    old = _bp("2026-09-20T07:30:00", 131, 84, seq=6)
    await _poll(hass, entry, old, events=[old], next_seq=6)

    assert _sensor(hass, entry, "bob", "systolic").state == "unknown"
    assert not unassigned
    stored = _stored(hass_storage, entry)
    assert stored["last_seq"] == 6
    assert stored["fingerprint"] is not None


async def test_a_rebase_resets_the_last_sequence(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = _entry(hass, SYNC_CUFF, MORNING_EVENING, _sync_options())
    await _setup(hass, entry)
    await _poll(hass, entry, _bp("2026-09-20T07:00:00"), events=[], next_seq=60000)
    assert _stored(hass_storage, entry)["last_seq"] == 60000

    # The cuff was cleared: its counter is back at 3 and the poll says so.
    await _poll(
        hass,
        entry,
        _bp("2026-09-21T06:00:00", 125, 82),
        events=[],
        next_seq=3,
        rebased=True,
    )
    assert _stored(hass_storage, entry)["last_seq"] == 3

    after = _bp("2026-09-21T07:30:00", 131, 84, seq=4)
    await _poll(hass, entry, after, events=[after], next_seq=4)
    assert _sensor(hass, entry, "bob", "systolic").state == "131"


async def test_a_failed_sync_read_assigns_nothing_and_keeps_the_baseline(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = _entry(hass, SYNC_CUFF, MORNING_EVENING, _sync_options())
    await _setup(hass, entry)
    await _poll(hass, entry, _bp("2026-09-20T07:00:00"), events=[], next_seq=5)
    before = dict(_stored(hass_storage, entry))

    ghost = _bp("2026-09-21T07:30:00", 131, 84, seq=6)
    await _poll(hass, entry, None, events=[ghost], next_seq=6)

    assert _sensor(hass, entry, "bob", "systolic").state == "unknown"
    assert _stored(hass_storage, entry) == before


# --- switching the sync on and off -------------------------------------------------------------


async def test_switching_the_sync_never_assigns_a_record_again(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = _entry(hass, SYNC_CUFF, MORNING_EVENING, _sync_options())
    await _setup(hass, entry)
    await _poll(hass, entry, _bp("2026-09-20T07:00:00"), events=[], next_seq=5)
    record = _bp("2026-09-21T07:30:00", 131, 84, seq=6)
    await _poll(hass, entry, record, events=[record], next_seq=6)
    assert _stored(hass_storage, entry)["last_seq"] == 6
    latest = {**record, "sequence": None}

    async def switch(sync: bool) -> None:
        hass.config_entries.async_update_entry(
            entry, options={CONF_SYNC_STORED_RECORDS: sync, CONF_SCAN_INTERVAL: 300}
        )
        await hass.async_block_till_done()

    users_module = "custom_components.omron.users.assign_by_rules"
    # Off: the same newest record is read, and it is not new.
    await switch(False)
    with patch(users_module) as spy:
        await _poll(hass, entry, latest)
    spy.assert_not_called()
    # The sequence is no use without the sync and would block a later one.
    assert _stored(hass_storage, entry)["last_seq"] is None

    # On again: the position was dropped, so the first sync is a baseline.
    await switch(True)
    with patch(users_module) as spy:
        await _poll(hass, entry, latest, events=[], next_seq=6)
    spy.assert_not_called()
    assert _stored(hass_storage, entry)["fingerprint"] == [
        "2026-09-21T07:30:00",
        131,
        84,
        60,
    ]

    # A genuinely new record afterwards is assigned as usual.
    newer = _bp("2026-09-22T07:30:00", 133, 85, seq=7)
    await _poll(hass, entry, newer, events=[newer], next_seq=7)
    assert _sensor(hass, entry, "bob", "systolic").state == "133"


# --- BLS values never reach the assignment -------------------------------------------------------


async def test_the_live_value_that_the_cuff_sensors_show_is_not_assigned(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """The parser keeps the cuff's own record; a live BLS value is not in it."""
    import datetime as dt
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.omron.omron_ble import OmronBluetoothDeviceData
    from custom_components.omron.omron_ble import parser as parser_module
    from custom_components.omron.omron_ble.session_trace import SessionTrace

    data = OmronBluetoothDeviceData(device_model=CUFF)
    data._bp_char_unavailable = True
    eeprom = {
        "sys": 118,
        "dia": 76,
        "bpm": 61,
        "datetime": dt.datetime(2026, 9, 21, 7, 30),
        "ihb": 0,
        "mov": 0,
    }
    live = {
        "sys": 150,
        "dia": 95,
        "bpm": 70,
        "datetime": dt.datetime(2026, 9, 21, 9, 0),
    }
    data._driver.get_latest_record = AsyncMock(return_value=dict(eeprom))
    data._driver.write_user_profile = AsyncMock()
    data._read_latest_via_bls_racp = AsyncMock(return_value=live)
    session = MagicMock()
    session.trace = SessionTrace()
    client = MagicMock()
    client.services.get_characteristic.return_value = None
    with patch.object(parser_module, "async_sync_eeprom_time", AsyncMock()):
        await data._poll_device_readout(
            session, client, MagicMock(address=ADDRESS), memory_session_active=False
        )

    # The device's own sensors show the live reading ...
    values = {
        key.key: value.native_value
        for key, value in data._finish_update().entity_values.items()
    }
    assert 150 in values.values()
    # ... the record for the users is the cuff's.
    assert data.last_record["sys"] == 118
    assert data.last_record["dia"] == 76
    assert data.last_record["bpm"] == 61
    assert data.last_record["device_time"] == "2026-09-21T07:30:00"
    assert data.last_record["sequence"] is None
    assert data.last_record["irregular_pulse"] is False


async def test_last_record_is_reset_when_the_next_readout_finds_nothing(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    import datetime as dt
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.omron.omron_ble import OmronBluetoothDeviceData
    from custom_components.omron.omron_ble import parser as parser_module
    from custom_components.omron.omron_ble.session_trace import SessionTrace

    data = OmronBluetoothDeviceData(device_model=SCALE)
    record = {"weight": 62.4, "datetime": dt.datetime(2026, 9, 21, 7, 30)}
    data._driver.get_latest_record = AsyncMock(side_effect=[record, None])
    data._driver.write_user_profile = AsyncMock()

    async def readout() -> None:
        session = MagicMock()
        session.trace = SessionTrace()
        client = MagicMock()
        client.services.get_characteristic.return_value = None
        with patch.object(parser_module, "async_sync_eeprom_time", AsyncMock()):
            await data._poll_device_readout(
                session, client, MagicMock(address=ADDRESS), memory_session_active=False
            )

    await readout()
    assert data.last_record["weight"] == 62.4
    assert data.last_record["device_time"] == "2026-09-21T07:30:00"
    await readout()
    assert data.last_record is None


async def test_a_rebase_reaches_pending_history() -> None:
    import datetime as dt
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.omron.omron_ble import OmronBluetoothDeviceData
    from custom_components.omron.omron_ble.driver import StoredUserRecords

    data = OmronBluetoothDeviceData(device_model=SYNC_CUFF)
    data.history_last_seq = {1: 60000}
    trace = MagicMock()
    trace.timed.return_value.__enter__ = MagicMock()
    trace.timed.return_value.__exit__ = MagicMock(return_value=False)
    record = {
        "user": 1,
        "sequence": 4,
        "sys": 120,
        "dia": 80,
        "bpm": 60,
        "datetime": dt.datetime(2026, 9, 21, 7, 30),
    }
    for outcome, rebased in (
        (StoredUserRecords(next_seq=3, index_seq=3, rebased=True), True),
        (StoredUserRecords(next_seq=4, index_seq=4, records=[record]), False),
    ):
        data._driver.get_records_since = AsyncMock(return_value={1: outcome})
        await data._collect_stored_history(MagicMock(), trace)
        assert data.pending_history["rebased"] == {1: rebased}


# --- restart, removal ------------------------------------------------------------------------------


async def test_the_users_last_values_come_back_after_a_restart(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = _entry(hass, SCALE, ALICE_BOB)
    await _setup(hass, entry)
    await _poll(hass, entry, _scale("2026-09-20T07:00:00", 70.0))  # baseline
    await _poll(hass, entry, _scale("2026-09-21T07:30:00", 62.4))
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    weight = _sensor(hass, entry, "alice", "weight")
    assert weight.state == "62.4"
    assert weight.attributes["assignment"] == "matched"
    assert _sensor(hass, entry, "alice", "bmi").state == "21.6"
    assert _sensor(hass, entry, "bob", "weight").state == "unknown"
    # What was learned is still there, and so is the baseline: the record
    # that was handled before the restart is not handled again.
    assert entry.runtime_data.users.recent["alice"] == [62.4]
    with patch("custom_components.omron.users.assign_by_weight") as spy:
        await _poll(hass, entry, _scale("2026-09-21T07:30:00", 62.4))
    spy.assert_not_called()


async def test_the_restored_cuff_values_come_back_too(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    await _poll(hass, entry, _bp("2026-09-20T07:00:00"))  # baseline
    await _poll(hass, entry, _bp("2026-09-21T07:30:00", 131, 84))
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert _sensor(hass, entry, "bob", "systolic").state == "131"
    assert _sensor(hass, entry, "bob", "measured_at").state == (
        "2026-09-21T07:30:00+00:00"
    )


async def test_changing_the_strategy_forgets_the_learned_weights_only(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = _entry(hass, SCALE, ALICE_BOB)
    await _setup(hass, entry)
    await _poll(hass, entry, _scale("2026-09-20T07:00:00", 70.0))  # baseline
    await _poll(hass, entry, _scale("2026-09-21T07:30:00", 62.4))
    assert _stored(hass_storage, entry)["users"]["alice"]["recent"] == [62.4]

    hass.config_entries.async_update_entry(
        entry,
        options={
            CONF_ASSIGNMENT_STRATEGY: STRATEGY_RULES,
            CONF_WEIGHT_TOLERANCE_PCT: 3.0,
        },
    )
    await hass.async_block_till_done()

    users = entry.runtime_data.users
    assert users.strategy == STRATEGY_RULES
    assert users.recent == {}
    assert users.fingerprint == ["2026-09-21T07:30:00", 62.4]
    assert _sensor(hass, entry, "alice", "weight").state == "62.4"


async def test_a_user_without_a_usual_weight_is_warned_about_and_never_assigned(
    hass: HomeAssistant,
    enable_bluetooth: None,
    unassigned: list,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A user added under the rule strategy, after the scale went back to weight."""
    users = [
        ALICE_BOB[0],
        _user("carol", "Carol", weight_min=50.0),
    ]
    entry = _entry(hass, SCALE, users)
    await _setup(hass, entry)
    assert "Carol has no usual weight" in caplog.text

    await _poll(hass, entry, _scale("2026-09-20T07:00:00", 70.0))  # baseline
    await _poll(hass, entry, _scale("2026-09-21T07:30:00", 55.0))

    assert _sensor(hass, entry, "carol", "weight").state == "unknown"
    assert [c["user_id"] for c in unassigned[0].data["candidates"]] == ["alice"]


async def test_a_wider_tolerance_is_used(
    hass: HomeAssistant, enable_bluetooth: None, unassigned: list
) -> None:
    entry = _entry(hass, SCALE, [ALICE_BOB[0]], {CONF_WEIGHT_TOLERANCE_PCT: 5.0})
    await _setup(hass, entry)
    await _poll(hass, entry, _scale("2026-09-20T07:00:00", 1.0))  # baseline
    await _poll(hass, entry, _scale("2026-09-21T07:30:00", 65.0))
    assert _sensor(hass, entry, "alice", "weight").attributes["assignment"] == (
        "matched"
    )


async def test_removing_a_user_forgets_them_and_the_last_one_forgets_everything(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = _entry(hass, SCALE, ALICE_BOB)
    await _setup(hass, entry)
    await _poll(hass, entry, _scale("2026-09-20T07:00:00", 70.0))  # baseline
    await _poll(hass, entry, _scale("2026-09-21T07:30:00", 62.4))
    await _poll(hass, entry, _scale("2026-09-22T07:30:00", 80.4))
    assert set(_stored(hass_storage, entry)["users"]) == {"alice", "bob"}

    hass.config_entries.async_remove_subentry(entry, "alice")
    await hass.async_block_till_done()
    # The reload loaded the Store, dropped the removed user ...
    assert set(entry.runtime_data.users.last) == {"bob"}
    await _poll(hass, entry, _scale("2026-09-23T07:30:00", 80.9))
    # ... and the next save no longer has them.
    assert set(_stored(hass_storage, entry)["users"]) == {"bob"}

    hass.config_entries.async_remove_subentry(entry, "bob")
    await hass.async_block_till_done()
    assert entry.runtime_data.users is None
    assert f"omron.users_{entry.entry_id}" not in hass_storage

    # A new first user starts again from a baseline.
    hass.config_entries.async_add_subentry(
        entry,
        config_entries_subentry(
            "carol", "Carol", {"name": "Carol", "expected_weight": 55.0}
        ),
    )
    await hass.async_block_till_done()
    await _poll(hass, entry, _scale("2026-09-24T07:30:00", 55.0))
    assert _sensor(hass, entry, "carol", "weight").state == "unknown"


def config_entries_subentry(subentry_id: str, title: str, data: dict[str, Any]):
    from homeassistant.config_entries import ConfigSubentry

    return ConfigSubentry(
        data=data,
        subentry_id=subentry_id,
        subentry_type=SUBENTRY_USER,
        title=title,
        unique_id=None,
    )


async def test_removing_the_entry_removes_the_users_store(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = _entry(hass, SCALE, ALICE_BOB)
    await _setup(hass, entry)
    await _poll(hass, entry, _scale("2026-09-20T07:00:00", 70.0))
    assert f"omron.users_{entry.entry_id}" in hass_storage

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert f"omron.users_{entry.entry_id}" not in hass_storage


# --- without users nothing is touched ---------------------------------------------------------------


async def test_a_device_without_users_assigns_and_stores_nothing(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict, unassigned: list
) -> None:
    entry = _entry(hass, CUFF, [])
    await _setup(hass, entry)
    with patch("custom_components.omron.users.assign_by_rules") as spy:
        await _poll(hass, entry, _bp("2026-09-21T07:30:00", 131, 84))
    spy.assert_not_called()
    assert not unassigned
    assert not [key for key in hass_storage if key.startswith("omron.users_")]


async def test_a_failure_while_assigning_does_not_fail_the_poll(
    hass: HomeAssistant, enable_bluetooth: None, measurements: list
) -> None:
    entry = _entry(hass, SYNC_CUFF, MORNING_EVENING, _sync_options())
    await _setup(hass, entry)
    await _poll(hass, entry, _bp("2026-09-20T07:00:00"), events=[], next_seq=5)

    record = _bp("2026-09-21T07:30:00", 131, 84, seq=6)
    with patch(
        "custom_components.omron.users.assign_by_rules", side_effect=RuntimeError
    ):
        await _poll(hass, entry, record, events=[record], next_seq=6)
    # The stored-record sync that follows still published the event.
    assert [e.data["sequence"] for e in measurements] == [6]


# --- the first poll after registering: taken after it, or left over -------------------
#
# Registering the first user notes when (UTC). On the first poll, a record the
# device says was taken after that is a measurement for the users and goes
# through the rules at once; one taken before only sets the baseline, so a
# reading somebody left on the device is not handed to the new user.

REGISTERED = "2026-09-21T07:00:00+00:00"


async def test_a_measurement_taken_after_registering_is_assigned_on_the_first_poll(
    hass: HomeAssistant,
    enable_bluetooth: None,
    hass_storage: dict,
    unassigned: list,
    freezer,
) -> None:
    """The reported case: add a user, measure, and the first poll hands it out."""
    freezer.move_to(REGISTERED)
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    assert _stored(hass_storage, entry)["since"] == REGISTERED

    freezer.move_to("2026-09-21T07:31:00+00:00")
    await _poll(hass, entry, _bp("2026-09-21T07:30:00", 131, 84))

    assert _sensor(hass, entry, "bob", "systolic").state == "131"
    assert _sensor(hass, entry, "bob", "systolic").attributes["assignment"] == "rule"
    assert not unassigned
    stored = _stored(hass_storage, entry)
    assert stored["fingerprint"] == ["2026-09-21T07:30:00", 131, 84, 60]
    # Used once: from now on records are told apart by their fingerprint.
    assert stored["since"] is None

    # The same record read again is not handed out a second time.
    await _poll(hass, entry, _bp("2026-09-21T07:30:00", 131, 84))
    assert _sensor(hass, entry, "bob", "systolic").state == "131"
    assert not unassigned


async def test_the_rules_still_decide_on_the_first_poll(
    hass: HomeAssistant,
    enable_bluetooth: None,
    unassigned: list,
    freezer,
) -> None:
    """Taken after registering but outside every window: announced, given to nobody."""
    freezer.move_to(REGISTERED)
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)

    await _poll(hass, entry, _bp("2026-09-21T12:00:00", 131, 84))

    assert _sensor(hass, entry, "bob", "systolic").state == "unknown"
    assert _sensor(hass, entry, "eve", "systolic").state == "unknown"
    assert [e.data["reason"] for e in unassigned] == ["no_match"]


@pytest.mark.parametrize(
    ("taken", "assigned"),
    [
        # Left over from before the registration: only the baseline.
        ("2026-09-20T07:30:00", False),
        ("2026-09-21T06:50:00", False),
        # Within the clock tolerance of the registration.
        ("2026-09-21T06:56:00", True),
        ("2026-09-21T07:00:00", True),
    ],
)
async def test_a_record_from_before_registering_only_sets_the_baseline(
    hass: HomeAssistant,
    enable_bluetooth: None,
    unassigned: list,
    freezer,
    taken: str,
    assigned: bool,
) -> None:
    freezer.move_to(REGISTERED)
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)

    await _poll(hass, entry, _bp(taken, 131, 84))

    assert (_sensor(hass, entry, "bob", "systolic").state == "131") is assigned
    assert not unassigned


async def test_a_record_without_a_time_only_sets_the_baseline(
    hass: HomeAssistant, enable_bluetooth: None, unassigned: list, freezer
) -> None:
    freezer.move_to(REGISTERED)
    entry = _entry(hass, CUFF, [_user("bob", "Bob", sys_min=100)])
    await _setup(hass, entry)

    await _poll(hass, entry, _bp(None, 131, 84))

    assert _sensor(hass, entry, "bob", "systolic").state == "unknown"
    assert not unassigned


async def test_synced_records_taken_after_registering_are_assigned_on_the_first_poll(
    hass: HomeAssistant,
    enable_bluetooth: None,
    hass_storage: dict,
    unassigned: list,
    freezer,
) -> None:
    freezer.move_to(REGISTERED)
    entry = _entry(hass, SYNC_CUFF, MORNING_EVENING, _sync_options())
    await _setup(hass, entry)

    before = _bp("2026-09-21T06:30:00", 140, 90, seq=5)
    after = _bp("2026-09-21T07:30:00", 131, 84, seq=6)
    evening = _bp("2026-09-21T19:30:00", 118, 76, seq=7)
    freezer.move_to("2026-09-21T20:00:00+00:00")
    await _poll(hass, entry, evening, events=[before, after, evening], next_seq=7)

    assert _sensor(hass, entry, "bob", "systolic").state == "131"
    assert _sensor(hass, entry, "eve", "systolic").state == "118"
    assert not unassigned
    stored = _stored(hass_storage, entry)
    assert stored["last_seq"] == 7
    assert stored["since"] is None


async def test_the_registration_time_survives_a_restart_before_the_first_poll(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict, freezer
) -> None:
    """A reload before the first poll keeps the first registration time."""
    freezer.move_to(REGISTERED)
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)

    freezer.move_to("2026-09-21T08:00:00+00:00")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _stored(hass_storage, entry)["since"] == REGISTERED

    await _poll(hass, entry, _bp("2026-09-21T07:30:00", 131, 84))
    assert _sensor(hass, entry, "bob", "systolic").state == "131"


async def test_an_existing_baseline_gets_no_registration_time(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict, freezer
) -> None:
    """Users that already have a baseline need none: nothing changes for them."""
    freezer.move_to(REGISTERED)
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    await _poll(hass, entry, _bp("2026-09-21T07:30:00", 131, 84))
    assert _stored(hass_storage, entry)["since"] is None

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _stored(hass_storage, entry)["since"] is None
    assert _sensor(hass, entry, "bob", "systolic").state == "131"
