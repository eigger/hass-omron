"""EEPROM 시간 필드 오프셋을 벤더 스펙과 대조한다.

``eeprom_time_offsets.json`` 은 OMRON connect 안드로이드 앱(일본판 011.005)의
``Memory_Map/<model>/SettingWriteIndex.json`` 에서 뽑은, 기종별 시간 필드
[year, month, day, hour, minute, second] 의 절대 바이트 오프셋이다.

이 대조가 두 개의 layout 을 없앴다. ``classic_mixed`` 와 ``classic_offset8`` 은
필드를 [month, year, hour, day, second, minute] 로 읽었는데, 겹치는 44개 기종
전부에서 벤더 스펙과 인접 쌍이 뒤바뀌어 있었다. 실기 캡처로 검증된 적도 없었고,
틀려도 EEPROM 동기가 실패하고 CTS 로 넘어가 조용히 묻혔다.
"""
import json
from pathlib import Path

from custom_components.omron.omron_ble.devices import get_device_config

_VENDOR = json.loads(
    (Path(__file__).parent / "eeprom_time_offsets.json").read_text(encoding="utf-8")
)


def _our_offsets(config) -> list[int] | None:
    """Absolute offsets our layout puts [year, month, day, hour, minute, second] at."""
    window = config.settings_time_sync_bytes
    if not window:
        return None
    start = window[0]
    layout = str(config.resolved_time_sync_layout())
    if layout.endswith("modern_offset8"):
        return [start + 8 + i for i in range(6)]
    if layout.endswith("linear_10"):
        return [start + 2 + i for i in range(6)]
    if layout.endswith("hem6401_prefix"):
        return [start + i for i in range(6)]
    raise AssertionError(f"unknown layout {layout} — add it to this test")


def test_the_fixture_covers_the_catalog() -> None:
    assert len(_VENDOR) >= 130, "the vendor offset table shrank"


def test_every_time_layout_matches_the_vendor_spec() -> None:
    mismatched = []
    for model, vendor in _VENDOR.items():
        ours = _our_offsets(get_device_config(model))
        if ours is None:
            continue
        if ours != vendor:
            mismatched.append((model, ours, vendor))
    assert not mismatched, f"{len(mismatched)} models disagree: {mismatched[:5]}"


def test_all_layouts_are_chronological() -> None:
    # Each surviving layout is [year, month, day, hour, minute, second] in
    # consecutive bytes; a layout that reorders fields is what this whole
    # comparison ruled out.
    for vendor in _VENDOR.values():
        assert vendor == list(range(vendor[0], vendor[0] + 6)), vendor
