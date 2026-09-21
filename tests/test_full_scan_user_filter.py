"""The full-scan fallback reads only the users it still needs.

``get_latest_records_per_user`` already knew which users were confirmed
empty by the index path, but ``get_all_records`` read every user anyway.
On a cuff that refuses reads in an unregistered user's region (#197) the
whole scan died there and took the users that do read with it.
"""
import asyncio
import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.omron.omron_ble.devices import DeviceConfig, Endianness
from custom_components.omron.omron_ble.driver import OmronDeviceDriver
from custom_components.omron.omron_ble.memory_protocol import MemoryReadRefused
from custom_components.omron.omron_ble.session import OmronDeviceSession

USER1_BASE = 0x01C4
USER2_BASE = 0x0804
RECORD = 0x10


def _config() -> DeviceConfig:
    return DeviceConfig(
        model="HEM-7380T1",
        endianness=Endianness.LITTLE,
        user_start_addresses=[USER1_BASE, USER2_BASE],
        per_user_records_count=[100, 100],
        record_byte_size=RECORD,
        settings_read_address=0x0010,
        index_pointer_layout={
            "index_region_byte_size": 0x18,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
    )


def _record(when: dt.datetime, sys_val: int) -> bytearray:
    flags1 = when.hour | (when.day << 5) | (when.month << 10)
    flags2 = when.second | (when.minute << 6)
    raw = bytearray(b"\xff" * RECORD)
    raw[0] = sys_val - 25
    raw[1] = 80
    raw[2] = 70
    raw[3] = when.year - 2000
    raw[4], raw[5] = flags1 & 0xFF, flags1 >> 8
    raw[6], raw[7] = flags2 & 0xFF, flags2 >> 8
    raw[8:12] = b"\x00\x00\x00\x00"
    return raw


def _driver_and_transport(*, user2_error: Exception | None = None):
    config = _config()
    driver = OmronDeviceDriver(config)
    driver._now_func = lambda: dt.datetime(2026, 9, 21, 12, 0, 0)
    transport = OmronDeviceSession(MagicMock(), config)
    transport.unlock = AsyncMock()

    # User 1 has one record in slot 0; everything else is the empty marker.
    user1_region = bytearray(b"\xff" * (100 * RECORD))
    user1_region[0:RECORD] = _record(dt.datetime(2026, 9, 21, 11, 30, 0), 121)
    reads: list[int] = []

    async def fake_read(addr, size, block_size=0x10):
        reads.append(addr)
        if addr == 0x0010:
            return bytearray(0x18)
        if addr >= USER2_BASE:
            if user2_error is not None:
                raise user2_error
            return bytearray(b"\xff" * size)
        return user1_region[addr - USER1_BASE:addr - USER1_BASE + size]

    transport.read_memory_range = AsyncMock(side_effect=fake_read)
    return driver, transport, reads


class TestGetAllRecords:
    def test_users_filter_skips_the_other_region_without_a_read(self):
        driver, transport, reads = _driver_and_transport()

        result = asyncio.run(driver.get_all_records(transport, users={1}))

        assert len(result) == 2
        assert [r["sys"] for r in result[0]] == [121]
        assert result[1] == []
        assert all(a < USER2_BASE for a in reads)

    def test_no_filter_reads_every_user(self):
        driver, transport, reads = _driver_and_transport()

        result = asyncio.run(driver.get_all_records(transport))

        assert [r["sys"] for r in result[0]] == [121]
        assert result[1] == []
        assert any(a >= USER2_BASE for a in reads)

    def test_a_refused_region_is_skipped_and_the_rest_kept(self):
        driver, transport, _ = _driver_and_transport(
            user2_error=MemoryReadRefused(USER2_BASE, 0xE3)
        )

        result = asyncio.run(driver.get_all_records(transport))

        assert [r["sys"] for r in result[0]] == [121]
        assert result[1] == []

    def test_a_silent_region_still_raises(self):
        # No answer is link trouble, not a verdict on the region: the scan
        # fails as it always has rather than reporting a user as empty.
        driver, transport, _ = _driver_and_transport(
            user2_error=ConnectionError("Failed to receive response after 4 retries")
        )

        with pytest.raises(ConnectionError):
            asyncio.run(driver.get_all_records(transport))


class TestLatestPerUserFallback:
    def test_fallback_reads_only_the_users_it_still_needs(self):
        # Index path: nothing for user 1, user 2 confirmed empty. The scan
        # must go into user 1's region only.
        driver, transport, reads = _driver_and_transport(
            user2_error=MemoryReadRefused(USER2_BASE, 0xE3)
        )
        driver._get_latest_via_index = AsyncMock(return_value=({}, {2}))

        latest = asyncio.run(driver.get_latest_records_per_user(transport))

        assert latest[1]["sys"] == 121
        assert 2 not in latest
        assert all(a < USER2_BASE for a in reads)

    def test_fallback_survives_a_refused_region_it_had_to_read(self):
        # Index path gave nothing and confirmed nothing, so both users are
        # scanned; user 2 refuses. User 1 must still come back.
        driver, transport, _ = _driver_and_transport(
            user2_error=MemoryReadRefused(USER2_BASE, 0xE3)
        )
        driver._get_latest_via_index = AsyncMock(return_value=({}, set()))

        latest = asyncio.run(driver.get_latest_records_per_user(transport))

        assert latest[1]["sys"] == 121
        assert 2 not in latest
