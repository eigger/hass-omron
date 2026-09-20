"""Unit tests for empty user clear_value handling in Omron BLE index readout.

A cursor equal to clear_value (0x8000) usually means the user has never
recorded anything, but the same word is also a live pointer (bit 15 flag +
low byte wrapped to 0x00), so the driver reads the cursor slot once: all-0xFF
confirms the user empty, a record means the pointer is live.
"""
import asyncio
import datetime as dt
from unittest.mock import AsyncMock, MagicMock

from custom_components.omron.omron_ble.devices import DeviceConfig, Endianness
from custom_components.omron.omron_ble.driver import OmronDeviceDriver
from custom_components.omron.omron_ble.session import OmronDeviceSession


class TestEmptyUserClearValue:
    def test_little_endian_empty_user2_skipped_with_clear_value(self):
        # 2-user little-endian config (e.g. HEM-7155T-MW3 / HEM-7155T_ESL1)
        config = DeviceConfig(
            model="HEM-7155T-MW3",
            endianness=Endianness.LITTLE,
            user_start_addresses=[0x02E8, 0x06A8],
            per_user_records_count=[60, 60],
            record_byte_size=0x10,
            settings_read_address=0x0260,
            index_pointer_layout={
                "index_region_byte_size": 0x10,
                "endianness": "little",
                "users": [
                    {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1, "clear_value": 0x8000},
                    {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1, "clear_value": 0x8000},
                ],
            },
        )
        driver = OmronDeviceDriver(config)
        transport = OmronDeviceSession(MagicMock(), config)
        transport.unlock = AsyncMock()

        # User 1: cursor = 0x0004 (slot 3), User 2: cursor = 0x8000 (clear_value, no records)
        # In little-endian:
        # offset 0x00: 04 00 ... (cursor 4)
        # offset 0x02: 00 80 ... (cursor 0x8000)
        index_bytes = bytearray(16)
        index_bytes[0:2] = b"\x04\x00"
        index_bytes[2:4] = b"\x00\x80"

        read_calls = []

        async def fake_read_memory_range(addr, size, block_size=0x10):
            read_calls.append((addr, size))
            if addr == 0x0260:
                return index_bytes
            if addr >= 0x06A8:
                # User 2 has never recorded: slot is the 0xFF empty marker.
                return bytearray(b"\xff" * 16)
            # Return valid record for user 1 probe
            # record format: timestamp, sys=120, dia=80, bpm=70...
            rec = bytearray(16)
            rec[0] = 0x01  # some non-FF data
            return rec

        transport.read_memory_range = AsyncMock(side_effect=fake_read_memory_range)

        records, empty_users = asyncio.run(
            driver._get_latest_via_index(transport, return_all_users=True)
        )

        # User 2 is confirmed empty after a single verification read of the
        # cursor slot (slot 59 at 0x06A8 + 59*16); no backtrack past it.
        assert 2 in empty_users
        user2_reads = [addr for addr, _ in read_calls if addr >= 0x06A8]
        assert user2_reads == [0x06A8 + 59 * 16]

    def test_big_endian_empty_user2_skipped_with_clear_value(self):
        # 2-user big-endian config (e.g. HEM-7320T / HEM-7322T)
        config = DeviceConfig(
            model="HEM-7320T",
            endianness=Endianness.BIG,
            user_start_addresses=[0x02AC, 0x05F4],
            per_user_records_count=[60, 60],
            record_byte_size=0x0E,
            settings_read_address=0x0260,
            index_pointer_layout={
                "index_region_byte_size": 0x08,
                "endianness": "big",
                "users": [
                    {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1, "clear_value": 0x8000},
                    {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1, "clear_value": 0x8000},
                ],
            },
        )
        driver = OmronDeviceDriver(config)
        transport = OmronDeviceSession(MagicMock(), config)
        transport.unlock = AsyncMock()

        # Big-endian:
        # offset 0x00: 00 05 (cursor 5)
        # offset 0x02: 80 00 (cursor 0x8000)
        index_bytes = bytearray(8)
        index_bytes[0:2] = b"\x00\x05"
        index_bytes[2:4] = b"\x80\x00"

        read_calls = []

        async def fake_read_memory_range(addr, size, block_size=0x10):
            read_calls.append((addr, size))
            if addr == 0x0260:
                return index_bytes
            if addr >= 0x05F4:
                return bytearray(b"\xff" * 14)
            rec = bytearray(14)
            rec[0] = 0x01
            return rec

        transport.read_memory_range = AsyncMock(side_effect=fake_read_memory_range)

        records, empty_users = asyncio.run(
            driver._get_latest_via_index(transport, return_all_users=True)
        )

        assert 2 in empty_users
        user2_reads = [addr for addr, _ in read_calls if addr >= 0x05F4]
        assert user2_reads == [0x05F4 + 59 * 14]

    def test_default_clear_value_without_explicit_key(self):
        # Verify that when user_cfg does not specify "clear_value", the default 0x8000 is used
        config = DeviceConfig(
            model="HEM-7155T",
            endianness=Endianness.LITTLE,
            user_start_addresses=[0x0098, 0x0458],
            per_user_records_count=[60, 60],
            record_byte_size=0x10,
            settings_read_address=0x0010,
            index_pointer_layout={
                "index_region_byte_size": 0x10,
                "endianness": "little",
                "users": [
                    {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
                    {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
                ],
            },
        )
        driver = OmronDeviceDriver(config)
        transport = OmronDeviceSession(MagicMock(), config)
        transport.unlock = AsyncMock()

        index_bytes = bytearray(16)
        index_bytes[0:2] = b"\x01\x00"  # User 1: cursor 1
        index_bytes[2:4] = b"\x00\x80"  # User 2: cursor 0x8000 (clear_value)

        read_calls = []

        async def fake_read_memory_range(addr, size, block_size=0x10):
            read_calls.append((addr, size))
            if addr == 0x0010:
                return index_bytes
            if addr >= 0x0458:
                return bytearray(b"\xff" * 16)
            rec = bytearray(16)
            rec[0] = 0x01
            return rec

        transport.read_memory_range = AsyncMock(side_effect=fake_read_memory_range)

        records, empty_users = asyncio.run(
            driver._get_latest_via_index(transport, return_all_users=True)
        )

        assert 2 in empty_users
        user2_reads = [addr for addr, _ in read_calls if addr >= 0x0458]
        assert user2_reads == [0x0458 + 59 * 16]

    def test_clear_value_with_live_record_is_not_empty(self):
        # HEM-7380T1 (#193): bit 15 toggles between writes and the low byte
        # reads 0x00 for slot 99, which this cuff reuses routinely, so a live
        # cursor can read exactly 0x8000. The TruRead session ending at slot
        # 99 (the pattern seen twice in the #193 log) must still come out as
        # the average, and the user must not be marked empty (that would
        # also skip the full scan).
        config = DeviceConfig(
            model="HEM-7380T1",
            endianness=Endianness.LITTLE,
            user_start_addresses=[0x01C4, 0x0804],
            per_user_records_count=[100, 100],
            record_byte_size=0x10,
            settings_read_address=0x0010,
            index_pointer_layout={
                "index_region_byte_size": 0x18,
                "endianness": "little",
                "truread_sequence": True,
                "users": [
                    {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
                    {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
                ],
            },
        )
        driver = OmronDeviceDriver(config)
        now = dt.datetime(2026, 9, 20, 14, 0, 0)
        driver._now_func = lambda: now
        transport = OmronDeviceSession(MagicMock(), config)
        transport.unlock = AsyncMock()

        index_bytes = bytearray(0x18)
        index_bytes[0:2] = b"\x00\x80"  # User 1: live cursor that collides with clear_value
        index_bytes[2:4] = b"\x00\x80"  # User 2: genuinely unrecorded

        def record(sys, dia, bpm, when, pos):
            flags1 = when.hour | (when.day << 5) | (when.month << 10)
            flags2 = when.second | (when.minute << 6) | (1 << 12) | (pos << 14)
            rec = bytearray(b"\xff" * 16)
            rec[0:4] = bytes([sys - 25, dia, bpm, when.year - 2000])
            rec[4:8] = bytes([flags1 & 0xFF, flags1 >> 8, flags2 & 0xFF, flags2 >> 8])
            rec[8:12] = b"\x00\x00\x00\x00"
            return rec

        end = now - dt.timedelta(minutes=27)
        slots = {
            97: record(120, 88, 70, end - dt.timedelta(minutes=2, seconds=19), 1),
            98: record(119, 85, 74, end - dt.timedelta(minutes=1, seconds=10), 2),
            99: record(118, 82, 71, end, 3),
        }

        read_calls = []

        async def fake_read_memory_range(addr, size, block_size=0x10):
            read_calls.append(addr)
            if addr == 0x0010:
                return index_bytes
            if 0x01C4 <= addr < 0x0804:
                return slots.get((addr - 0x01C4) // 16, bytearray(b"\xff" * 16))
            return bytearray(b"\xff" * 16)

        transport.read_memory_range = AsyncMock(side_effect=fake_read_memory_range)

        records, empty_users = asyncio.run(
            driver._get_latest_via_index(transport, return_all_users=True)
        )

        assert 1 not in empty_users
        assert records[1]["measurement_type"] == "TruRead Average"
        assert (records[1]["sys"], records[1]["dia"], records[1]["bpm"]) == (119, 85, 72)
        # The live pointer runs the normal probe: 99 → 98 → 97.
        assert [a for a in read_calls if 0x01C4 <= a < 0x0804] == [0x01C4 + s * 16 for s in (99, 98, 97)]
        # User 2 is confirmed empty after one 0xFF read, no reach-back.
        assert 2 in empty_users
        assert [a for a in read_calls if a >= 0x0804] == [0x0804 + 99 * 16]

    def test_live_clear_value_cursor_backtracks_past_gap(self):
        # A live 0x8000 cursor on a backtrack_slots=5 profile: the cursor
        # slot holds garbage, the slot behind it is 0xFF, and a valid record
        # sits two back. The empty early-exit must only fire when *every*
        # slot so far was 0xFF, so the gap must not stop the backtrack.
        config = DeviceConfig(
            model="HEM-9601T",
            endianness=Endianness.LITTLE,
            user_start_addresses=[0x01C4],
            per_user_records_count=[100],
            record_byte_size=0x10,
            settings_read_address=0x0010,
            index_pointer_layout={
                "index_region_byte_size": 0x18,
                "endianness": "little",
                "backtrack_slots": 5,
                "users": [
                    {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
                ],
            },
        )
        driver = OmronDeviceDriver(config)
        now = dt.datetime(2026, 9, 20, 14, 0, 0)
        driver._now_func = lambda: now
        transport = OmronDeviceSession(MagicMock(), config)
        transport.unlock = AsyncMock()

        index_bytes = bytearray(0x18)
        index_bytes[0:2] = b"\x00\x80"

        when = now - dt.timedelta(hours=1)
        flags1 = when.hour | (when.day << 5) | (when.month << 10)
        flags2 = when.second | (when.minute << 6) | (1 << 12)
        valid = bytearray(b"\xff" * 16)
        valid[0:4] = bytes([125 - 25, 85, 70, when.year - 2000])
        valid[4:8] = bytes([flags1 & 0xFF, flags1 >> 8, flags2 & 0xFF, flags2 >> 8])
        valid[8:12] = b"\x00\x00\x00\x00"
        garbage = bytearray(16)
        garbage[0] = 0x01  # non-0xFF, but rejected by the parser as a placeholder

        slots = {99: garbage, 97: valid}
        read_calls = []

        async def fake_read_memory_range(addr, size, block_size=0x10):
            read_calls.append(addr)
            if addr == 0x0010:
                return index_bytes
            return slots.get((addr - 0x01C4) // 16, bytearray(b"\xff" * 16))

        transport.read_memory_range = AsyncMock(side_effect=fake_read_memory_range)

        records, empty_users = asyncio.run(
            driver._get_latest_via_index(transport, return_all_users=True)
        )

        assert empty_users == set()
        assert records[1]["sys"] == 125
        assert [a for a in read_calls if a >= 0x01C4] == [0x01C4 + s * 16 for s in (99, 98, 97)]
