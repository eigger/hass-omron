"""HN-300T2 weight scale (#233).

The bytes come from a reporter's nRF Connect capture of the vendor app reading
the scale: the index block at 0x01A0, the record it then fetched at 0x03F0 and
the clock block it wrote back at 0x0248. The scale showed 102.8 kg at 18:58:39.
"""
import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.omron.omron_ble.devices import (
    MeasurementKind,
    RecordParser,
    TimeSyncLayout,
    get_device_config,
    infer_model_id_from_local_name,
    resolve_profile_model_id,
)
from custom_components.omron.omron_ble.driver import (
    OmronDeviceDriver,
    _cursor_parity_ok,
    _decode_eeprom_time_payload,
    _encode_eeprom_time_payload,
)
from custom_components.omron.omron_ble.record_parsers import parse_weight_16
from custom_components.omron.omron_ble.session import OmronDeviceSession
from custom_components.omron.omron_ble.util import entity_name_translation

INDEX = bytes.fromhex("540000000100000001 7c0080".replace(" ", ""))
RECORD = bytes.fromhex("08081a0a0612 3a27 00017c046dff9aff".replace(" ", ""))
CLOCK_READ = bytes.fromhex("1a0a06123a3ab0ff")
CLOCK_WRITTEN = bytes.fromhex("1a0a0612 3b12 89ff".replace(" ", ""))
WEIGHT_SERVICE = "0000181d-0000-1000-8000-00805f9b34fb"
PRESSURE_SERVICE = "00001810-0000-1000-8000-00805f9b34fb"
CAPTURE_AT = datetime.datetime(2026, 10, 6, 18, 59, 18)


class TestProfile:
    def test_resolves_to_a_weight_profile(self):
        config = get_device_config("HN-300T2")
        assert config.measurement_kind == MeasurementKind.WEIGHT
        assert config.is_blood_pressure is False
        assert config.record_parser == RecordParser.WEIGHT_16
        assert config.display_model == "HN-300T2"

    def test_variants_share_the_profile(self):
        for variant in ("HN-300T2_AP", "HN-300T2_E-BK", "HN-300T2_JT_TW-BK"):
            assert resolve_profile_model_id(variant) == "HN-300T2", variant
            assert get_device_config(variant).display_model == variant

    def test_the_layout_matches_the_capture(self):
        config = get_device_config("HN-300T2")
        assert config.settings_read_address == 0x01A0
        assert config.settings_write_address == 0x0230
        assert config.user_start_addresses == [0x02C0]
        assert config.per_user_records_count == [30]
        assert config.record_byte_size == 0x10
        assert config.ignore_checksum_blocks == (1,)
        user = config.index_pointer_layout["users"][0]
        assert user["write_cursor_mask"] == 0x3F
        assert user["cursor_parity"] == "odd"

    def test_a_cuff_stays_a_cuff(self):
        config = get_device_config("HEM-7142T2")
        assert config.measurement_kind == MeasurementKind.BLOOD_PRESSURE
        assert config.is_blood_pressure is True
        assert config.ignore_checksum_blocks == ()

    def test_the_local_name_infers_the_model(self):
        assert infer_model_id_from_local_name("HN-300T2") == "HN-300T2"
        assert infer_model_id_from_local_name("OMRON HN-300T2_AP") == "HN-300T2_AP"

    def test_a_name_that_only_ends_in_hn_is_not_a_scale(self):
        assert infer_model_id_from_local_name("XHN-300T2") is None
        assert infer_model_id_from_local_name("XHN-1 HEM-7600T") == "HEM-7600T"

    def test_the_standard_weight_service_alone_is_compatible(self):
        scale = get_device_config("HN-300T2")
        cuff = get_device_config("HEM-7142T2")
        weight, pressure = [WEIGHT_SERVICE], [PRESSURE_SERVICE]
        assert scale.is_advertisement_compatible(weight)
        assert not scale.is_advertisement_compatible(pressure)
        assert cuff.is_advertisement_compatible(pressure)
        assert not cuff.is_advertisement_compatible(weight)


class TestRecord:
    def test_capture_record(self):
        record = parse_weight_16(RECORD, "big")
        assert record["weight"] == 102.8
        assert record["datetime"] == datetime.datetime(2026, 10, 6, 18, 58, 39)
        assert record["_record_id"] == 0x017C

    def test_the_weight_is_big_endian(self):
        # 0x07B0 = 98.4 kg; read little-endian it would be 2259 kg.
        raw = bytearray(RECORD)
        raw[0:2] = bytes([0x07, 0xB0])
        assert get_device_config("HN-300T2").parse_record(bytes(raw))["weight"] == 98.4

    def test_an_empty_slot_is_rejected(self):
        with pytest.raises(ValueError):
            parse_weight_16(b"\xff" * 16, "big")

    def test_a_short_record_is_rejected(self):
        with pytest.raises(ValueError):
            parse_weight_16(RECORD[:10], "big")

    def test_an_impossible_date_keeps_the_record_without_one(self):
        bad = bytearray(RECORD)
        bad[3] = 13
        assert parse_weight_16(bytes(bad), "big")["datetime"] is None


