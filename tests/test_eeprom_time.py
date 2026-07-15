"""EEPROM 시간 디코드(_decode_eeprom_time_payload) 단위 테스트.

바이트는 실기기 HA 디버그 로그의 "EEPROM time raw" 라인에서 그대로 가져왔고,
같은 로그의 "Device ... time is already in sync (...)" 라인과 대조해 검증됐다.
"""
import datetime

from custom_components.omron.omron_ble.omron_driver import (
    _decode_eeprom_time_payload,
)


class TestModernOffset8:
    def test_hem7382t1(self):
        # addr=0x0010+0x30 size=16 raw=c6a40100000000001a070f113b13fa00
        # -> "Device HEM-7382T1 time is already in sync (2026-07-15 17:59:19)"
        raw = bytearray.fromhex("c6a40100000000001a070f113b13fa00")
        assert _decode_eeprom_time_payload("eeprom_time_modern_offset8", raw) == (
            datetime.datetime(2026, 7, 15, 17, 59, 19)
        )

    def test_hem7142t2(self):
        # addr=0x0260+0x2C size=16 raw=c8a80000000000001a06120e3509ee00
        # -> "Device HEM-7142T2 time is already in sync (2026-06-18 14:53:09)"
        raw = bytearray.fromhex("c8a80000000000001a06120e3509ee00")
        assert _decode_eeprom_time_payload("eeprom_time_modern_offset8", raw) == (
            datetime.datetime(2026, 6, 18, 14, 53, 9)
        )
