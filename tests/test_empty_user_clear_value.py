"""Unit tests for empty user clear_value handling in Omron BLE index readout."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.omron.omron_ble.devices import DeviceConfig, Endianness
from custom_components.omron.omron_ble.omron_driver import OmronDeviceDriver, OmronDeviceSession


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
                    {"write_cursor_offset": 0x08, "unread_counter_offset": 0x0C, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1, "clear_value": 0x8000},
                ],
            },
        )
        driver = OmronDeviceDriver(config)
        transport = OmronDeviceSession(MagicMock(), config)
        transport.unlock = AsyncMock()

        # User 1: cursor = 0x0004 (slot 3), User 2: cursor = 0x8000 (clear_value, no records)
        # In little-endian:
        # offset 0x00: 04 00 ... (cursor 4)
        # offset 0x08: 00 80 ... (cursor 0x8000)
        index_bytes = bytearray(16)
        index_bytes[0:2] = b"\x04\x00"
        index_bytes[8:10] = b"\x00\x80"

        read_calls = []

        async def fake_read_memory_range(addr, size, block_size=0x10):
            read_calls.append((addr, size))
            if addr == 0x0260:
                return index_bytes
            # Return valid record for user 1 probe
            # record format: timestamp, sys=120, dia=80, bpm=70...
            rec = bytearray(16)
            rec[0] = 0x01  # some non-FF data
            return rec

        transport.read_memory_range = AsyncMock(side_effect=fake_read_memory_range)

        records, empty_users = asyncio.run(
            driver._get_latest_via_index(transport, return_all_users=True)
        )

        # User 2 must be confirmed empty without attempting to probe slot 59 (at 0x06A8 + 59*16)
        assert 2 in empty_users
        # User 2 should NOT have a read call into its memory block (0x06A8..)
        user2_reads = [addr for addr, _ in read_calls if addr >= 0x06A8]
        assert len(user2_reads) == 0

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
            rec = bytearray(14)
            rec[0] = 0x01
            return rec

        transport.read_memory_range = AsyncMock(side_effect=fake_read_memory_range)

        records, empty_users = asyncio.run(
            driver._get_latest_via_index(transport, return_all_users=True)
        )

        assert 2 in empty_users
        user2_reads = [addr for addr, _ in read_calls if addr >= 0x05F4]
        assert len(user2_reads) == 0

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
            rec = bytearray(16)
            rec[0] = 0x01
            return rec

        transport.read_memory_range = AsyncMock(side_effect=fake_read_memory_range)

        records, empty_users = asyncio.run(
            driver._get_latest_via_index(transport, return_all_users=True)
        )

        assert 2 in empty_users
        user2_reads = [addr for addr, _ in read_calls if addr >= 0x0458]
        assert len(user2_reads) == 0
