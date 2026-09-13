"""페어링 세션의 등록 쓰기 (#175, #91).

WLD3.0 token-key 커프는 방금 만든 본드를 받아들이고도 다음 연결에서 재개를
거부한다 (HCI 0x06). 페어링 세션이 앱과 같은 설정 미러를 써두면 재개된다:
미읽음 카운터를 지운 인덱스 영역 + 10바이트 전송 슬롯 하나, 그리고 현재 시각을
찍은 시계 레코드. BP5465 / 로컬 BlueZ 에서 전원 리셋을 거쳐 실기 검증됐다.

여기서 고정하는 것:

- 바이트 단위로 검증된 그 변환이 리팩터링으로 흘러가지 않게 (정확한 출력)
- 주소가 모델이 아니라 프로파일에서 나오게
- 페어링 세션에서만, 링크당 한 번만
- ``close_memory_session`` 사이에 끼지 않게 — 080f 는 어떤 경우에도 나가야 한다
- 호출 지점이 예외를 삼키게 — 등록 실패는 세션을 죽일 이유가 아니다

conftest 가 homeassistant/bleak 를 MagicMock 으로 치환하므로 통합 계층은 AST 로,
드라이버 메서드는 unbound 호출로 검사한다 (test_session_cccd_persistence.py 방식).
"""
import ast
import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.omron.omron_ble.devices import get_device_config
from custom_components.omron.omron_ble.omron_driver import OmronDeviceSession
from custom_components.omron.omron_ble.settings_mirror import (
    SettingsMirrorLayout,
    clock_block,
)

_COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "omron"


def _function(path: Path, name: str):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} 을(를) 찾지 못했다 — 이름을 바꿨다면 이 테스트도 갱신할 것")


def _session(config, *, pairing=True, memory_active=True, memory=None):
    """커프 EEPROM 을 흉내 내는 최소 세션: 읽으면 memory 를, 쓰면 기록을 남긴다."""
    memory = memory if memory is not None else bytes(range(256)) * 16
    writes: list[tuple[int, bytes]] = []

    async def read_memory_range(address, size, block_size):
        return memory[address : address + size]

    async def write_memory_range(address, data, block_size):
        writes.append((address, bytes(data)))

    target = SimpleNamespace(
        _config=config,
        _pairing_session=pairing,
        _pairing_registration_done=False,
        _memory_session_active=memory_active,
        _require_connected=lambda _what: None,
        read_memory_range=read_memory_range,
        write_memory_range=write_memory_range,
        writes=writes,
    )
    return target


def _commit(target):
    return asyncio.run(OmronDeviceSession.commit_pairing_registration(target))


class TestVerifiedBytes:
    """#175 가 하드웨어에서 검증한 변환을 바이트 단위로 고정한다."""

    def test_the_registration_block_matches_the_capture_transform(self):
        cfg = get_device_config("HEM-7386T1")
        memory = bytearray(0x400)
        head = bytes(range(0x30))              # 0x0010 에서 읽히는 48바이트
        memory[0x0010 : 0x0010 + 0x30] = head
        target = _session(cfg, memory=bytes(memory))

        assert _commit(target) is True
        (head_addr, head_written), (clock_addr, clock_written) = target.writes

        # #175 의 변환 그대로: [:0x26], [4]=0x00 [5]=0x80 [17]=0x80 [32]+=1 [36]+=1
        expected = bytearray(head[:0x26])
        expected[4] = 0x00
        expected[5] = 0x80
        expected[17] = 0x80
        expected[32] = (expected[32] + 1) & 0xFF
        expected[36] = (expected[36] + 1) & 0xFF
        assert head_addr == 0x0058
        assert head_written == bytes(expected)
        assert clock_addr == 0x0088
        assert len(clock_written) == 16

    def test_the_slot_counters_wrap_at_a_byte(self):
        cfg = get_device_config("HEM-7386T1")
        memory = bytearray(0x400)
        memory[0x0010 + 32] = 0xFF
        memory[0x0010 + 36] = 0xFF
        target = _session(cfg, memory=bytes(memory))
        _commit(target)
        _, head_written = target.writes[0]
        assert head_written[32] == 0x00
        assert head_written[36] == 0x00

    def test_the_clock_block_is_the_shared_one(self):
        """시계 블록 변환은 secure flow 와 같은 함수여야 한다 — 두 벌이면 갈라진다."""
        cfg = get_device_config("HEM-7386T1")
        target = _session(cfg)
        _commit(target)
        _, clock_written = target.writes[1]
        # 플래그 비트, 6바이트 시각, 끝에서 두 번째 바이트의 가산 체크섬
        assert clock_written[4] & 0x01
        assert clock_written[14] == sum(clock_written[:14]) & 0xFF
        now = datetime(2026, 9, 13, 8, 0, 0)
        ref = clock_block(bytes(range(28)), now, 16)
        assert ref[8:14] == bytes((26, 9, 13, 8, 0, 0))


