"""Stored-record sync: every measurement taken between two polls (#234)."""
import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.omron import history
from custom_components.omron.omron_ble.devices import DeviceConfig, Endianness, get_device_config
from custom_components.omron.omron_ble.driver import OmronDeviceDriver
from custom_components.omron.omron_ble.session import OmronDeviceSession

INDEX_ADDR = 0x0010
BASE = [0x0098, 0x06D8]
SLOTS = 100
SIZE = 0x10
NOW = dt.datetime(2026, 9, 20, 9, 0, 0)


def _config(seq_offsets=(0x08, 0x0C)) -> DeviceConfig:
    users = []
    for i, seq in enumerate(seq_offsets):
        user = {"write_cursor_offset": 2 * i, "unread_counter_offset": 4 + 2 * i,
                "write_cursor_mask": 0xFF, "slot_index_min": 0,
                "slot_index_max": SLOTS - 1, "slot_index_bias": -1}
        if seq is not None:
            user["latest_sequence_offset"] = seq
        users.append(user)
    return DeviceConfig(
        model="HEM-7361T",
        endianness=Endianness.LITTLE,
        user_start_addresses=BASE[: len(users)],
        per_user_records_count=[SLOTS] * len(users),
        record_byte_size=SIZE,
        settings_read_address=INDEX_ADDR,
        index_pointer_layout={
            "index_region_byte_size": 0x10, "endianness": "little", "users": users,
        },
    )


def _record(seq: int, minute: int, sys: int = 120) -> bytearray:
    when = NOW.replace(hour=8) + dt.timedelta(minutes=minute)
    flags1 = when.hour | (when.day << 5) | (when.month << 10)
    flags2 = when.second | (when.minute << 6) | (1 << 12)
    raw = bytearray(b"\xff" * SIZE)
    raw[0:3] = bytes([sys - 25, 80, 60])
    raw[3] = when.year - 2000
    raw[4:8] = bytes([flags1 & 0xFF, flags1 >> 8, flags2 & 0xFF, flags2 >> 8])
    raw[8:10] = b"\x00\x00"
    raw[10:12] = (seq & 0xFFFF).to_bytes(2, "little")
    return raw


class Cuff:
    """A fake cuff: records are numbered by sequence, stored at seq % SLOTS."""

    def __init__(self, config, *, newest=(0, None)):
        self.config = config
        self.newest = list(newest)  # per user newest sequence
        self.reads: list[int] = []
        self.fail_at: int | None = None
        self.cleared = set()

    def slot_of(self, seq):
        return (seq - 1) % SLOTS

    def memory(self, addr, size, block=0x10):
        self.reads.append(addr)
        if addr == INDEX_ADDR:
            raw = bytearray(0x10)
            for i, newest in enumerate(self.newest):
                if newest is None:
                    continue
                cursor = (self.slot_of(newest) + 1) % SLOTS
                raw[2 * i:2 * i + 2] = cursor.to_bytes(2, "little")
                raw[8 + 4 * i:10 + 4 * i] = (newest & 0xFFFF).to_bytes(2, "little")
            return raw
        if self.fail_at is not None and len(self.reads) > self.fail_at:
            raise ConnectionError("link lost")
        user = 1 if addr >= BASE[1] else 0
        slot = (addr - BASE[user]) // SIZE
        newest = self.newest[user]
        for seq in range(newest, max(newest - SLOTS, 0), -1):
            if self.slot_of(seq) == slot:
                return _record(seq, seq % 1000, sys=100 + seq % 100)
        return bytearray(b"\xff" * SIZE)


def _sync(cuff, last, max_reads=30):
    driver = OmronDeviceDriver(cuff.config)
    driver._now_func = lambda: NOW + dt.timedelta(days=1)
    transport = OmronDeviceSession(MagicMock(), cuff.config)
    transport.memory.read_memory_range = AsyncMock(side_effect=cuff.memory)
    return asyncio.run(driver.get_records_since(transport, last, max_reads=max_reads))


def _seqs(out, user=1):
    return [r["sequence"] for r in out[user].records]


