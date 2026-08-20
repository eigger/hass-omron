"""페어링 세션 핸드오프 회귀 테스트 (discussions#119).

WLD3.0 계열 커프(HEM-7380T1 / 7188T1 등)는 페어링 세션 중에만 데이터를 주고
이후의 새 연결은 거부한다 — device_catalog._WLD3_BOND_POLICY 주석 참고.
그래서 페어링 직후 링크를 닫고 폴링이 새로 연결하면 그 연결이 타임아웃난다.

config flow(최초 등록) 경로는 이미 세션을 _setup_sessions 에 넘겨 첫 폴이
같은 링크를 재사용하지만, 자동 페어링(__init__.py)과 재페어링 버튼(button.py)
경로는 세션을 버리고 재연결했다. 그 배선 누락을 고정한다.

conftest 가 homeassistant/bleak 를 MagicMock 으로 치환해 실제 인스턴스를 만들 수
없으므로(test_poll_deadline.py 와 동일한 제약), 순수 헬퍼는 런타임으로, HA 에
얽힌 호출부 배선은 AST 로 검사한다.
"""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

_COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "omron"


def _parse(relative_path: str) -> ast.Module:
    return ast.parse((_COMPONENT / relative_path).read_text(encoding="utf-8"))


def _find_async_function(tree: ast.AST, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f"{name} 을(를) 찾지 못했다 — 이름을 바꿨다면 이 테스트도 함께 갱신할 것"
    )


def _find_method(tree: ast.AST, class_name: str, method_name: str) -> ast.AsyncFunctionDef:
    """클래스를 지정해 메서드를 찾는다 — 같은 이름의 메서드가 여러 클래스에 있다."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return _find_async_function(node, method_name)
    raise AssertionError(
        f"{class_name} 을(를) 찾지 못했다 — 이름을 바꿨다면 이 테스트도 함께 갱신할 것"
    )


def _called_names(node: ast.AST) -> set[str]:
    return {
        ast.unparse(sub.func)
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call)
    }


class _FakeSession:
    """release/reclaim/aclose 호출 순서를 기록하는 최소 세션 스텁."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def release_for_handoff(self) -> "_FakeSession":
        self.events.append("release")
        return self

    def reclaim_ownership(self) -> None:
        self.events.append("reclaim")

    async def aclose(self) -> None:
        self.events.append("aclose")


def _fake_hass() -> SimpleNamespace:
    return SimpleNamespace(data={})


class TestHandoffHelpers:
    """stash/discard 헬퍼의 소유권 처리."""

    def test_stash_releases_ownership_and_parks_the_session(self):
        from custom_components.omron.ble_session import stash_handoff_session
        from custom_components.omron.const import DOMAIN

        hass = _fake_hass()
        session = _FakeSession()

        stash_handoff_session(hass, "AA:BB:CC:DD:EE:FF", session)

        assert session.events == ["release"]
        parked = hass.data[DOMAIN]["_setup_sessions"]
        assert parked["AA:BB:CC:DD:EE:FF"] is session

    def test_discard_reclaims_before_closing(self):
        """release_for_handoff() 가 끊을 책임을 비웠으므로, 되찾지 않고 aclose()
        하면 링크가 살아남는다. reclaim 이 aclose 보다 먼저여야 한다."""
        from custom_components.omron.ble_session import (
            discard_handoff_session,
            stash_handoff_session,
        )
        from custom_components.omron.const import DOMAIN

        hass = _fake_hass()
        session = _FakeSession()
        stash_handoff_session(hass, "AA:BB:CC:DD:EE:FF", session)

        asyncio.run(discard_handoff_session(hass, "AA:BB:CC:DD:EE:FF"))

        assert session.events == ["release", "reclaim", "aclose"]
        assert hass.data[DOMAIN]["_setup_sessions"] == {}

    def test_discard_is_a_noop_once_the_poll_adopted_the_session(self):
        """폴이 이미 pop 해간 뒤의 정리 호출은 조용히 통과해야 한다."""
        from custom_components.omron.ble_session import discard_handoff_session

        hass = _fake_hass()
        asyncio.run(discard_handoff_session(hass, "AA:BB:CC:DD:EE:FF"))

    def test_discard_swallows_close_errors(self):
        """정리 실패가 페어링 성공을 뒤엎으면 안 된다."""
        from custom_components.omron.ble_session import (
            discard_handoff_session,
            stash_handoff_session,
        )

        class _ExplodingSession(_FakeSession):
            async def aclose(self) -> None:
                raise RuntimeError("link already gone")

        hass = _fake_hass()
        stash_handoff_session(hass, "AA:BB:CC:DD:EE:FF", _ExplodingSession())

        asyncio.run(discard_handoff_session(hass, "AA:BB:CC:DD:EE:FF"))


