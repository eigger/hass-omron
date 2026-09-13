"""페어링 세션의 등록 쓰기 (#175, #91).

WLD3.0 token-key 커프는 방금 만든 본드를 받아들이고도 다음 연결에서 재개를
거부한다 (HCI 0x06). 페어링 세션이 앱과 같은 설정 미러를 써두면 재개된다.

그 쓰기의 실체:

- 인덱스 영역의 **스트림별 미읽음 카운터를 전부 유휴값으로** — 혈압 두 스트림은
  2바이트 `0x8000`, 나머지는 1바이트 `0x80`. #175 캡처에서 0x80 이 된 바이트는
  플래그가 아니라 그중 하나다.
- 사용자 프로필 슬롯(10B): +4 의 u32 전송 카운트를 1 올리고, +8 에 앞 8바이트의
  가산 체크섬 — 시계 레코드와 같은 규칙.
- `0xFFFF` 로 시작하는 슬롯은 한 번도 쓰인 적이 없다 — 손대지 않는다.
- 시계 레코드에 현재 시각.

여기서 고정하는 것: 위 변환의 정확한 출력, 주소가 프로파일에서 나온다는 것,
페어링 세션에서만 링크당 한 번, close 를 막지 않는다는 것, 호출 지점이 예외를
삼킨다는 것.

conftest 가 homeassistant/bleak 를 MagicMock 으로 치환하므로 통합 계층은 AST 로,
드라이버 메서드는 unbound 호출로 검사한다 (test_session_cccd_persistence.py 방식).
"""
import ast
import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.omron.omron_ble.devices import (
    PairingRegistration,
    get_device_config,
)
from custom_components.omron.omron_ble.omron_driver import OmronDeviceSession
from custom_components.omron.omron_ble.settings_mirror import (
    SettingsMirrorLayout,
    clock_block,
    slot_checksum,
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

    return SimpleNamespace(
        _config=config,
        _pairing_session=pairing,
        _pairing_registration_done=False,
        _memory_session_active=memory_active,
        _require_connected=lambda _what: None,
        read_memory_range=read_memory_range,
        write_memory_range=write_memory_range,
        writes=writes,
    )


def _commit(target):
    return asyncio.run(OmronDeviceSession.commit_pairing_registration(target))


def _populated_slot(count: int, birth=(126, 4, 17)) -> bytes:
    """#67 캡처의 슬롯 모양: 생년월일 3B, 플래그 1B, u32 카운트, 체크섬, 패딩."""
    body = bytes(birth) + b"\x00" + count.to_bytes(4, "little")
    return body + bytes([sum(body) & 0xFF, 0x00])


def _settings(slot_offset: int, slot: bytes, size: int = 0x30) -> bytes:
    region = bytearray(range(size))
    region[slot_offset : slot_offset + 10] = slot
    return bytes(region)


class TestTheWrite:
    def test_every_unread_counter_goes_idle_and_the_slot_steps(self):
        cfg = get_device_config("HEM-7386T1")
        memory = bytearray(0x400)
        memory[0x0010 : 0x0010 + 0x30] = _settings(0x1C, _populated_slot(1))
        target = _session(cfg, memory=bytes(memory))

        assert _commit(target) is True
        (head_addr, head), (clock_addr, clock) = target.writes
        assert head_addr == 0x0058 and len(head) == 0x1C + 10
        assert clock_addr == 0x0088 and len(clock) == 16

        expected = bytearray(_settings(0x1C, _populated_slot(1))[: 0x1C + 10])
        for offset, idle in cfg.pairing_registration.unread_clears:
            width = 2 if idle > 0xFF else 1
            expected[offset : offset + width] = idle.to_bytes(width, "little")
        expected[0x1C : 0x1C + 10] = _populated_slot(2)
        assert head == bytes(expected)
        # #175 가 하드웨어에서 검증한 바이트는 이 일반 규칙의 특수 사례여야 한다.
        assert head[4:6] == b"\x00\x80" and head[0x11] == 0x80
        assert head[0x1C + 4] == 2 and head[0x1C + 8] == slot_checksum(head[0x1C:])

    def test_the_count_is_a_32_bit_value_not_a_byte(self):
        """0xFF 에서 한 바이트만 감기면 카운트가 0 으로 돌아간다."""
        cfg = get_device_config("HEM-7386T1")
        memory = bytearray(0x400)
        memory[0x0010 : 0x0010 + 0x30] = _settings(0x1C, _populated_slot(0xFF))
        target = _session(cfg, memory=bytes(memory))
        _commit(target)
        _, head = target.writes[0]
        assert int.from_bytes(head[0x1C + 4 : 0x1C + 8], "little") == 0x100

    def test_the_slot_checksum_is_recomputed_not_bumped(self):
        """카운트가 +1 이면 8비트 합도 +1 — 그래서 +1 이 우연히 맞았다. 규칙을 고정."""
        cfg = get_device_config("HEM-7386T1")
        stale = bytearray(_populated_slot(5))
        stale[8] = 0x00                                  # 읽은 체크섬이 틀려 있어도
        memory = bytearray(0x400)
        memory[0x0010 : 0x0010 + 0x30] = _settings(0x1C, bytes(stale))
        target = _session(cfg, memory=bytes(memory))
        _commit(target)
        _, head = target.writes[0]
        assert head[0x1C + 8] == slot_checksum(head[0x1C:])   # 쓸 때는 맞는 값

    def test_an_empty_slot_is_left_alone(self):
        """0xFFFF 로 시작하는 슬롯은 한 번도 쓰인 적이 없다. 올리면 채워진 척만 한다."""
        cfg = get_device_config("HEM-7386T1")
        memory = bytearray(0x400)
        memory[0x0010 : 0x0010 + 0x30] = _settings(0x1C, b"\xff" * 10)
        target = _session(cfg, memory=bytes(memory))
        _commit(target)
        _, head = target.writes[0]
        assert head[0x1C : 0x1C + 10] == b"\xff" * 10
        assert head[4:6] == b"\x00\x80"                       # 인덱스는 그래도 초기화

    def test_the_clock_block_is_the_shared_one(self):
        cfg = get_device_config("HEM-7386T1")
        target = _session(cfg)
        _commit(target)
        _, clock = target.writes[1]
        assert clock[4] & 0x01
        assert clock[14] == sum(clock[:14]) & 0xFF
        assert clock_block(bytes(range(28)), datetime(2026, 9, 13, 8, 0, 0), 16)[8:14] == bytes(
            (26, 9, 13, 8, 0, 0)
        )


class TestDerivedFromTheProfile:
    def test_every_address_comes_from_the_catalog_fields(self):
        cfg = get_device_config("HEM-7386T1")
        layout = SettingsMirrorLayout(cfg)
        assert layout.head_read_address == cfg.settings_read_address == 0x0010
        assert layout.head_read_size == cfg.settings_time_sync_bytes[0] == 0x30
        assert layout.head_write_address == cfg.settings_write_address == 0x0058
        assert layout.clock_read_address == 0x0040
        assert layout.clock_write_address == 0x0088
        assert layout.clock_write_size == 16

    def test_a_shifted_profile_moves_the_writes_with_it(self):
        cfg = SimpleNamespace(
            model="synthetic",
            pairing_registration=PairingRegistration(
                slot_offset=0x1C, unread_clears=((0x04, 0x8000),)
            ),
            settings_read_address=0x0100,
            settings_write_address=0x0200,
            settings_time_sync_bytes=[0x30, 0x40],
            transmission_block_size=0x38,
            index_pointer_layout={"index_region_byte_size": 0x1C},
        )
        target = _session(cfg)
        _commit(target)
        assert [addr for addr, _ in target.writes] == [0x0200, 0x0230]

    def test_a_counter_outside_the_index_region_is_refused(self):
        cfg = SimpleNamespace(
            model="broken",
            pairing_registration=PairingRegistration(
                slot_offset=0x18, unread_clears=((0x17, 0x8000),)   # 2B 가 슬롯을 침범
            ),
            settings_read_address=0x0010,
            settings_write_address=0x0054,
            settings_time_sync_bytes=[0x2C, 0x3C],
            transmission_block_size=0x38,
            index_pointer_layout={"index_region_byte_size": 0x18},
        )
        target = _session(cfg)
        with pytest.raises(ConnectionError, match="outside the index region"):
            _commit(target)
        assert target.writes == []

    def test_the_driver_names_no_model(self):
        source = (_COMPONENT / "omron_ble" / "omron_driver.py").read_text(encoding="utf-8")
        assert "HEM-7386T1" not in source
        assert "resolve_profile_model_id" not in source

    def test_the_profiles_carry_their_own_counter_lists(self):
        """같은 지오메트리라도 스트림 수는 다르다 — 7386T1 여덟, 7376T1 여섯."""
        seven = get_device_config("HEM-7386T1").pairing_registration
        assert seven.slot_offset == 0x1C
        assert [o for o, _ in seven.unread_clears] == [0x04, 0x06, 0x11, 0x13, 0x16, 0x18, 0x19, 0x1B]
        assert get_device_config("HEM-7382T1-AZAZ").pairing_registration == seven
        for sibling in ("HEM-7376T1", "HEM-7377T1"):
            reg = get_device_config(sibling).pairing_registration
            assert reg.slot_offset == 0x1C
            assert [o for o, _ in reg.unread_clears] == [0x04, 0x06, 0x11, 0x13, 0x19, 0x1B]
        eighty = get_device_config("HEM-7380T1").pairing_registration
        assert eighty.slot_offset == 0x18
        assert [o for o, _ in eighty.unread_clears] == [0x04, 0x06, 0x11, 0x13, 0x15, 0x17]
        # 두 혈압 스트림만 2바이트, 나머지는 1바이트 유휴값.
        for reg in (seven, eighty):
            assert {v for o, v in reg.unread_clears if o in (4, 6)} == {0x8000}
            assert {v for o, v in reg.unread_clears if o not in (4, 6)} == {0x80}

    def test_profiles_without_evidence_stay_off(self):
        for other in ("HEM-7155T-MW3", "HEM-7188T1-LEO", "HEM-7142T2", "HEM-7196T1"):
            assert get_device_config(other).pairing_registration is None, other

    def test_hem_7380t1_writes_the_mw3_shaped_block(self):
        cfg = get_device_config("HEM-7380T1")
        memory = bytearray(0x400)
        memory[0x0010 : 0x0010 + 0x2C] = _settings(0x18, _populated_slot(3), size=0x2C)
        target = _session(cfg, memory=bytes(memory))
        _commit(target)
        (head_addr, head), (clock_addr, clock) = target.writes
        assert head_addr == 0x0054 and len(head) == 34
        assert clock_addr == 0x0080 and len(clock) == 16
        assert head_addr + len(head) <= 0x01C4 and clock_addr + len(clock) <= 0x01C4
        assert head[0x18 : 0x18 + 10] == _populated_slot(4)
        assert head[0x11] == 0x80 and head[0x17] == 0x80


class TestWhenItRuns:
    def test_not_on_an_ordinary_session(self):
        target = _session(get_device_config("HEM-7386T1"), pairing=False)
        assert _commit(target) is False and target.writes == []

    def test_not_on_a_profile_without_it(self):
        target = _session(get_device_config("HEM-7155T-MW3"))
        assert _commit(target) is False and target.writes == []

    def test_once_per_link(self):
        target = _session(get_device_config("HEM-7386T1"))
        assert _commit(target) is True
        assert _commit(target) is False
        assert len(target.writes) == 2

    def test_needs_an_open_memory_session(self):
        target = _session(get_device_config("HEM-7386T1"), memory_active=False)
        with pytest.raises(ConnectionError, match="memory session"):
            _commit(target)


class TestItNeverBlocksTheClose:
    def test_close_memory_session_has_no_registration_hook(self):
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
        assert call is not None
        assert any(
            isinstance(node, ast.Try)
            and node.lineno <= call.lineno <= getattr(node, "end_lineno", node.lineno)
            for node in ast.walk(fn)
        )