def test_first_sync_only_notes_the_counter():
    cuff = Cuff(_config(), newest=(1643, 13))
    out = _sync(cuff, {})
    assert out[1].records == [] and out[1].next_seq == 1643
    assert out[2].next_seq == 13
    assert cuff.reads == [INDEX_ADDR]


def test_nothing_new_reads_only_the_index():
    cuff = Cuff(_config(), newest=(50, 7))
    out = _sync(cuff, {1: 50, 2: 7})
    assert out[1].records == [] and out[1].next_seq == 50
    assert cuff.reads == [INDEX_ADDR]


def test_new_records_come_back_oldest_first_with_one_read_each():
    cuff = Cuff(_config(), newest=(53, 7))
    out = _sync(cuff, {1: 50, 2: 7})
    assert _seqs(out) == [51, 52, 53]
    assert out[1].next_seq == 53 and out[1].complete and out[1].lost == 0
    assert [r["user"] for r in out[1].records] == [1, 1, 1]
    assert len(cuff.reads) == 1 + 3
    times = [r["datetime"] for r in out[1].records]
    assert times == sorted(times)


def test_second_user_is_read_from_its_own_region():
    cuff = Cuff(_config(), newest=(10, 12))
    out = _sync(cuff, {1: 10, 2: 10})
    assert _seqs(out, 2) == [11, 12]
    assert out[2].records[0]["user"] == 2


def test_wrap_across_the_ring_end():
    cuff = Cuff(_config(), newest=(102, 0))
    out = _sync(cuff, {1: 98, 2: 0})
    assert _seqs(out) == [99, 100, 101, 102]


def test_big_endian_counter_word_uses_its_low_sixteen_bits():
    config = _config()
    config.index_pointer_layout["endianness"] = "big"
    cuff = Cuff(config, newest=(0, 0))
    driver = OmronDeviceDriver(config)
    transport = OmronDeviceSession(MagicMock(), config)
    index = bytearray(0x10)
    index[0:2] = (6).to_bytes(2, "big")  # cursor: next slot 6 -> newest slot 5
    index[8:12] = (0xC0000000 | 6).to_bytes(4, "big")  # flags set, counter 6

    async def memory(addr, size, block=0x10):
        if addr == INDEX_ADDR:
            return index
        slot = (addr - BASE[0]) // SIZE
        return _record(slot + 1, slot + 1)

    transport.memory.read_memory_range = AsyncMock(side_effect=memory)
    out = asyncio.run(driver.get_records_since(transport, {1: 4}, max_reads=30))
    assert out[1].index_seq == 6 and _seqs(out) == [5, 6]
    del cuff


def test_sixteen_bit_counter_wrap():
    cuff = Cuff(_config(), newest=(0x0002, 0))
    cuff.newest[0] = 0x10002  # slots keep counting; the field stores 16 bits
    out = _sync(cuff, {1: 0xFFFF, 2: 0})
    assert out[1].next_seq == 0x0002
    assert len(out[1].records) == 3


def test_more_new_than_the_ring_holds_counts_the_loss():
    cuff = Cuff(_config(), newest=(250, 0))
    out = _sync(cuff, {1: 100, 2: 0}, max_reads=200)
    assert out[1].lost == 50
    assert len(out[1].records) == SLOTS
    assert out[1].next_seq == 250 and out[1].complete


def test_max_reads_cuts_the_backlog_and_the_next_poll_resumes():
    cuff = Cuff(_config(), newest=(60, 0))
    first = _sync(cuff, {1: 40, 2: 0}, max_reads=5)
    assert _seqs(first) == [41, 42, 43, 44, 45]
    assert first[1].next_seq == 45 and not first[1].complete
    second = _sync(cuff, {1: first[1].next_seq, 2: 0}, max_reads=30)
    assert _seqs(second) == list(range(46, 61))
    assert second[1].complete


def test_failed_read_keeps_what_was_read_and_stops():
    cuff = Cuff(_config(), newest=(60, 0))
    cuff.fail_at = 3  # index + two records, then the link drops
    out = _sync(cuff, {1: 50, 2: 0})
    assert _seqs(out) == [51, 52]
    assert out[1].next_seq == 52 and not out[1].complete


def test_counter_going_backwards_rebases_without_events():
    cuff = Cuff(_config(), newest=(5, 0))
    out = _sync(cuff, {1: 900, 2: 0})
    assert out[1].rebased and out[1].records == [] and out[1].next_seq == 5


