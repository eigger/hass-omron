"""Stored-record sync for profiles with no record counter: the write cursor.

The sequence is made up from how far the cursor moved. It is derived from the
memory maps and was not checked on a device; the ring is simulated here.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant

from custom_components.omron.const import CONF_SYNC_STORED_RECORDS
from custom_components.omron.omron_ble import OmronBluetoothDeviceData
from custom_components.omron.omron_ble.device_catalog import CANONICAL_DEVICE_PROFILES
from custom_components.omron.omron_ble.devices import (
    SEQUENCE_COUNTER,
    SEQUENCE_CURSOR,
    SEQUENCE_RECORD_ID,
    DeviceConfig,
    Endianness,
    get_device_config,
    stored_record_sequence_kind,
)
from custom_components.omron.omron_ble.driver import (
    OmronDeviceDriver,
    StoredUserRecords,
)
from custom_components.omron.omron_ble.session import OmronDeviceSession

from .test_stored_records_sync import NOW, SIZE, _record as classic_record
from .user_harness import (
    add_entry,
    bp_event,
    poll,
    sensor_state,
    setup_entry,
    user,
)

INDEX_ADDR = 0x0010
BASE = 0x0098
SLOTS = 100


def _config(*, clear_value: int | None = None) -> DeviceConfig:
    layout: dict[str, Any] = {
        "write_cursor_offset": 0,
        "unread_counter_offset": 4,
        "write_cursor_mask": 0xFF,
        "slot_index_min": 0,
        "slot_index_max": SLOTS - 1,
        "slot_index_bias": -1,
    }
    if clear_value is not None:
        layout["clear_value"] = clear_value
    return DeviceConfig(
        model="HEM-7361T",
        endianness=Endianness.LITTLE,
        user_start_addresses=[BASE],
        per_user_records_count=[SLOTS],
        record_byte_size=SIZE,
        settings_read_address=INDEX_ADDR,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "users": [layout],
        },
    )


class Cuff:
    """A cuff with no counter: only a write cursor and the records themselves.

    ``written`` is how many measurements were ever stored, so the newest sits in
    slot ``(written - 1) % SLOTS`` and the cursor reads ``written % SLOTS``.
    ``salt`` shifts every value, to stand for a memory that was cleared and
    filled again with other readings.
    """

    def __init__(self, written: int, *, salt: int = 0, minute_shift: int = 0):
        self.config = _config()
        self.written = written
        self.salt = salt
        self.minute_shift = minute_shift
        self.reads: list[int] = []
        self.fail_after: int | None = None

    def slot_of(self, number: int) -> int:
        return (number - 1) % SLOTS

    def memory(self, addr, size, block=0x10):
        self.reads.append(addr)
        if addr == INDEX_ADDR:
            raw = bytearray(0x10)
            # Both are what a counter-less cuff has: the pointer and nothing else.
            raw[0:2] = (self.written % SLOTS).to_bytes(2, "little")
            return raw
        if self.fail_after is not None and len(self.reads) > self.fail_after:
            raise ConnectionError("link lost")
        slot = (addr - BASE) // SIZE
        for number in range(self.written, max(self.written - SLOTS, 0), -1):
            if self.slot_of(number) == slot:
                raw = classic_record(
                    0,
                    (number + self.minute_shift) % 1000,
                    sys=100 + (number + self.salt) % 100,
                )
                raw[10:12] = b"\x00\x00"  # no record number on these profiles
                return raw
        return bytearray(b"\xff" * SIZE)


def _state(cuff: Cuff, written: int) -> dict[str, Any]:
    """What the host saved after handling the first ``written`` records of ``cuff``."""
    other = Cuff(written, salt=cuff.salt, minute_shift=cuff.minute_shift)
    slot = (written - 1) % SLOTS
    parsed = other.config.parse_record(bytes(other.memory(BASE + slot * SIZE, SIZE)))
    return {
        "slot": slot,
        "newest": [
            parsed["datetime"].isoformat(),
            parsed["sys"],
            parsed["dia"],
            parsed["bpm"],
        ],
    }


def _sync(
    cuff: Cuff,
    last: int | None,
    state: dict[str, Any] | None,
    max_reads: int = 30,
) -> StoredUserRecords | None:
    driver = OmronDeviceDriver(cuff.config)
    driver._now_func = lambda: NOW + dt.timedelta(days=1)
    transport = OmronDeviceSession(MagicMock(), cuff.config)
    transport.memory.read_memory_range = AsyncMock(side_effect=cuff.memory)
    out = asyncio.run(
        driver.get_records_since(
            transport,
            {} if last is None else {1: last},
            max_reads=max_reads,
            cursor_by_user={} if state is None else {1: state},
        )
    )
    return out.get(1)


# --- which profiles this is for ----------------------------------------------------------


def test_the_kind_is_told_from_the_layout() -> None:
    assert (
        stored_record_sequence_kind({"latest_sequence_offset": 8}) == SEQUENCE_COUNTER
    )
    assert (
        stored_record_sequence_kind(
            {"sequence_source": "record_id", "write_cursor_offset": 0}
        )
        == SEQUENCE_RECORD_ID
    )
    assert stored_record_sequence_kind({"write_cursor_offset": 0}) == SEQUENCE_CURSOR
    assert stored_record_sequence_kind({}) is None


def test_every_profile_with_a_cursor_but_no_counter_is_a_cursor_profile() -> None:
    cursor_profiles = {
        model
        for model, config in CANONICAL_DEVICE_PROFILES.items()
        if SEQUENCE_CURSOR in config.stored_record_sequence_kinds
    }
    assert "HEM-7600T" in cursor_profiles
    for model, config in CANONICAL_DEVICE_PROFILES.items():
        assert config.supports_stored_record_sync, model
        for user_layout, kind in zip(
            config.index_pointer_layout["users"],
            config.stored_record_sequence_kinds,
            strict=True,
        ):
            if kind == SEQUENCE_CURSOR:
                assert user_layout.get("latest_sequence_offset") is None
                assert user_layout.get("sequence_source") is None
    # The profiles that already had a counter or a record number keep it.
    assert get_device_config("HEM-7530T").stored_record_sequence_kinds == [
        SEQUENCE_COUNTER
    ]
    assert get_device_config("HN-300T2").stored_record_sequence_kinds == [
        SEQUENCE_RECORD_ID
    ]
    assert (
        get_device_config("HBF-222T").stored_record_sequence_kinds
        == [SEQUENCE_RECORD_ID] * 4
    )


# --- the cursor ---------------------------------------------------------------------------


def test_first_sync_only_notes_where_the_cursor_is() -> None:
    cuff = Cuff(written=37)
    out = _sync(cuff, None, None)
    assert out.records == [] and not out.rebased
    assert out.next_seq == 0 and out.index_seq == 0
    assert out.cursor == _state(cuff, 37)
    # The index and the newest record, nothing replayed.
    assert cuff.reads == [INDEX_ADDR, BASE + 36 * SIZE]


def test_nothing_new_reads_the_index_and_the_newest_record_only() -> None:
    cuff = Cuff(written=37)
    out = _sync(cuff, 12, _state(cuff, 37))
    assert out.records == [] and not out.rebased and out.next_seq == 12
    assert out.cursor == _state(cuff, 37)
    assert len(cuff.reads) == 2


def test_new_records_are_numbered_from_the_last_sequence_oldest_first() -> None:
    before = Cuff(written=37)
    cuff = Cuff(written=40)
    out = _sync(cuff, 12, _state(before, 37))
    assert [r["sequence"] for r in out.records] == [13, 14, 15]
    assert [r["sys"] for r in out.records] == [138, 139, 140]
    times = [r["datetime"] for r in out.records]
    assert times == sorted(times)
    assert out.next_seq == 15 and out.index_seq == 15
    assert out.complete and out.lost == 0 and not out.rebased
    assert out.cursor == _state(cuff, 40)
    # The newest slot is read once, not twice.
    assert len(cuff.reads) == 1 + 3
    assert [r["user"] for r in out.records] == [1, 1, 1]


def test_the_ring_end_is_crossed() -> None:
    before = Cuff(written=98)
    cuff = Cuff(written=103)  # slots 98, 99, 0, 1, 2
    out = _sync(cuff, 5, _state(before, 98))
    assert [r["sequence"] for r in out.records] == [6, 7, 8, 9, 10]
    assert out.cursor["slot"] == 2


def test_the_made_up_sequence_wraps_at_sixteen_bits() -> None:
    before = Cuff(written=10)
    cuff = Cuff(written=12)
    out = _sync(cuff, 0xFFFF, _state(before, 10))
    assert [r["sequence"] for r in out.records] == [0, 1]
    assert out.next_seq == 1


def test_a_cursor_that_has_not_moved_but_a_newest_record_that_changed_is_a_rebase() -> (
    None
):
    # Written exactly one lap, or the memory cleared and refilled to the same slot.
    before = Cuff(written=37)
    cleared_and_refilled = Cuff(written=37, salt=40)
    out = _sync(cleared_and_refilled, 12, _state(before, 37))
    assert out.rebased and out.records == []
    assert out.next_seq == 12 and out.index_seq == 12
    assert out.cursor == _state(cleared_and_refilled, 37)


def test_a_newest_record_older_than_the_last_handled_one_is_a_rebase() -> None:
    before = Cuff(written=37)
    # The cursor moved, but what it points at was taken before the last handled.
    earlier = Cuff(written=40, minute_shift=-30)
    out = _sync(earlier, 12, _state(before, 37))
    assert out.rebased and out.records == []
    assert out.next_seq == 12
    assert out.cursor == _state(earlier, 40)


def test_a_rebase_is_not_reported_for_a_plain_advance() -> None:
    before = Cuff(written=37)
    out = _sync(Cuff(written=38), 12, _state(before, 37))
    assert not out.rebased and len(out.records) == 1


def test_a_long_backlog_is_cut_at_max_reads_and_resumed() -> None:
    before = Cuff(written=20)
    cuff = Cuff(written=30)
    first = _sync(cuff, 0, _state(before, 20), max_reads=4)
    assert [r["sequence"] for r in first.records] == [1, 2, 3, 4]
    assert not first.complete and first.next_seq == 4 and first.index_seq == 10
    assert first.cursor["slot"] == 23  # 24th record written: slot 23
    second = _sync(cuff, first.next_seq, first.cursor)
    assert [r["sequence"] for r in second.records] == [5, 6, 7, 8, 9, 10]
    assert second.complete and second.cursor == _state(cuff, 30)


def test_a_failed_read_keeps_what_was_read_and_stops() -> None:
    before = Cuff(written=20)
    cuff = Cuff(written=30)
    cuff.fail_after = 3  # index, newest record, one more, then the link drops
    out = _sync(cuff, 0, _state(before, 20))
    assert [r["sequence"] for r in out.records] == [1]
    assert out.next_seq == 1 and not out.complete
    assert out.cursor["slot"] == 20


def test_an_unreadable_record_is_counted_lost_and_still_numbered() -> None:
    before = Cuff(written=20)
    cuff = Cuff(written=23)
    original = cuff.memory

    def memory(addr, size, block=0x10):
        if addr == BASE + 21 * SIZE:
            return bytearray(b"\xff" * SIZE)
        return original(addr, size, block)

    cuff.memory = memory  # type: ignore[method-assign]
    out = _sync(cuff, 0, _state(before, 20))
    assert [r["sequence"] for r in out.records] == [1, 3]
    assert out.lost == 1 and out.next_seq == 3


def test_a_cuff_with_nothing_stored_is_baselined_and_its_first_record_arrives() -> None:
    empty = Cuff(written=0)
    first = _sync(empty, None, None)
    assert first.cursor["slot"] == SLOTS - 1 and first.cursor["newest"] is None
    cuff = Cuff(written=1)
    out = _sync(cuff, first.next_seq, first.cursor)
    assert [r["sequence"] for r in out.records] == [1]


def test_a_cleared_cursor_with_nothing_behind_it_is_left_alone() -> None:
    config = _config(clear_value=0x8000)
    cuff = Cuff(written=0)
    cuff.config = config

    def memory(addr, size, block=0x10):
        if addr == INDEX_ADDR:
            raw = bytearray(0x10)
            raw[0:2] = (0x8000).to_bytes(2, "little")
            return raw
        return bytearray(b"\xff" * SIZE)

    cuff.memory = memory  # type: ignore[method-assign]
    assert _sync(cuff, None, None) is None


def test_a_stale_position_outside_the_ring_is_taken_as_a_new_baseline() -> None:
    cuff = Cuff(written=37)
    out = _sync(cuff, 7, {"slot": 5000, "newest": None})
    assert out.records == [] and out.next_seq == 7
    assert out.cursor == _state(cuff, 37)


def test_a_profile_with_a_real_counter_ignores_the_cursor_state() -> None:
    # The same machinery as test_stored_records_sync, with a cursor state given.
    from .test_stored_records_sync import Cuff as CounterCuff
    from .test_stored_records_sync import _config as counter_config

    cuff = CounterCuff(counter_config(), newest=(53, 7))
    driver = OmronDeviceDriver(cuff.config)
    driver._now_func = lambda: NOW + dt.timedelta(days=1)
    transport = OmronDeviceSession(MagicMock(), cuff.config)
    transport.memory.read_memory_range = AsyncMock(side_effect=cuff.memory)
    out = asyncio.run(
        driver.get_records_since(
            transport,
            {1: 50, 2: 7},
            max_reads=30,
            cursor_by_user={1: {"slot": 3, "newest": None}},
        )
    )
    assert [r["sequence"] for r in out[1].records] == [51, 52, 53]
    assert out[1].cursor is None and out[2].cursor is None


# --- the parser and the saved position --------------------------------------------------------


def test_the_parser_hands_the_cursor_to_the_driver_and_back() -> None:
    config = _config()
    before = Cuff(written=37)
    cuff = Cuff(written=39)
    data = OmronBluetoothDeviceData(device_model="HEM-7361T")
    data._device_config = config
    data._driver = OmronDeviceDriver(config)
    data._driver._now_func = lambda: NOW + dt.timedelta(days=1)
    data.history_last_seq = {1: 12}
    data.history_cursor = {1: _state(before, 37)}
    session = MagicMock()
    session.memory.read_memory_range = AsyncMock(side_effect=cuff.memory)
    trace = MagicMock()
    trace.timed.return_value.__enter__ = MagicMock()
    trace.timed.return_value.__exit__ = MagicMock(return_value=False)

    asyncio.run(data._collect_stored_history(session, trace))

    pending = data.pending_history
    assert [e["sequence"] for e in pending["events"]] == [13, 14]
    assert pending["next_seq"] == {1: 14}
    assert pending["rebased"] == {1: False}
    assert pending["cursor"] == {1: _state(cuff, 39)}
    # The older record is a state change of its own; the newest is the poll's.
    assert len(data.history_updates) == 1


def test_a_counter_profile_leaves_no_cursor_in_the_findings() -> None:
    data = OmronBluetoothDeviceData(device_model="HEM-7361T")
    data.history_last_seq = {1: 1}
    data._driver.get_records_since = AsyncMock(
        return_value={1: StoredUserRecords(next_seq=1, index_seq=1)}
    )
    trace = MagicMock()
    trace.timed.return_value.__enter__ = MagicMock()
    trace.timed.return_value.__exit__ = MagicMock(return_value=False)
    asyncio.run(data._collect_stored_history(MagicMock(), trace))
    assert "cursor" not in data.pending_history


def test_the_cursor_is_saved_with_the_position_and_loaded_back() -> None:
    from unittest.mock import patch

    from custom_components.omron import history

    saved: dict[str, Any] = {}
    store = MagicMock()
    store.async_save = AsyncMock(side_effect=saved.update)
    state = {"slot": 4, "newest": ["2026-09-20T08:04:00", 104, 80, 60]}
    pending = {
        "next_seq": {1: 3},
        "events": [],
        "shown": {1: 3},
        "cursor": {1: state},
    }
    device_data = MagicMock()
    device_data.pending_history = pending
    device_data.history_last_seq = {1: 0}
    device_data.history_shown = {}
    device_data.history_cursor = {}
    entry = MagicMock()
    entry.runtime_data.device_data = device_data
    with (
        patch.object(history, "_store", return_value=store),
        patch.object(history.dr, "async_get"),
    ):
        asyncio.run(history.async_publish_history(MagicMock(), entry))
    assert saved == {
        "last_seq": {"1": 3},
        "shown": {"1": 3},
        "cursor": {"1": state},
    }
    assert device_data.history_cursor == {1: state}

    store.async_load = AsyncMock(return_value={**saved, "cursor": {"1": state, "x": 1}})
    target = MagicMock()
    with patch.object(history, "_store", return_value=store):
        asyncio.run(
            history.async_setup_history(
                MagicMock(), "e1", target, supported=True, enabled=True
            )
        )
    assert target.history_last_seq == {1: 3}
    assert target.history_cursor == {1: state}


# --- the option, the events and the users --------------------------------------------------


async def test_the_sync_is_off_on_a_counterless_cuff_until_asked_for(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, "HEM-7600T", [], {})
    await setup_entry(hass, entry)
    assert entry.runtime_data.device_data.history_last_seq is None


async def test_the_sync_on_a_counterless_cuff_follows_the_option(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, "HEM-7600T", [], {CONF_SYNC_STORED_RECORDS: True})
    await setup_entry(hass, entry)
    assert entry.runtime_data.device_data.history_last_seq == {}
    assert entry.runtime_data.device_data.history_cursor == {}


async def test_made_up_sequences_flow_through_events_and_users(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    from pytest_homeassistant_custom_component.common import async_capture_events

    measurements = async_capture_events(hass, "omron_measurement")
    users = [
        user("bob", "Bob", time_windows="06:00-09:00"),
        user("eve", "Eve", time_windows="18:00-21:00"),
    ]
    entry = add_entry(hass, "HEM-7600T", users, {CONF_SYNC_STORED_RECORDS: True})
    await setup_entry(hass, entry)
    baseline = {"slot": 9, "newest": ["2026-09-20T07:00:00", 120, 80, 60]}

    # Baseline: the cursor and the sequence 0, nothing emitted.
    await poll(
        hass,
        entry,
        bp_event("2026-09-20T07:00:00"),
        events=[],
        next_seq=0,
        cursor={1: baseline},
    )
    assert not measurements
    history_store = hass_storage[f"omron.history_{entry.entry_id}"]["data"]
    assert history_store["last_seq"] == {"1": 0}
    assert history_store["cursor"] == {"1": baseline}

    morning = bp_event("2026-09-21T07:30:00", 131, 84, seq=1)
    evening = bp_event("2026-09-21T19:30:00", 118, 76, seq=2)
    moved = {"slot": 11, "newest": ["2026-09-21T19:30:00", 118, 76, 60]}
    await poll(
        hass,
        entry,
        evening,
        events=[morning, evening],
        next_seq=2,
        cursor={1: moved},
    )
    assert [e.data["sequence"] for e in measurements] == [1, 2]
    assert sensor_state(hass, entry, "bob", "systolic").state == "131"
    assert sensor_state(hass, entry, "eve", "systolic").state == "118"
    history_store = hass_storage[f"omron.history_{entry.entry_id}"]["data"]
    assert history_store["last_seq"] == {"1": 2}
    assert history_store["cursor"] == {"1": moved}
    assert entry.runtime_data.device_data.history_cursor == {1: moved}

    # A rebase: nothing is emitted, the sequence stands, the cursor is the new one.
    rebased = {"slot": 50, "newest": ["2026-10-01T07:30:00", 140, 90, 60]}
    await poll(
        hass,
        entry,
        bp_event("2026-10-01T07:30:00", 140, 90),
        events=[],
        next_seq=2,
        rebased=True,
        cursor={1: rebased},
    )
    assert len(measurements) == 2
    assert hass_storage[f"omron.history_{entry.entry_id}"]["data"]["cursor"] == {
        "1": rebased
    }
    after = bp_event("2026-10-02T07:30:00", 133, 85, seq=3)
    await poll(
        hass,
        entry,
        after,
        events=[after],
        next_seq=3,
        cursor={1: {"slot": 51, "newest": ["2026-10-02T07:30:00", 133, 85, 60]}},
    )
    assert sensor_state(hass, entry, "bob", "systolic").state == "133"
