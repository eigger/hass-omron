"""TruRead sequence collection on the index read path (#58, #193).

A TruRead session is stored as three consecutive slots tagged pos=1, 2, 3;
the monitor displays only their average. The index probe has to reach back
past the cursor to rebuild it, but only on models that opt in and only while
the slots still count down 3 → 2 → 1.
"""
import asyncio
import datetime as dt
from unittest.mock import AsyncMock, MagicMock

from custom_components.omron.omron_ble.devices import DeviceConfig, Endianness
from custom_components.omron.omron_ble.driver import OmronDeviceDriver
from custom_components.omron.omron_ble.session import OmronDeviceSession

INDEX_ADDR = 0x0010
USER1_BASE = 0x01C4
SLOTS = 100
RECORD_SIZE = 0x10
NOW = dt.datetime(2026, 9, 20, 9, 0, 0)


def _record(sys: int, dia: int, bpm: int, when: dt.datetime, pos: int = 0) -> bytearray:
    """Encode a classic 14-byte vital record (byte-aligned layout) into a 0x10 slot."""
    flags1 = when.hour | (when.day << 5) | (when.month << 10)
    flags2 = when.second | (when.minute << 6) | (1 << 12) | (pos << 14)
    raw = bytearray(b"\xff" * RECORD_SIZE)
    raw[0] = sys - 25
    raw[1] = dia
    raw[2] = bpm
    raw[3] = when.year - 2000
    raw[4] = flags1 & 0xFF
    raw[5] = flags1 >> 8
    raw[6] = flags2 & 0xFF
    raw[7] = flags2 >> 8
    raw[8:12] = b"\x00\x00\x00\x00"
    return raw


def _config(truread: bool) -> DeviceConfig:
    layout = {
        "index_region_byte_size": 0x18,
        "endianness": "little",
        "users": [
            {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF,
             "slot_index_min": 0, "slot_index_max": SLOTS - 1, "slot_index_bias": -1},
        ],
    }
    if truread:
        layout["truread_sequence"] = True
    return DeviceConfig(
        model="HEM-7380T1",
        endianness=Endianness.LITTLE,
        user_start_addresses=[USER1_BASE],
        per_user_records_count=[SLOTS],
        record_byte_size=RECORD_SIZE,
        settings_read_address=INDEX_ADDR,
        index_pointer_layout=layout,
    )


def _run(config: DeviceConfig, cursor_slot: int, slots: dict[int, bytearray]):
    """Probe with the cursor on ``cursor_slot``; return (per-user result, slots read)."""
    driver = OmronDeviceDriver(config)
    driver._now_func = lambda: NOW
    transport = OmronDeviceSession(MagicMock(), config)
    transport.unlock = AsyncMock()

    index_bytes = bytearray(0x18)
    # slot_index_bias=-1: the cursor holds the *next* write position.
    index_bytes[0:2] = (cursor_slot + 1).to_bytes(2, "little")
    probed: list[int] = []

    async def fake_read(addr, size, block_size=0x10):
        if addr == INDEX_ADDR:
            return index_bytes
        slot = (addr - USER1_BASE) // RECORD_SIZE
        probed.append(slot)
        return slots.get(slot, bytearray(b"\xff" * RECORD_SIZE))

    transport.read_memory_range = AsyncMock(side_effect=fake_read)
    result, _empty = asyncio.run(driver._get_latest_via_index(transport, return_all_users=True))
    return result, probed


def _session(first_slot: int, start: dt.datetime) -> dict[int, bytearray]:
    """Three TruRead sub-measurements one minute apart, starting at first_slot."""
    return {
        first_slot % SLOTS: _record(120, 88, 70, start, pos=1),
        (first_slot + 1) % SLOTS: _record(119, 85, 74, start + dt.timedelta(minutes=1), pos=2),
        (first_slot + 2) % SLOTS: _record(118, 82, 71, start + dt.timedelta(minutes=2), pos=3),
    }


class TestTruReadIndexSequence:
    def test_opted_in_model_reports_average(self):
        start = NOW - dt.timedelta(hours=1)
        result, probed = _run(_config(True), 22, _session(20, start))

        rec = result[1]
        assert rec["measurement_type"] == "TruRead Average"
        assert (rec["sys"], rec["dia"], rec["bpm"]) == (119, 85, 72)
        assert rec["pos"] == 0
        assert [d["pos"] for d in rec["truread_details"]] == [1, 2, 3]
        assert probed == [22, 21, 20]

    def test_sequence_across_ring_wrap(self):
        # Slots 98, 99, 0: the cursor sits on slot 0 and the older two are at
        # the top of the ring. Slot numbers restart, but probe order does not.
        start = NOW - dt.timedelta(hours=1)
        result, probed = _run(_config(True), 0, _session(98, start))

        assert result[1]["measurement_type"] == "TruRead Average"
        assert probed == [0, 99, 98]

    def test_single_measurement_reads_one_slot(self):
        # A Single at the cursor (pos=0) must not trigger the extra reads,
        # even when an older TruRead session sits just behind it.
        start = NOW - dt.timedelta(hours=2)
        slots = _session(20, start)
        slots[23] = _record(130, 90, 65, NOW - dt.timedelta(hours=1))
        result, probed = _run(_config(True), 23, slots)

        rec = result[1]
        assert rec["measurement_type"] == "Single"
        assert (rec["sys"], rec["dia"], rec["bpm"]) == (130, 90, 65)
        assert probed == [23]

    def test_cursor_record_wins_over_older_later_timestamp(self):
        # Device clock was set back: the slot behind the cursor carries a
        # later timestamp. The cursor slot is still the newest measurement.
        slots = {
            21: _record(140, 95, 80, NOW - dt.timedelta(minutes=10), pos=2),
            22: _record(125, 85, 70, NOW - dt.timedelta(minutes=70), pos=3),
        }
        result, probed = _run(_config(True), 22, slots)

        rec = result[1]
        assert rec["measurement_type"] == "Single"
        assert rec["sys"] == 125
        assert probed == [22, 21, 20]

    def test_partial_sequence_falls_back_to_cursor(self):
        # Polled mid-session: pos=3 at the cursor but pos=1 is missing.
        slots = {
            21: _record(119, 85, 74, NOW - dt.timedelta(minutes=2), pos=2),
            22: _record(118, 82, 71, NOW - dt.timedelta(minutes=1), pos=3),
        }
        result, probed = _run(_config(True), 22, slots)

        assert result[1]["measurement_type"] == "Single"
        assert result[1]["sys"] == 118
        assert probed == [22, 21, 20]

    def test_model_without_flag_keeps_single_read(self):
        start = NOW - dt.timedelta(hours=1)
        result, probed = _run(_config(False), 22, _session(20, start))

        rec = result[1]
        assert rec["measurement_type"] == "Single"
        assert rec["sys"] == 118
        assert probed == [22]