def test_users_without_a_sequence_field_are_left_out():
    cuff = Cuff(_config(seq_offsets=(0x08, None)), newest=(3, 3))
    out = _sync(cuff, {1: 1, 2: 1})
    assert set(out) == {1}


def test_profile_without_the_field_does_nothing():
    cuff = Cuff(_config(seq_offsets=(None, None)), newest=(3, 3))
    assert _sync(cuff, {1: 1}) == {}


OPTED_IN = {
    "HEM-7361T", "HEM-7342T", "HEM-7155T", "HEM-7155T-K4", "HEM-1026T2",
    "HEM-7153JT", "HEM-7150T", "HEM-7157T", "HEM-7151T", "HEM-7142T2",
    "HEM-6161T", "HEM-6231T", "HEM-6231T_Z", "HEM-7530T", "HEM-7136T", "HEM-6232T",
}


def test_only_the_listed_blood_pressure_profiles_opt_in():
    from custom_components.omron.omron_ble.device_catalog import CANONICAL_DEVICE_PROFILES

    opted = {m for m, c in CANONICAL_DEVICE_PROFILES.items() if c.supports_stored_record_sync}
    assert opted == OPTED_IN
    for model in opted:
        config = CANONICAL_DEVICE_PROFILES[model]
        layout = config.index_pointer_layout
        assert config.is_blood_pressure
        offsets = [u.get("latest_sequence_offset") for u in layout["users"]]
        assert offsets == [8, 12][: len(offsets)]


def test_sequence_field_outside_the_index_region_is_rejected():
    with pytest.raises(ValueError, match="latest sequence"):
        _config(seq_offsets=(0x0F, None))


# --- the Home Assistant side -------------------------------------------------


def _entry_and_hass(pending, last_seq=None):
    device_data = SimpleNamespace(
        pending_history=pending, history_last_seq=last_seq, history_shown={}
    )
    entry = SimpleNamespace(
        entry_id="e1",
        runtime_data=SimpleNamespace(address="AA:BB:CC:DD:EE:FF", device_data=device_data),
    )
    hass = MagicMock()
    return hass, entry, device_data


def test_publish_fires_oldest_first_then_saves_the_position():
    order = []
    store = MagicMock()
    store.async_save = AsyncMock(side_effect=lambda d: order.append(("save", d)))
    pending = {
        "next_seq": {1: 3},
        "events": [{"user": 1, "sequence": 2}, {"user": 1, "sequence": 3}],
        "shown": {1: 3},
    }
    hass, entry, device_data = _entry_and_hass(pending, {2: 9})
    hass.bus.async_fire.side_effect = lambda name, data: order.append(("fire", data["sequence"]))
    with patch.object(history, "_store", return_value=store), patch.object(
        history.dr, "async_get"
    ) as registry:
        registry.return_value.async_get_device.return_value = SimpleNamespace(id="dev")
        asyncio.run(history.async_publish_history(hass, entry))
    assert order[:2] == [("fire", 2), ("fire", 3)]
    assert device_data.history_last_seq == {2: 9, 1: 3}
    assert device_data.pending_history is None
    assert device_data.history_shown == {1: 3}
    assert order[-1] == ("save", {"last_seq": {"2": 9, "1": 3}, "shown": {"1": 3}})
    assert hass.bus.async_fire.call_args.args[0] == "omron_measurement"
    assert hass.bus.async_fire.call_args.args[1]["device_id"] == "dev"


def test_publish_without_findings_does_nothing():
    hass, entry, _ = _entry_and_hass(None)
    asyncio.run(history.async_publish_history(hass, entry))
    hass.bus.async_fire.assert_not_called()


def test_first_poll_saves_the_baseline_without_events():
    store = MagicMock()
    store.async_save = AsyncMock()
    hass, entry, device_data = _entry_and_hass({"next_seq": {1: 1643, 2: 13}, "events": []}, {})
    with patch.object(history, "_store", return_value=store), patch.object(
        history.dr, "async_get"
    ):
        asyncio.run(history.async_publish_history(hass, entry))
    hass.bus.async_fire.assert_not_called()
    store.async_save.assert_awaited_once_with(
        {"last_seq": {"1": 1643, "2": 13}, "shown": {}}
    )
    assert device_data.history_last_seq == {1: 1643, 2: 13}


