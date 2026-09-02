"""EEPROM 시간 디코드(_decode_eeprom_time_payload) 단위 테스트.

바이트는 실기기 HA 디버그 로그의 "EEPROM time raw" 라인에서 그대로 가져왔고,
같은 로그의 "Device ... time is already in sync (...)" 라인과 대조해 검증됐다.

``_SWAPPED`` layout 들이 여기 있는 이유가 있다. 벤더 앱의 메모리 맵은 시간
필드를 시간순으로 적어두는데, 그 프레임은 우리가 보는 원시 스트림과 다르다.
그 차이를 모르고 순서를 "고치면" 아래 기기들의 시간 동기가 조용히 깨진다 —
EEPROM 디코드가 실패하고 CTS 로 넘어가므로 증상이 안 보인다.
"""
import datetime

import pytest

from custom_components.omron.omron_ble.omron_driver import (
    _decode_eeprom_time_payload,
)


class TestAt8:
    def test_hem7382t1(self):
        # addr=0x0010+0x30 size=16 raw=c6a40100000000001a070f113b13fa00
        # -> "Device HEM-7382T1 time is already in sync (2026-07-15 17:59:19)"
        raw = bytearray.fromhex("c6a40100000000001a070f113b13fa00")
        assert _decode_eeprom_time_payload("eeprom_time_at_8", raw) == (
            datetime.datetime(2026, 7, 15, 17, 59, 19)
        )

    def test_hem7142t2(self):
        # addr=0x0260+0x2C size=16 raw=c8a80000000000001a06120e3509ee00
        # -> "Device HEM-7142T2 time is already in sync (2026-06-18 14:53:09)"
        raw = bytearray.fromhex("c8a80000000000001a06120e3509ee00")
        assert _decode_eeprom_time_payload("eeprom_time_at_8", raw) == (
            datetime.datetime(2026, 6, 18, 14, 53, 9)
        )


class TestAt2Swapped:
    """이슈 #74 — HEM-7600T-E. 원시 바이트와 기기가 들고 있던 시각이 같은 로그에 있다."""

    def test_hem7600t_e(self):
        # addr=0x0260+0x14 size=10 raw=a0c0061a120f131437c8
        # -> "Device HEM-7600T-E time is already in sync (2026-06-15 18:20:19)"
        raw = bytearray.fromhex("a0c0061a120f131437c8")
        assert _decode_eeprom_time_payload("eeprom_time_at_2_swapped", raw) == (
            datetime.datetime(2026, 6, 15, 18, 20, 19)
        )

    def test_the_chronological_reading_of_the_same_bytes_is_impossible(self):
        # 같은 바이트를 시간순으로 읽으면 month=26 이 되어 파싱 자체가 실패한다.
        # 이 대비가 없으면 벤더 스펙만 보고 순서를 "고치려는" 시도가 반복된다.
        raw = bytearray.fromhex("a0c0061a120f131437c8")
        with pytest.raises(ValueError):
            _decode_eeprom_time_payload("eeprom_time_at_2", raw)

    def test_hem7322t_e(self):
        # 이슈 #38, 2026-05-13 16:14 구간의 폴에서 나온 덤프.
        raw = bytearray.fromhex("08c8051a100d190ef033")
        assert _decode_eeprom_time_payload("eeprom_time_at_2_swapped", raw) == (
            datetime.datetime(2026, 5, 13, 16, 14, 25)
        )


class TestAt8Swapped:
    """이슈 #38 — HEM-6232T-E. 같은 기기에서 at_8 은 실패하고 at_8_swapped 는 맞았다."""

    def test_hem6232t_e(self):
        # addr=0x0260+0x2C size=16 raw=50c0000300000000051a0a0e0806a758
        # -> "Device HEM-6232T-E time is already in sync (2026-05-14 10:06:08)"
        raw = bytearray.fromhex("50c0000300000000051a0a0e0806a758")
        assert _decode_eeprom_time_payload("eeprom_time_at_8_swapped", raw) == (
            datetime.datetime(2026, 5, 14, 10, 6, 8)
        )

    def test_the_chronological_reading_of_the_same_bytes_is_impossible(self):
        raw = bytearray.fromhex("50c0000300000000051a0a0e0806a758")
        with pytest.raises(ValueError):
            _decode_eeprom_time_payload("eeprom_time_at_8", raw)
