"""record_parsers.py 단위 테스트.

바이트 문자열은 실기기 HA 디버그 로그에서 그대로 가져온 것으로, 로그에 함께
찍힌 sys/dia/bpm/datetime 파싱 결과와 대조해 검증됐다 (HEM-7142T2, 2026-06-18).
"""
import datetime

import pytest

from custom_components.omron.omron_ble.record_parsers import (
    parse_classic_vital_14,
    parse_classic_vital_14_bitpacked,
)


class TestParseClassicVital14:
    def test_slot13_hem7142t2(self):
        # User1 [HEM-7142T2] slot=13 raw=6558541a331ade1800004c00ba00
        # -> sys=126 dia=88 bpm=84 dt=2026-06-17 19:35:30
        raw = bytes.fromhex("6558541a331ade1800004c00ba00")
        record = parse_classic_vital_14(raw, endianness="little")
        assert record["sys"] == 126
        assert record["dia"] == 88
        assert record["bpm"] == 84
        assert record["datetime"] == datetime.datetime(2026, 6, 17, 19, 35, 30)

    def test_slot7_hem7142t2(self):
        # User1 [HEM-7142T2] slot=7 raw=6457431a7316161c000046001900
        # -> sys=125 dia=87 bpm=67 dt=2026-05-19 19:48:22
        raw = bytes.fromhex("6457431a7316161c000046001900")
        record = parse_classic_vital_14(raw, endianness="little")
        assert record["sys"] == 125
        assert record["dia"] == 87
        assert record["bpm"] == 67
        assert record["datetime"] == datetime.datetime(2026, 5, 19, 19, 48, 22)

    def test_record_id_reads_official_sequence_no_offset(self):
        # The record sequence number sits at offset 10, size 2. On the two real
        # HEM-7142T2 slots above that yields 70 (slot 7) and 76 (slot 13) —
        # monotonic with the slot index, as a sequence number must be. Reading
        # the last two bytes instead gives 25 and 186.
        slot7 = parse_classic_vital_14(
            bytes.fromhex("6457431a7316161c000046001900"), endianness="little"
        )
        slot13 = parse_classic_vital_14(
            bytes.fromhex("6558541a331ade1800004c00ba00"), endianness="little"
        )
        assert slot7["_record_id"] == 70
        assert slot13["_record_id"] == 76
        assert slot13["_record_id"] - slot7["_record_id"] == 13 - 7

    def test_no_battery_field(self):
        # flags2 bit 13 is not a battery flag on any model — no known memory map
        # defines a field there, so the parser must not invent one.
        record = parse_classic_vital_14(
            bytes.fromhex("6558541a331ade1800004c00ba00"), endianness="little"
        )
        assert "battery" not in record
        assert record["cuff"] == 1
        assert record["pos"] == 0

    def test_empty_slot_all_ff_raises(self):
        raw = bytes.fromhex("ff" * 14)
        with pytest.raises(ValueError):
            parse_classic_vital_14(raw, endianness="little")

    def test_zero_filled_slot_raises(self):
        # sys byte alone (0x00) is a valid decode (25 mmHg) but the rest being
        # all-zero is the device's "never written" placeholder, not a real
        # reading — must still raise.
        raw = bytes(14)
        with pytest.raises(ValueError):
            parse_classic_vital_14(raw, endianness="little")

    def test_sys_above_0xe1_is_empty_marker(self):
        raw = bytes([0xE2]) + bytes(13)
        with pytest.raises(ValueError):
            parse_classic_vital_14(raw, endianness="little")


class TestParseClassicVital14Bitpacked:
    def test_hem7600t_slot23(self):
        # HEM-7600T-E slot=23 raw=5b781a4819d71b42000014006996
        # -> sys=145 dia=91 bpm=72 (confirmed against device EEPROM dump)
        raw = bytes.fromhex("5b781a4819d71b42000014006996")
        record = parse_classic_vital_14_bitpacked(raw, endianness="big")
        assert record["sys"] == 145
        assert record["dia"] == 91
        assert record["bpm"] == 72
        assert "battery" not in record

    def test_no_battery_field_6232_family(self):
        from custom_components.omron.omron_ble.record_parsers import (
            parse_classic_vital_14_6232_family,
        )

        record = parse_classic_vital_14_6232_family(
            bytes.fromhex("5b781a4819d71b42000014006996"), endianness="big"
        )
        assert "battery" not in record


class TestParseClassicVital24Heartguide:
    def test_valid_heartguide_record(self):
        from custom_components.omron.omron_ble.record_parsers import parse_classic_vital_24_heartguide

        raw = bytearray(24)
        raw[0] = 100  # sys: 100 + 25 = 125
        raw[1] = 80   # dia: 80
        raw[2] = 70   # bpm: 70
        raw[3] = 26   # year: 2026
        # flags1: hour=19, day=17, month=6, ihb=1, mov=1
        # -> 19 | (17<<5) | (6<<10) | (1<<14) | (1<<15) = 6707 | 16384 | 32768 = 55859 (0xDA33)
        raw[4:6] = (55859).to_bytes(2, "little")
        # flags2: second=30, minute=35, cuff=1 -> 30 | (35<<6) | (1<<12) = 6366 (0x18DE)
        raw[6:8] = (6366).to_bytes(2, "little")
        raw[10:12] = (42).to_bytes(2, "little")  # _record_id = 42
        raw[17] = 0x00  # success status

        record = parse_classic_vital_24_heartguide(raw, endianness="little")
        assert record["sys"] == 125
        assert record["dia"] == 80
        assert record["bpm"] == 70
        assert record["datetime"] == datetime.datetime(2026, 6, 17, 19, 35, 30)
        assert record["ihb"] == 1
        assert record["mov"] == 1
        assert record["cuff"] == 1
        assert record["_record_id"] == 42

    def test_heartguide_error_code_rejected(self):
        from custom_components.omron.omron_ble.record_parsers import parse_classic_vital_24_heartguide

        raw = bytearray(24)
        raw[0] = 100
        raw[1] = 80
        raw[17] = 0x05  # error status code
        with pytest.raises(ValueError, match="measurement error code 0x05"):
            parse_classic_vital_24_heartguide(raw, endianness="little")

    def test_heartguide_empty_slot_rejected(self):
        from custom_components.omron.omron_ble.record_parsers import parse_classic_vital_24_heartguide

        raw = bytes([0xFF] * 24)
        with pytest.raises(ValueError, match="record slot is empty"):
            parse_classic_vital_24_heartguide(raw, endianness="little")

    def test_heartguide_too_short_rejected(self):
        from custom_components.omron.omron_ble.record_parsers import parse_classic_vital_24_heartguide

        raw = bytes(20)
        with pytest.raises(ValueError, match="record too short"):
            parse_classic_vital_24_heartguide(raw, endianness="little")