def test_stored_state_loads_tolerantly():
    store = MagicMock()
    store.async_load = AsyncMock(
        return_value={"last_seq": {"1": 5, "x": 1, "2": None}, "shown": {"1": 7}}
    )
    with patch.object(history, "_store", return_value=store):
        assert asyncio.run(history.async_load_history_state(MagicMock(), "e1")) == (
            {1: 5}, {1: 7},
        )
    store.async_load = AsyncMock(return_value=None)
    with patch.object(history, "_store", return_value=store):
        assert asyncio.run(history.async_load_history_state(MagicMock(), "e1")) == ({}, {})


def test_each_older_record_gets_its_own_state_change():
    coordinator = SimpleNamespace(data="d0")
    seen = []
    coordinator.async_set_updated_data = lambda d: (seen.append(d), setattr(coordinator, "data", d))
    asyncio.run(
        history.async_push_history_updates(
            coordinator, ["u1", "u2"], lambda prev, new: f"{prev}+{new}"
        )
    )
    assert seen == ["d0+u1", "d0+u1+u2"]


# --- the parser --------------------------------------------------------------


def _parser_with(outcomes):
    from custom_components.omron.omron_ble import OmronBluetoothDeviceData

    data = OmronBluetoothDeviceData(device_model="HEM-7361T", user_aliases={1: "Ann"})
    data.history_last_seq = {1: 1}
    data._driver.get_records_since = AsyncMock(return_value=outcomes)
    trace = MagicMock()
    trace.timed.return_value.__enter__ = MagicMock()
    trace.timed.return_value.__exit__ = MagicMock(return_value=False)
    return data, trace


def _stored(user, seq, minute, sys):
    return {
        "user": user, "sequence": seq, "sys": sys, "dia": 80, "bpm": 60,
        "datetime": NOW.replace(hour=8) + dt.timedelta(minutes=minute),
        "ihb": 0, "mov": 1, "cuff": 0, "pos": 0,
    }


def test_parser_turns_the_backlog_into_updates_and_events():
    from custom_components.omron.omron_ble.driver import StoredUserRecords

    records = [_stored(1, 2, 1, 118), _stored(1, 3, 2, 121), _stored(1, 4, 3, 125)]
    data, trace = _parser_with({1: StoredUserRecords(next_seq=4, index_seq=4, records=records)})
    asyncio.run(data._collect_stored_history(MagicMock(), trace))

    # The newest is the poll's own result; the two before it get an update each.
    assert len(data.history_updates) == 2
    systolic = [
        next(v.native_value for k, v in u.entity_values.items() if k.key.startswith("blood_pressure_systolic"))
        for u in data.history_updates
    ]
    assert systolic == [118, 121]
    # No other-user placeholder may ride along and blank that user's sensors.
    assert all(
        v.native_value is not None
        for u in data.history_updates for v in u.entity_values.values()
    )
    events = data.pending_history["events"]
    assert [e["sequence"] for e in events] == [2, 3, 4]
    assert events[0]["user_alias"] == "Ann"
    assert dt.datetime.fromisoformat(events[0]["measured_at"]).tzinfo is not None
    assert events[0]["device_time"] == "2026-09-20T08:01:00"
    assert events[0]["body_movement"] is True and events[0]["irregular_pulse"] is False
    assert data.pending_history["next_seq"] == {1: 4}


def test_parser_cut_short_read_publishes_every_record_it_got():
    from custom_components.omron.omron_ble.driver import StoredUserRecords

    records = [_stored(1, 2, 1, 118), _stored(1, 3, 2, 121)]
    data, trace = _parser_with(
        {1: StoredUserRecords(next_seq=3, index_seq=9, records=records, complete=False)}
    )
    asyncio.run(data._collect_stored_history(MagicMock(), trace))
    assert len(data.history_updates) == 2
    assert data.pending_history["next_seq"] == {1: 3}


