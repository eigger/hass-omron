"""SECURE_SESSION 프로파일의 자격증명 왕복 (#24).

이 전송 방식의 기기는 BLE 본드와 별개로 **애플리케이션 계층 자격증명**을 갖는다.
첫 세션이 초기화를 마치고 기기가 close 를 수락했을 때만 자격증명을 남기고, 이후
세션은 그걸 그대로 재생하며 아무것도 쓰지 않는다. 자격증명을 잃으면 사용자가
커프의 -P- 창을 다시 거쳐야 하므로, 저장 경로가 끊기지 않는 것이 중요하다.

conftest 가 homeassistant/bleak 를 MagicMock 으로 치환해 통합 계층은 실행할 수
없어 (test_stale_bond_guard.py 와 같은 이유) 그쪽은 AST 로 검사한다.
"""
import ast
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace

import pytest

from custom_components.omron.omron_ble.devices import (
    HostPairingMode,
    UnlockMode,
    get_device_config,
)
from custom_components.omron.omron_ble.secure_flow import (
    SecureInitLayout,
    clock_block,
)

_ROOT = Path(__file__).resolve().parent.parent
_COMPONENT = _ROOT / "custom_components" / "omron"


def _tree(relative: str) -> ast.AST:
    return ast.parse((_COMPONENT / relative).read_text(encoding="utf-8"))


class TestSecureInitLayout:
    """초기화 주소는 프로파일에서 유도된다 — 한 기종에 박아넣지 않는다."""

    def test_addresses_come_from_the_profile(self):
        cfg = get_device_config("HEM-7188T1-LEO")
        layout = SecureInitLayout(cfg)
        # 읽기/쓰기 영역은 카탈로그의 settings 주소 그대로,
        # 시계 레코드는 그 안의 time-sync 오프셋에 얹힌다.
        assert layout.head_read_address == cfg.settings_read_address
        assert layout.head_write_address == cfg.settings_write_address
        assert layout.clock_read_address == cfg.settings_read_address + 0x2C
        assert layout.clock_write_address == cfg.settings_write_address + 0x2C
        assert layout.clock_write_size == 0x3C - 0x2C

    def test_a_different_profile_shape_moves_every_address(self):
        """주소가 상수로 굳어 있으면 이 테스트가 잡는다."""
        other = SimpleNamespace(
            model="synthetic",
            settings_read_address=0x0100,
            settings_write_address=0x0200,
            settings_time_sync_bytes=[0x10, 0x20],
            index_pointer_layout={"index_region_byte_size": 0x08},
        )
        layout = SecureInitLayout(other)
        assert layout.head_read_address == 0x0100
        assert layout.head_read_size == 0x10
        assert layout.head_write_address == 0x0200
        assert layout.head_write_size == 0x08
        assert layout.clock_read_address == 0x0110
        assert layout.clock_write_address == 0x0210
        assert layout.clock_write_size == 0x10

    def test_the_clock_read_always_covers_what_is_written_back(self):
        """짧게 읽으면 head 쓰기가 끝난 뒤에 ValueError 로 죽는다.

        시계 레코드 크기는 time-sync 범위에서 오고 읽기 길이는 인덱스 영역
        크기에서 왔는데, 후자가 더 작은 배치에서는 되쓸 레코드를 만들 수조차
        없다 — 그것도 기기 메모리를 이미 한 번 쓴 뒤에.
        """
        for index_size, time_sync in (
            (0x08, [0x10, 0x20]),   # 인덱스 영역이 레코드보다 작은 배치
            (0x18, [0x2C, 0x3C]),   # HEM-7188T1-LEO
            (0x40, [0x2C, 0x3C]),   # 인덱스 영역이 훨씬 큰 배치
        ):
            cfg = SimpleNamespace(
                model="synthetic",
                settings_read_address=0x0100,
                settings_write_address=0x0200,
                settings_time_sync_bytes=time_sync,
                index_pointer_layout={"index_region_byte_size": index_size},
            )
            layout = SecureInitLayout(cfg)
            assert layout.clock_read_size >= layout.clock_write_size
            # 실제로 만들어봐야 의미가 있다: 주소만 검사하면 이 버그를 놓친다.
            block = clock_block(
                bytes(layout.clock_read_size), datetime(2026, 9, 6, 12, 30, 45),
                layout.clock_write_size,
            )
            assert len(block) == layout.clock_write_size

    def test_the_checksum_sits_at_the_end_of_the_record(self):
        """체크섬 위치를 14 로 박아두면 16바이트 레코드에서만 우연히 맞는다."""
        for size in (16, 20, 24):
            block = clock_block(bytes(range(size)), datetime(2026, 9, 6, 12, 30, 45), size)
            assert block[size - 2] == sum(block[: size - 2]) & 0xFF
        # #67 캡처의 실제 레코드로 규칙 자체를 고정한다.
        captured = bytes.fromhex("c8a80000010000001a06110f1806cf00")
        assert captured[14] == sum(captured[:14]) & 0xFF

    def test_an_incomplete_profile_is_rejected(self):
        bare = SimpleNamespace(
            model="bare",
            settings_read_address=None,
            settings_write_address=None,
            settings_time_sync_bytes=None,
            index_pointer_layout=None,
        )
        with pytest.raises(ValueError, match="secure initialization"):
            SecureInitLayout(bare)


