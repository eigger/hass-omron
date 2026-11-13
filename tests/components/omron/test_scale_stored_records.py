"""Stored-record sync of the HN-300T2 weight scale, by the record's own number.

Nothing here was checked on a scale: the layout is the reporter's capture
(docs/hn-300t2-memory-map.md), and the ring is simulated.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from unittest.mock import AsyncMock, MagicMock

from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.omron.const import (
    CONF_ASSIGNMENT_STRATEGY,
    CONF_DEVICE_MODEL,
    CONF_SYNC_STORED_RECORDS,
    DOMAIN,
    EVENT_UNASSIGNED_MEASUREMENT,
    STRATEGY_RULES,
)
from custom_components.omron.omron_ble import OmronBluetoothDeviceData
from custom_components.omron.omron_ble.devices import get_device_config
from custom_components.omron.omron_ble.driver import (
    OmronDeviceDriver,
    StoredUserRecords,
)
from custom_components.omron.omron_ble.session import OmronDeviceSession

from .user_harness import (
    ADDRESS,
    add_entry,
    poll,
    scale_event,
    sensor_state,
    setup_entry,
    user,
)

INDEX_ADDR = 0x01A0
RECORDS = 0x02C0
RING = 30
NOW = dt.datetime(2026, 10, 7, 12, 0, 0)


def _cursor_byte(pointer: int) -> int:
    """The pointer in the low six bits, bit 7 keeping the byte's parity odd."""
    byte = pointer & 0x3F
    if bin(byte).count("1") % 2 == 0:
        byte |= 0x80
    return byte


def _record(number: int, weight: float) -> bytes:
    """One 16-byte record the way the capture lays it out."""
    when = dt.datetime(2026, 10, 6, 18, 0, 0) + dt.timedelta(minutes=number % 600)
    raw = bytearray(16)
    raw[0:2] = round(weight / 0.05).to_bytes(2, "big")
    raw[2:8] = bytes(
        [when.year - 2000, when.month, when.day, when.hour, when.minute, when.second]
    )
    raw[9:11] = (number & 0xFFFF).to_bytes(2, "big")
    raw[11:13] = round(weight * 2.20462 / 0.2).to_bytes(2, "big")
    return bytes(raw)


class Scale:
    """A scale whose newest record is ``newest``; numbers count up from 1."""

    def __init__(self, newest: int | None, weights: dict[int, float] | None = None):
        self.newest = newest
        self.weights = weights or {}
        self.reads: list[int] = []

    def weight(self, number: int) -> float:
        return self.weights.get(number, 60.0 + number % 40)

    def slot_of(self, number: int) -> int:
        return (number - 1) % RING

    def memory(self, addr, size, block=0x10):
        self.reads.append(addr)
        if addr == INDEX_ADDR:
            raw = bytearray(12)
            if self.newest is None:
                raw[0] = 0x80
                raw[4] = 0x80
                raw[11] = 0x80
                return raw
            raw[0] = _cursor_byte((self.slot_of(self.newest) + 1) % RING)
            raw[4] = 0x01
            raw[8:10] = (self.newest & 0xFFFF).to_bytes(2, "big")
            raw[11] = 0x80
            return raw
        slot = (addr - RECORDS) // 16
        if self.newest is not None:
            for number in range(self.newest, max(self.newest - RING, 0), -1):
                if self.slot_of(number) == slot:
                    return bytearray(_record(number, self.weight(number)))
        return bytearray(b"\xff" * 16)


def _sync(scale: Scale, last: dict[int, int], max_reads: int = 30):
    config = get_device_config("HN-300T2")
    driver = OmronDeviceDriver(config)
    driver._now_func = lambda: NOW
    transport = OmronDeviceSession(MagicMock(), config)
    transport.memory.read_memory_range = AsyncMock(side_effect=scale.memory)
    return asyncio.run(driver.get_records_since(transport, last, max_reads=max_reads))


def test_the_profile_syncs_by_the_records_own_number() -> None:
    config = get_device_config("HN-300T2")
    assert config.supports_stored_record_sync
    assert not config.is_blood_pressure
    assert [u.get("sequence_source") for u in config.index_pointer_layout["users"]] == [
        "record_id"
    ]


def test_the_first_sync_only_notes_the_newest_number() -> None:
    scale = Scale(380)
    out = _sync(scale, {})
    assert out[1].records == [] and out[1].next_seq == 380
    # The index and the newest record: nothing replayed.
    assert scale.reads == [INDEX_ADDR, RECORDS + 16 * scale.slot_of(380)]