def test_parser_history_failure_is_forgiven():
    data, trace = _parser_with({})
    data._driver.get_records_since = AsyncMock(side_effect=ConnectionError("gone"))
    asyncio.run(data._collect_stored_history(MagicMock(), trace))
    trace.forgive.assert_called_once_with("history")
    assert data.pending_history is None and data.history_updates == []


def test_single_user_profile_publishes_backlog_without_a_user_suffix():
    from custom_components.omron.omron_ble import OmronBluetoothDeviceData
    from custom_components.omron.omron_ble.driver import StoredUserRecords

    data = OmronBluetoothDeviceData(device_model="HEM-7142T2")
    data.history_last_seq = {1: 1}
    records = [_stored(1, 2, 1, 118), _stored(1, 3, 2, 121)]
    data._driver.get_records_since = AsyncMock(
        return_value={1: StoredUserRecords(next_seq=3, index_seq=3, records=records)}
    )
    trace = MagicMock()
    trace.timed.return_value.__enter__ = MagicMock()
    trace.timed.return_value.__exit__ = MagicMock(return_value=False)
    asyncio.run(data._collect_stored_history(MagicMock(), trace))
    assert len(data.history_updates) == 1
    keys = {k.key for k in data.history_updates[0].entity_values}
    assert "blood_pressure_systolic" in keys


def _collect(data, outcome):
    trace = MagicMock()
    trace.timed.return_value.__enter__ = MagicMock()
    trace.timed.return_value.__exit__ = MagicMock(return_value=False)
    data._driver.get_records_since = AsyncMock(return_value=outcome)
    asyncio.run(data._collect_stored_history(MagicMock(), trace))


def test_a_retried_readout_does_not_double_the_backlog():
    from custom_components.omron.omron_ble.driver import StoredUserRecords

    records = [_stored(1, 2, 1, 118), _stored(1, 3, 2, 121), _stored(1, 4, 3, 125)]
    outcome = {1: StoredUserRecords(next_seq=4, index_seq=4, records=records)}
    data, _ = _parser_with(outcome)
    _collect(data, outcome)
    _collect(data, outcome)
    assert len(data.history_updates) == 2
    assert len(data.pending_history["events"]) == 3


def test_a_drain_spanning_polls_never_moves_the_sensors_back():
    from custom_components.omron.omron_ble.driver import StoredUserRecords

    data, _ = _parser_with({})
    # Poll 1: the cuff is at 45 but only 41-43 were read; they get states and
    # the poll then shows 45.
    first = [_stored(1, n, n, 100 + n) for n in (41, 42, 43)]
    _collect(data, {1: StoredUserRecords(next_seq=43, index_seq=45, records=first, complete=False)})
    assert len(data.history_updates) == 3
    data.history_shown.update(data.pending_history["shown"])
    assert data.history_shown == {1: 45}
    # Poll 2 finishes the drain: 44 is behind what is already shown, so it
    # only gets its event; 45 is the poll's own.
    second = [_stored(1, n, n, 100 + n) for n in (44, 45)]
    _collect(data, {1: StoredUserRecords(next_seq=45, index_seq=45, records=second)})
    assert data.history_updates == []
    assert [e["sequence"] for e in data.pending_history["events"]] == [44, 45]


def test_an_unreadable_newest_record_does_not_hide_the_one_before_it():
    from custom_components.omron.omron_ble.driver import StoredUserRecords

    records = [_stored(1, 2, 1, 118), _stored(1, 3, 2, 121)]
    data, _ = _parser_with({})
    _collect(data, {1: StoredUserRecords(next_seq=4, index_seq=4, records=records, lost=1)})
    assert len(data.history_updates) == 2


def test_truread_profiles_get_events_only():
    from custom_components.omron.omron_ble import OmronBluetoothDeviceData
    from custom_components.omron.omron_ble.driver import StoredUserRecords

    data = OmronBluetoothDeviceData(device_model="HEM-7342T")
    data.history_last_seq = {1: 1}
    records = [_stored(1, 2, 1, 118), _stored(1, 3, 2, 121), _stored(1, 4, 3, 125)]
    _collect(data, {1: StoredUserRecords(next_seq=4, index_seq=4, records=records)})
    assert data.history_updates == []
    assert len(data.pending_history["events"]) == 3


