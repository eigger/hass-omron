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
from types import SimpleNamespace

import pytest

from custom_components.omron.omron_ble.devices import (
    HostPairingMode,
    UnlockMode,
    get_device_config,
)
from custom_components.omron.omron_ble.secure_flow import SecureInitLayout

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