def test_nothing_new_reads_only_the_index_and_the_newest_record() -> None:
    out = _sync(Scale(380), {1: 380})
    assert out[1].records == [] and out[1].next_seq == 380 and not out[1].rebased


def test_new_weights_come_back_oldest_first_with_a_time() -> None:
    scale = Scale(383, {381: 70.1, 382: 70.5, 383: 71.0})
    out = _sync(scale, {1: 380})
    records = out[1].records
    assert [r["sequence"] for r in records] == [381, 382, 383]
    assert [r["weight"] for r in records] == [70.1, 70.5, 71.0]
    assert all(isinstance(r["datetime"], dt.datetime) for r in records)
    assert out[1].next_seq == 383 and out[1].complete and out[1].lost == 0
    assert {r["user"] for r in records} == {1}


def test_the_ring_end_is_crossed() -> None:
    # Slot 29 holds number 30, 60, 90 ...; the next one lands in slot 0.
    out = _sync(Scale(32), {1: 28})
    assert [r["sequence"] for r in out[1].records] == [29, 30, 31, 32]


def test_the_sixteen_bit_number_wraps() -> None:
    out = _sync(Scale(0x10002), {1: 0xFFFF})
    assert [r["sequence"] for r in out[1].records] == [0, 1, 2]


def test_a_number_that_went_back_is_a_rebase() -> None:
    out = _sync(Scale(5), {1: 900})
    assert out[1].rebased and out[1].records == [] and out[1].next_seq == 5


def test_a_scale_that_never_measured_is_left_alone() -> None:
    assert _sync(Scale(None), {1: 3}) == {}


def test_a_cursor_wrapped_to_zero_is_not_taken_for_a_cleared_scale() -> None:
    # Odd parity makes pointer 0 the byte 0x80, which is also the cleared marker;
    # the record the cursor points at tells the two apart.
    scale = Scale(60)
    assert scale.memory(INDEX_ADDR, 12)[0] == 0x80
    out = _sync(scale, {1: 58})
    assert [r["sequence"] for r in out[1].records] == [59, 60]


def test_the_backlog_is_cut_at_max_reads_and_resumed() -> None:
    scale = Scale(60)
    first = _sync(scale, {1: 40}, max_reads=5)
    assert [r["sequence"] for r in first[1].records] == [41, 42, 43, 44, 45]
    assert not first[1].complete and first[1].next_seq == 45
    second = _sync(scale, {1: 45})
    assert [r["sequence"] for r in second[1].records][-1] == 60 and second[1].complete


# --- the parser: weight reaches the events and the state changes ---------------------


def _collect(data: OmronBluetoothDeviceData, outcome) -> None:
    trace = MagicMock()
    trace.timed.return_value.__enter__ = MagicMock()
    trace.timed.return_value.__exit__ = MagicMock(return_value=False)
    data._driver.get_records_since = AsyncMock(return_value=outcome)
    asyncio.run(data._collect_stored_history(MagicMock(), trace))


def _stored_weight(number: int, minute: int, weight: float) -> dict:
    return {
        "user": 1,
        "sequence": number,
        "weight": weight,
        "datetime": dt.datetime(2026, 10, 6, 18, minute, 0),
    }


def test_synced_weights_become_events_and_states() -> None:
    data = OmronBluetoothDeviceData(device_model="HN-300T2")
    data.history_last_seq = {1: 380}
    records = [
        _stored_weight(381, 1, 70.1),
        _stored_weight(382, 2, 70.5),
        _stored_weight(383, 3, 71.0),
    ]
    _collect(
        data,
        {1: StoredUserRecords(next_seq=383, index_seq=383, records=records)},
    )
    events = data.pending_history["events"]
    assert [e["sequence"] for e in events] == [381, 382, 383]
    assert [e["weight"] for e in events] == [70.1, 70.5, 71.0]
    assert events[0]["device_time"] == "2026-10-06T18:01:00"
    assert "sys" not in events[0]
    # The newest is the poll's own result; the two before it get a state each.
    assert len(data.history_updates) == 2
    weights = [
        next(
            v.native_value for k, v in update.entity_values.items() if k.key == "weight"
        )
        for update in data.history_updates
    ]
    assert weights == [70.1, 70.5]
    assert all(
        any(k.key == "measurement_timestamp" for k in update.entity_values)
        for update in data.history_updates
    )


# --- the option and the user hook --------------------------------------------------