def test_a_rebase_restarts_the_shown_position():
    from custom_components.omron.omron_ble.driver import StoredUserRecords

    data, _ = _parser_with({})
    data.history_shown = {1: 500}
    _collect(data, {1: StoredUserRecords(next_seq=3, index_seq=3, rebased=True)})
    assert data.pending_history["shown"] == {1: 3}
    data.history_shown.update(data.pending_history["shown"])
    records = [_stored(1, n, n, 100 + n) for n in (4, 5, 6)]
    _collect(data, {1: StoredUserRecords(next_seq=6, index_seq=6, records=records)})
    assert len(data.history_updates) == 2


# --- the poll path ------------------------------------------------------------


def _poll_harness(monkeypatch, device_data):
    from contextlib import asynccontextmanager

    import custom_components.omron as omron_init

    calls = []
    coordinator = SimpleNamespace(data="d0")

    def set_data(value):
        calls.append(("state", value))
        coordinator.data = value

    coordinator.async_set_updated_data = set_data
    runtime = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        session_lock=asyncio.Lock(),
        force_poll_after_lock=False,
        history_lock=asyncio.Lock(),
        unloading=False,
        device_data=device_data,
        poll_coordinator=coordinator,
        readout_coordinator=SimpleNamespace(async_set_updated_data=lambda v: None),
    )
    entry = SimpleNamespace(entry_id="e1", runtime_data=runtime)
    monkeypatch.setattr(omron_init, "async_ble_device_from_address", lambda h, a: object())
    monkeypatch.setattr(omron_init, "adopt_handoff_session", lambda h, a: None)
    monkeypatch.setattr(omron_init, "_merge_poll_sensor_update", lambda prev, new: f"{prev}+{new}")

    @asynccontextmanager
    async def telemetry(hass, runtime, operation="poll"):
        yield

    monkeypatch.setattr(omron_init, "omron_poll_ble_telemetry", telemetry)
    hass = MagicMock()
    hass.bus.async_fire.side_effect = lambda name, data: calls.append(("fire", data["sequence"]))
    return omron_init, hass, entry, calls


class _PollingData:
    pending_credential = None
    last_readout_at = None

    def __init__(self, fail=False):
        self.fail = fail
        self.history_updates = ["u1", "u2"]
        self.pending_history = {
            "next_seq": {1: 3}, "events": [{"user": 1, "sequence": 2}, {"user": 1, "sequence": 3}],
            "shown": {1: 3},
        }
        self.history_last_seq = {}
        self.history_shown = {}

    async def async_poll(self, device, preconnected_session=None):
        if self.fail:
            raise RuntimeError("link lost")
        return "latest"


def test_poll_pushes_backlog_states_then_events_then_saves_then_returns_latest(monkeypatch):
    data = _PollingData()
    omron_init, hass, entry, calls = _poll_harness(monkeypatch, data)
    store = MagicMock()
    store.async_save = AsyncMock(side_effect=lambda d: calls.append(("save", d)))
    with patch.object(history, "_store", return_value=store), patch.object(history.dr, "async_get"):
        result = asyncio.run(omron_init.async_poll_data(hass, entry))
    assert calls[:2] == [("state", "d0+u1"), ("state", "d0+u1+u2")]
    assert calls[2:4] == [("fire", 2), ("fire", 3)]
    assert calls[4][0] == "save"
    assert result == "d0+u1+u2+latest"
    assert data.history_updates == [] and data.history_last_seq == {1: 3}