class TestOneSecurePath:
    """SECURE_SESSION 은 하나의 경로여야 한다 — 기종별 분기를 두지 않는다."""

    def test_the_driver_has_no_model_whitelist(self):
        source = (_COMPONENT / "omron_ble" / "omron_driver.py").read_text(encoding="utf-8")
        assert "HEM-7188T1-LEO" not in source, (
            "드라이버가 특정 기종 이름으로 분기한다 — 프로파일 필드로 표현할 것"
        )

    def test_the_secure_flow_dispatches_on_the_unlock_mode_alone(self):
        fn = None
        for node in ast.walk(_tree("omron_ble/omron_driver.py")):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "unlock":
                fn = node
        assert fn is not None, "unlock() 을 찾지 못했다"
        calls = {
            node.func.attr
            for node in ast.walk(fn)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "_secure_unlock" in calls
        assert "_x2_unlock" not in calls, "기종 전용 unlock 이 남아 있다"


class TestPairingIsOptional:
    """커프가 스스로 보안을 주도하는 프로파일에서는 Pair() 를 부르지 않는다."""

    def test_the_profile_holds_an_agent_instead_of_pairing(self):
        cfg = get_device_config("HEM-7188T1-LEO")
        assert cfg.host_pairing_mode is HostPairingMode.NONE
        assert cfg.unlock_mode is UnlockMode.SECURE_SESSION
        # 에이전트가 없으면 BlueZ 5.72+ 는 Just Works 확인을 방치한다.
        assert cfg.register_pairing_agent is True

    def test_the_agent_is_held_for_the_session_not_just_the_connect(self):
        """핸드셰이크 시점에 커프가 Security Request 를 올리면 답할 주체가 있어야
        한다. 하드웨어로 검증된 순서도 세션 내내 에이전트를 붙잡고 있었다."""
        fn = None
        for node in ast.walk(_tree("omron_ble/omron_driver.py")):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "connect":
                fn = node
        assert fn is not None, "connect() 를 찾지 못했다"
        body = ast.unparse(fn)
        assert "_bluez_pairing_agent()" in body, (
            "connect() 가 에이전트를 잡지 않는다 — establish_connection 안에서만 "
            "유지되면 이후 핸드셰이크의 보안 요청에 답할 주체가 없다"
        )
        assert "register_pairing_agent" in body

        released = None
        for node in ast.walk(_tree("omron_ble/omron_driver.py")):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "aclose":
                released = ast.unparse(node)
        assert released is not None, "aclose() 를 찾지 못했다"
        assert "_pairing_agent" in released, "세션이 끝나도 에이전트를 놓지 않는다"

    def test_pair_is_a_no_op_rather_than_an_error(self):
        """예외를 던지면 평범한 setup/재시도 경로가 전부 특수 분기를 져야 한다."""
        fn = None
        for node in ast.walk(_tree("omron_ble/omron_driver.py")):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "pair":
                fn = node
        assert fn is not None, "pair() 를 찾지 못했다"
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            test = ast.unparse(node.test)
            if "HostPairingMode.NONE" not in test:
                continue
            body = ast.unparse(node.body)
            assert "raise" not in body, f"NONE 분기가 여전히 예외를 던진다: {body}"
            assert "return" in body
            break
        else:
            raise AssertionError("pair() 에 HostPairingMode.NONE 분기가 없다")


class TestCredentialRoundTrip:
    """저장 -> 세션 -> 갱신 -> 저장 고리가 어느 지점에서도 끊기면 안 된다."""

    def test_sessions_are_opened_with_the_stored_credential(self):
        fn = None
        for node in ast.walk(_tree("omron_ble/parser.py")):
            if isinstance(node, ast.FunctionDef) and node.name == "_open_session":
                fn = node
        assert fn is not None, "_open_session 을 찾지 못했다"
        assert "self.transport_credential" in ast.unparse(fn), (
            "세션이 저장된 자격증명 없이 열린다 — 재연결이 인증에 실패한다"
        )

    def test_every_session_construction_goes_through_the_helper(self):
        """직접 생성하는 자리가 남으면 그 세션만 자격증명 없이 열린다."""
        built = []
        for node in ast.walk(_tree("omron_ble/parser.py")):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                isinstance(sub, ast.Call)
                and getattr(sub.func, "id", None) == "OmronDeviceSession"
                for sub in ast.walk(node)
            ):
                built.append(node.name)
        assert built == ["_open_session"], f"세션을 직접 여는 자리: {built}"

    def test_the_entry_stores_what_a_pairing_established(self):
        source = (_COMPONENT / "config_flow.py").read_text(encoding="utf-8")
        assert "session.new_credential" in source
        assert "CONF_TRANSPORT_CREDENTIAL" in source

    def test_storing_it_does_not_reload_the_integration(self):
        """엔트리 갱신은 update_listener 를 통해 통합을 리로드한다. 자격증명 쓰기는
        코디네이터의 업데이트 메서드 안에서 일어나므로, 리로드하면 그 폴을 돌리고
        있는 코디네이터를 스스로 무너뜨린다."""
        source = (_COMPONENT / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        listener = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "update_listener":
                listener = node
        assert listener is not None, "update_listener 를 찾지 못했다"
        body = ast.unparse(listener)
        assert "credential_write" in body, (
            "리스너가 자격증명 전용 변경을 구분하지 않는다 — 폴 도중 리로드가 걸린다"
        )
        # 건너뛰는 경로는 리로드에 도달하기 전에 반환해야 한다.
        assert body.index("credential_write") < body.index("async_reload")

        persist = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_persist_transport_credential":
                persist = node
        assert persist is not None
        marked = ast.unparse(persist)
        assert marked.index("credential_write") < marked.index("async_update_entry"), (
            "표시를 엔트리 갱신 뒤에 하면 리스너가 이미 지나간 뒤다"
        )

    def test_setup_loads_it_and_a_poll_writes_it_back(self):
        source = (_COMPONENT / "__init__.py").read_text(encoding="utf-8")
        assert "data.transport_credential = bytes.fromhex" in source, (
            "저장된 자격증명을 파서에 싣지 않는다"
        )
        assert "_persist_transport_credential" in source
        tree = ast.parse(source)
        fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_persist_transport_credential":
                fn = node
        assert fn is not None, "_persist_transport_credential 을 찾지 못했다"
        body = ast.unparse(fn)
        assert "async_update_entry" in body
        # 값이 그대로면 쓰지 않는다: 엔트리 갱신은 통합을 리로드한다.
        assert "return" in body