async def test_the_options_form_offers_the_sync_for_the_scale(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=ADDRESS, data={CONF_DEVICE_MODEL: "HN-300T2"}
    )
    entry.add_to_hass(hass)
    form = await hass.config_entries.options.async_init(entry.entry_id)
    assert CONF_SYNC_STORED_RECORDS in {str(k) for k in form["data_schema"].schema}
    done = await hass.config_entries.options.async_configure(
        form["flow_id"],
        user_input={
            CONF_SCAN_INTERVAL: 300,
            CONF_SYNC_STORED_RECORDS: True,
            "assignment_strategy": "weight",
            "weight_tolerance_pct": 3.0,
        },
    )
    assert done["data"][CONF_SYNC_STORED_RECORDS] is True


async def test_turning_the_sync_on_loads_it_into_the_parser(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, "HN-300T2", [], {CONF_SYNC_STORED_RECORDS: True})
    await setup_entry(hass, entry)
    assert entry.runtime_data.device_data.history_last_seq == {}


ALICE_BOB = [
    user("alice", "Alice", expected_weight=62.0, height_cm=170),
    user("bob", "Bob", expected_weight=80.0, height_cm=180),
]
SYNC = {CONF_SYNC_STORED_RECORDS: True}


async def test_synced_weights_are_assigned_by_weight_oldest_first(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    unassigned = async_capture_events(hass, EVENT_UNASSIGNED_MEASUREMENT)
    entry = add_entry(hass, "HN-300T2", ALICE_BOB, dict(SYNC))
    await setup_entry(hass, entry)
    newest = scale_event("2026-10-06T19:00:00", 80.4, seq=383)
    await poll(
        hass,
        entry,
        scale_event("2026-10-06T07:00:00", 70.0, seq=380),
        events=[],
        next_seq=380,
    )  # baseline

    # Numbered 381 (Alice) before 383 (Bob), though the scale's clock disagrees.
    older = scale_event("2026-10-06T20:00:00", 62.3, seq=381)
    ambiguous = scale_event("2026-10-06T08:00:00", 71.0, seq=382)
    await poll(hass, entry, newest, events=[newest, ambiguous, older], next_seq=383)

    assert sensor_state(hass, entry, "alice", "weight").state == "62.3"
    assert sensor_state(hass, entry, "bob", "weight").state == "80.4"
    assert sensor_state(hass, entry, "bob", "bmi").state == "24.8"
    # 71.0 is between the two and out of both tolerances: announced, not given.
    assert [e.data["weight"] for e in unassigned] == [71.0]
    assert unassigned[0].data["sequence"] == 382
    assert unassigned[0].data["reason"] == "out_of_range"


async def test_synced_weights_are_assigned_by_rules_too(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    morning_evening = [
        user("alice", "Alice", time_windows="06:00-09:00"),
        user("bob", "Bob", time_windows="18:00-21:00"),
    ]
    entry = add_entry(
        hass,
        "HN-300T2",
        morning_evening,
        {**SYNC, CONF_ASSIGNMENT_STRATEGY: STRATEGY_RULES},
    )
    await setup_entry(hass, entry)
    await poll(
        hass,
        entry,
        scale_event("2026-10-06T07:00:00", 70.0, seq=10),
        events=[],
        next_seq=10,
    )

    morning = scale_event("2026-10-07T07:30:00", 61.0, seq=11)
    evening = scale_event("2026-10-07T19:30:00", 82.0, seq=12)
    await poll(hass, entry, evening, events=[evening, morning], next_seq=12)

    assert sensor_state(hass, entry, "alice", "weight").state == "61.0"
    assert sensor_state(hass, entry, "bob", "weight").state == "82.0"
    assert sensor_state(hass, entry, "bob", "weight").attributes["assignment"] == "rule"


async def test_an_event_already_handled_is_not_given_twice(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, "HN-300T2", ALICE_BOB, dict(SYNC))
    await setup_entry(hass, entry)
    await poll(
        hass,
        entry,
        scale_event("2026-10-06T07:00:00", 70.0, seq=10),
        events=[],
        next_seq=10,
    )
    first = scale_event("2026-10-07T07:00:00", 62.0, seq=11)
    await poll(hass, entry, first, events=[first], next_seq=11)
    assert entry.runtime_data.users.recent["alice"] == [62.0]

    # The same event again (a repeated poll) and a later one.
    later = scale_event("2026-10-08T07:00:00", 62.6, seq=12)
    await poll(hass, entry, later, events=[first, later], next_seq=12)
    assert entry.runtime_data.users.recent["alice"] == [62.0, 62.6]
