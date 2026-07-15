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