class TestCursor:
    def test_the_capture_cursor_keeps_odd_parity(self):
        assert _cursor_parity_ok(0x54, "odd")
        assert _cursor_parity_ok(0x80, "odd")
        assert not _cursor_parity_ok(0x55, "odd")
        assert _cursor_parity_ok(0x55, "even")


class TestClock:
    def test_the_device_clock_in_the_capture_decodes(self):
        layout = TimeSyncLayout.AT_0_CHECKSUM.value
        assert _decode_eeprom_time_payload(layout, bytearray(CLOCK_READ)) == (
            datetime.datetime(2026, 10, 6, 18, 58, 58)
        )

    def test_the_block_we_write_is_the_one_the_app_wrote(self):
        layout = TimeSyncLayout.AT_0_CHECKSUM.value
        assert bytes(
            _encode_eeprom_time_payload(layout, bytearray(CLOCK_READ), CAPTURE_AT)
        ) == CLOCK_WRITTEN

    def test_the_window_is_the_eight_bytes_the_app_touched(self):
        config = get_device_config("HN-300T2")
        assert config.supports_eeprom_time_sync
        start, end = config.settings_time_sync_bytes
        assert config.settings_read_address + start == 0x01B8
        assert config.settings_write_address + start == 0x0248
        assert end - start == len(CLOCK_WRITTEN)


def _driver_and_transport(slot_bytes: dict[int, bytes], index: bytes = INDEX):
    config = get_device_config("HN-300T2")
    driver = OmronDeviceDriver(config)
    driver._now_func = lambda: datetime.datetime(2026, 10, 7, 12, 0, 0)
    transport = OmronDeviceSession(MagicMock(), config)
    transport.unlock = AsyncMock()
    reads: list[int] = []

    async def fake_read(addr, size, block_size=0x10):
        reads.append(addr)
        if addr == 0x01A0:
            return bytearray(index)
        return bytearray(slot_bytes.get(addr, b"\xff" * size))

    transport.memory.read_memory_range = AsyncMock(side_effect=fake_read)
    return driver, transport, reads


class TestReadout:
    def test_the_cursor_points_at_the_record_the_app_read(self):
        driver, transport, reads = _driver_and_transport({0x03F0: RECORD})

        latest = asyncio.run(driver.get_latest_record(transport))

        assert reads == [0x01A0, 0x03F0]
        assert latest["weight"] == 102.8
        assert latest["datetime"] == datetime.datetime(2026, 10, 6, 18, 58, 39)
        assert latest["user"] == 1
        assert "measurement_type" not in latest

    def test_a_scale_that_never_measured_is_confirmed_empty(self):
        cleared = bytes([0x80, 0, 0, 0, 0x80, 0, 0, 0, 0, 0, 0, 0x80])
        driver, transport, reads = _driver_and_transport({}, index=cleared)

        records, empty = asyncio.run(
            driver._get_latest_via_index(transport, return_all_users=True)
        )

        assert records == {}
        assert empty == {1}

    @staticmethod
    def _latest_with_raw_weight(raw: int):
        record = bytearray(RECORD)
        record[0:2] = raw.to_bytes(2, "big")
        driver, transport, _ = _driver_and_transport({0x03F0: bytes(record)})
        return asyncio.run(driver._get_latest_via_index(transport))

    def test_zero_and_over_the_ceiling_are_dropped(self):
        assert self._latest_with_raw_weight(0x0000) is None
        assert self._latest_with_raw_weight(0x1771) is None  # 300.05 kg

    def test_the_floor_is_kept(self):
        assert self._latest_with_raw_weight(0x0014)["weight"] == 1.0  # 1.00 kg

    def test_a_future_dated_record_is_dropped(self):
        record = bytearray(RECORD)
        record[2] = 40  # 2040
        driver, transport, _ = _driver_and_transport({0x03F0: bytes(record)})

        assert asyncio.run(driver._get_latest_via_index(transport)) is None


class TestProfileHook:
    def test_no_profile_needs_a_write_yet(self):
        driver, transport, _ = _driver_and_transport({})
        asyncio.run(driver.write_user_profile(transport))


class TestEntityNames:
    def test_weight_is_a_translated_name(self):
        assert entity_name_translation("weight", None) == ("weight", {})

    def test_a_second_slot_keeps_the_entered_name(self):
        key, placeholders = entity_name_translation("weight_alex", {2: "Alex"})
        assert key == "weight_user"
        assert placeholders == {"user": "Alex"}