class TestRetryPairingReturnsSession:
    """async_retry_pairing 은 링크를 닫지 말고 넘겨줘야 한다."""

    def test_returns_the_session_instead_of_none(self):
        fn = _find_async_function(_parse("omron_ble/parser.py"), "async_retry_pairing")

        assert fn.returns is not None and "OmronDeviceSession" in ast.unparse(
            fn.returns
        ), "async_retry_pairing 은 살아있는 세션을 반환해야 한다"

        returned = [
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.Return) and node.value is not None
        ]
        assert returned, (
            "세션을 반환하지 않으면 호출부가 넘겨받을 링크가 없다 — "
            "페어링 직후 재연결로 되돌아간다"
        )
        assert any(
            "release_for_handoff" in ast.unparse(node.value) for node in returned
        ), "반환 전에 release_for_handoff() 로 끊을 책임을 넘겨야 한다"

    def test_leaves_the_memory_session_open_for_the_poll(self):
        """폴이 같은 링크에서 바로 읽도록 readout 세션을 열어둔 채 넘긴다."""
        fn = _find_async_function(_parse("omron_ble/parser.py"), "async_retry_pairing")

        keywords = [
            kw
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "leave_memory_session_open"
        ]
        assert keywords, "async_sync_device_time 에 leave_memory_session_open 을 넘겨야 한다"
        assert any(
            isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in keywords
        )

    def test_closes_the_session_when_pairing_fails(self):
        """실패 시엔 링크를 반드시 닫아야 한다 — 안 그러면 세션이 샌다."""
        fn = _find_async_function(_parse("omron_ble/parser.py"), "async_retry_pairing")

        handlers = [node for node in ast.walk(fn) if isinstance(node, ast.ExceptHandler)]
        closing = [h for h in handlers if "aclose" in ast.unparse(h)]
        assert closing, "예외 경로에서 session.aclose() 를 호출해야 한다"
        assert any(
            any(isinstance(sub, ast.Raise) for sub in ast.walk(h)) for h in closing
        ), "정리 후 원래 예외를 다시 올려야 한다"


class TestCallSitesHandOffTheSession:
    """세션을 만든 두 경로 모두 넘기고, 안 쓰이면 정리해야 한다."""

    def test_auto_pairing_hands_off_and_cleans_up(self):
        fn = _find_async_function(_parse("__init__.py"), "_run_auto_session")
        called = _called_names(fn)

        assert "stash_handoff_session" in called, (
            "광고 트리거 자동 페어링이 세션을 버리면 이어지는 폴이 재연결하고, "
            "PER_SESSION 커프는 그 두 번째 연결을 거부한다"
        )
        assert "discard_handoff_session" in called, (
            "폴이 채가지 않은 세션은 닫아야 한다"
        )

    def test_retry_pairing_button_hands_off_and_cleans_up(self):
        fn = _find_method(
            _parse("button.py"), "OmronRetryPairingButtonEntity", "async_press"
        )
        called = _called_names(fn)

        assert "stash_handoff_session" in called
        assert "discard_handoff_session" in called

    def test_config_flow_uses_the_shared_helper(self):
        """세 경로가 같은 헬퍼를 쓰도록 고정 — 배선이 또 갈라지지 않게."""
        fn = _find_async_function(_parse("config_flow.py"), "_async_do_pairing")
        assert "stash_handoff_session" in _called_names(fn)

    def test_poll_cleanup_reclaims_ownership_before_closing(self):
        """넘겨받은 세션을 폴이 채가지 못했을 때, 소유권을 되찾아야 실제로 끊긴다."""
        fn = _find_async_function(_parse("__init__.py"), "_async_poll_data")
        called = _called_names(fn)

        assert any(name.endswith("reclaim_ownership") for name in called), (
            "aclose() 는 _owns_connection 이 False 면 링크를 끊지 않는다"
        )