def test_failed_poll_pushes_nothing_and_saves_nothing(monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    data = _PollingData(fail=True)
    omron_init, hass, entry, calls = _poll_harness(monkeypatch, data)
    store = MagicMock()
    store.async_save = AsyncMock()
    with patch.object(history, "_store", return_value=store), pytest.raises(UpdateFailed):
        asyncio.run(omron_init.async_poll_data(hass, entry))
    assert calls == [] and data.history_last_seq == {}
    store.async_save.assert_not_called()


def test_an_unchanged_position_is_not_rewritten():
    store = MagicMock()
    store.async_save = AsyncMock()
    pending = {"next_seq": {1: 5}, "events": [], "shown": {1: 5}}
    hass, entry, device_data = _entry_and_hass(pending, {1: 5})
    device_data.history_shown = {1: 5}
    with patch.object(history, "_store", return_value=store), patch.object(
        history.dr, "async_get"
    ):
        asyncio.run(history.async_publish_history(hass, entry))
    store.async_save.assert_not_called()


# --- switching it on and off --------------------------------------------------


def _setup(supported, enabled, stored=None):
    data = SimpleNamespace(history_last_seq=None, history_shown={})
    store = MagicMock()
    store.async_load = AsyncMock(return_value=stored)
    store.async_remove = AsyncMock()
    with patch.object(history, "_store", return_value=store):
        asyncio.run(
            history.async_setup_history(
                MagicMock(), "e1", data, supported=supported, enabled=enabled
            )
        )
    return data, store


def test_off_leaves_the_parser_alone_and_drops_the_saved_position():
    data, store = _setup(True, False, {"last_seq": {"1": 5}, "shown": {"1": 5}})
    assert data.history_last_seq is None and data.history_shown == {}
    store.async_load.assert_not_called()
    store.async_remove.assert_awaited_once()


def test_on_loads_the_saved_position():
    data, store = _setup(True, True, {"last_seq": {"1": 5}, "shown": {"1": 4}})
    assert data.history_last_seq == {1: 5} and data.history_shown == {1: 4}
    store.async_remove.assert_not_called()


def test_unsupported_profiles_are_never_touched():
    data, store = _setup(False, True)
    assert data.history_last_seq is None
    store.async_load.assert_not_called()
    store.async_remove.assert_not_called()


def _readout(history_last_seq):
    from custom_components.omron.omron_ble import OmronBluetoothDeviceData
    from custom_components.omron.omron_ble import parser as parser_module
    from custom_components.omron.omron_ble.session_trace import SessionTrace

    data = OmronBluetoothDeviceData(device_model="HEM-7361T")
    data.history_last_seq = history_last_seq
    data._driver.write_user_profile = AsyncMock()
    data._driver.get_latest_records_per_user = AsyncMock(return_value={})
    data._driver.get_records_since = AsyncMock(return_value={})
    session = MagicMock()
    session.trace = SessionTrace()
    session.memory.commit_pairing_registration = AsyncMock()
    client = MagicMock()
    client.services.get_characteristic.return_value = None
    with patch.object(parser_module, "async_sync_eeprom_time", AsyncMock()):
        asyncio.run(
            data._poll_device_readout(
                session, client, SimpleNamespace(address="AA:BB"), memory_session_active=True
            )
        )
    return data


def test_a_readout_reads_no_history_until_the_host_asks():
    data = _readout(None)
    data._driver.get_records_since.assert_not_called()
    assert data.pending_history is None and data.history_updates == []


def test_a_readout_reads_history_once_the_host_asks():
    data = _readout({})
    data._driver.get_records_since.assert_awaited_once()


def test_a_poll_that_started_before_a_reload_does_not_write_the_position_back(monkeypatch):
    data = _PollingData()
    omron_init, hass, entry, calls = _poll_harness(monkeypatch, data)
    entry.runtime_data.unloading = True
    store = MagicMock()
    store.async_save = AsyncMock()
    with patch.object(history, "_store", return_value=store), patch.object(history.dr, "async_get"):
        asyncio.run(omron_init.async_poll_data(hass, entry))
    assert calls == [] and data.history_last_seq == {}
    store.async_save.assert_not_called()


def test_unload_waits_for_a_poll_that_is_saving_the_position(monkeypatch):
    import custom_components.omron as omron_init

    runtime = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        unloading=False,
        history_lock=asyncio.Lock(),
        background_tasks=set(),
        pending_forced_transfer_task=None,
    )
    entry = SimpleNamespace(runtime_data=runtime)
    hass = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    monkeypatch.setattr(omron_init, "discard_handoff_session", AsyncMock())
    monkeypatch.setattr(omron_init, "discard_probe_session", AsyncMock())

    async def scenario():
        async with runtime.history_lock:
            unload = asyncio.create_task(omron_init.async_unload_entry(hass, entry))
            for _ in range(5):
                await asyncio.sleep(0)
            assert runtime.unloading and not unload.done()
        assert await unload is True

    asyncio.run(scenario())