class TestDerivedFromTheProfile:
    """주소가 상수로 굳어 있으면 다른 배치의 프로파일에서 엉뚱한 곳을 쓴다."""

    def test_every_address_comes_from_the_catalog_fields(self):
        cfg = get_device_config("HEM-7386T1")
        layout = SettingsMirrorLayout(cfg)
        assert layout.head_read_address == cfg.settings_read_address == 0x0010
        assert layout.head_read_size == cfg.settings_time_sync_bytes[0] == 0x30
        assert layout.head_write_address == cfg.settings_write_address == 0x0058
        assert layout.clock_read_address == 0x0010 + 0x30 == 0x0040
        assert layout.clock_write_address == 0x0058 + 0x30 == 0x0088
        assert layout.clock_write_size == 0x40 - 0x30 == 16
        # 38 = 인덱스 영역(0x1C) + 슬롯(10)
        assert layout.head_write_size + 10 == 0x26

    def test_a_shifted_profile_moves_the_writes_with_it(self):
        cfg = SimpleNamespace(
            model="synthetic",
            pairing_registration_write=True,
            settings_read_address=0x0100,
            settings_write_address=0x0200,
            settings_time_sync_bytes=[0x30, 0x40],
            transmission_block_size=0x38,
            index_pointer_layout={
                "index_region_byte_size": 0x1C,
                "users": [{"unread_counter_offset": 0x04}],
            },
        )
        target = _session(cfg)
        _commit(target)
        assert [addr for addr, _ in target.writes] == [0x0200, 0x0230]

    def test_the_driver_names_no_model(self):
        source = (_COMPONENT / "omron_ble" / "omron_driver.py").read_text(encoding="utf-8")
        assert "HEM-7386T1" not in source, (
            "드라이버가 기종 이름으로 분기한다 — 프로파일 플래그로 표현할 것"
        )
        assert "resolve_profile_model_id" not in source

    def test_the_flag_is_set_on_the_verified_family_only(self):
        assert get_device_config("HEM-7386T1").pairing_registration_write is True
        assert get_device_config("HEM-7382T1-AZAZ").pairing_registration_write is True
        for other in ("HEM-7155T-MW3", "HEM-7380T1", "HEM-7188T1-LEO", "HEM-7142T2"):
            assert get_device_config(other).pairing_registration_write is False, other


class TestWhenItRuns:
    def test_not_on_an_ordinary_session(self):
        target = _session(get_device_config("HEM-7386T1"), pairing=False)
        assert _commit(target) is False
        assert target.writes == []

    def test_not_on_a_profile_without_the_flag(self):
        target = _session(get_device_config("HEM-7155T-MW3"))
        assert _commit(target) is False
        assert target.writes == []

    def test_once_per_link(self):
        """재시도된 readout 이 커프의 전송 카운터를 두 번 올리면 안 된다."""
        target = _session(get_device_config("HEM-7386T1"))
        assert _commit(target) is True
        assert _commit(target) is False
        assert len(target.writes) == 2

    def test_needs_an_open_memory_session(self):
        target = _session(get_device_config("HEM-7386T1"), memory_active=False)
        with pytest.raises(ConnectionError, match="memory session"):
            _commit(target)
        assert target.writes == []


class TestItNeverBlocksTheClose:
    def test_close_memory_session_has_no_registration_hook(self):
        """close 와 080f 사이에 무엇이 끼면, 그것이 실패할 때 080f 가 안 나간다."""
        fn = _function(_COMPONENT / "omron_ble" / "omron_driver.py", "close_memory_session")
        assert "registration" not in ast.unparse(fn).lower()

    def test_the_call_site_swallows_failures(self):
        fn = _function(_COMPONENT / "omron_ble" / "parser.py", "_poll_device_readout")
        call = None
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "commit_pairing_registration"
            ):
                call = node
        assert call is not None, "readout 이 등록을 커밋하지 않는다"
        guarded = any(
            isinstance(node, ast.Try)
            and node.lineno <= call.lineno <= getattr(node, "end_lineno", node.lineno)
            for node in ast.walk(fn)
        )
        assert guarded, "등록 실패가 폴을 통째로 실패시킨다"
