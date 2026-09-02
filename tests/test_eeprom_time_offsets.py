"""EEPROM 시간 창(window)의 시작 위치를 벤더 스펙과 대조한다.

``eeprom_time_offsets.json`` 은 OMRON connect 안드로이드 앱(일본판 011.005)의
``Memory_Map/<model>/SettingWriteIndex.json`` 에서 뽑은, 기종별 시간 필드
[year, month, day, hour, minute, second] 의 바이트 오프셋이다. 133개 기종 전부
연속된 6바이트이고, 그래서 이 표가 확인해 주는 것은 **여섯 바이트가 어디서
시작하는가** 하나다 — 즉 ``settings_time_sync_bytes`` 와 layout 의 오프셋 부분.

바이트 순서는 이 표로 알 수 없다. 앱의 프레임은 항상 시간순이고, 우리가 보는
원시 스트림은 일부 기종군에서 16비트 워드마다 뒤집혀 있다. 그쪽은
``test_eeprom_time.py`` 의 실기 덤프가 고정한다 (#74, #38).
"""
import json
from pathlib import Path

from custom_components.omron.omron_ble.devices import (
    TimeSyncLayout,
    get_device_config,
)

_VENDOR = json.loads(
    (Path(__file__).parent / "eeprom_time_offsets.json").read_text(encoding="utf-8")
)

_WINDOW_START = {
    TimeSyncLayout.AT_0: 0,
    TimeSyncLayout.AT_2: 2,
    TimeSyncLayout.AT_2_SWAPPED: 2,
    TimeSyncLayout.AT_8: 8,
    TimeSyncLayout.AT_8_SWAPPED: 8,
}


def test_the_fixture_covers_the_catalog() -> None:
    assert len(_VENDOR) >= 130, "the vendor offset table shrank"


def test_the_vendor_rows_are_six_consecutive_bytes() -> None:
    # The app's frame is always chronological, which is why it says nothing
    # about the byte order our transport delivers.
    for model, vendor in _VENDOR.items():
        assert vendor == list(range(vendor[0], vendor[0] + 6)), model


def test_every_profile_starts_the_window_where_the_vendor_does() -> None:
    mismatched = []
    for model, vendor in _VENDOR.items():
        config = get_device_config(model)
        window = config.settings_time_sync_bytes
        if not window:
            continue
        ours = window[0] + _WINDOW_START[config.resolved_time_sync_layout()]
        if ours != vendor[0]:
            mismatched.append((model, ours, vendor[0]))
    assert not mismatched, f"{len(mismatched)} models disagree: {mismatched[:5]}"


def test_every_layout_is_covered() -> None:
    assert set(_WINDOW_START) == set(TimeSyncLayout), (
        "a layout was added without saying where its window starts"
    )
