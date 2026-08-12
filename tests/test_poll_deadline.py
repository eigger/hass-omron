"""폴 데드라인 회귀 테스트 (hass-omron#110).

bleak 의 BlueZ 백엔드는 read/write/notify D-Bus 호출에 타임아웃을 걸지 않는다
(경계가 있는 건 disconnect 뿐이다). 그래서 bluetoothd 가 물리면 폴 하나가
영원히 대기하고, 그 폴이 session_lock 을 쥔 채로 남아 이후의 모든 예약 폴 /
Refresh Data / 광고 트리거가 "lock held" 로 조용히 빠져나간다. 로그도 안 남고
HA 재시작 전까지 통합이 죽는다.

이를 막는 두 불변조건을 소스 구조 수준에서 고정한다:

1. _async_poll_data 는 async_poll 을 asyncio.timeout 으로 감싼다.
2. async_poll 의 예외 핸들러는 CancelledError 를 삼키지 않는다. asyncio.timeout
   은 마감 시각에 태스크를 딱 한 번만 취소하므로, 그 CancelledError 를 삼키면
   루프가 다음 무경계 대기로 넘어가고 다시 발동할 데드라인이 남지 않는다.

conftest 가 homeassistant/bleak 를 MagicMock 으로 치환하는 탓에 실제 인스턴스를
만들 수 없어(클래스 자체가 MagicMock 이 된다) 런타임 대신 AST 로 검사한다.
"""
import ast
from pathlib import Path

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


def _caught_names(handler: ast.ExceptHandler) -> list[str]:
    if handler.type is None:
        return ["<bare except>"]
    caught = (
        handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    return [ast.unparse(node) for node in caught]


class TestPollDeadline:
    """예약 폴은 반드시 유한 시간 안에 session_lock 을 돌려줘야 한다."""

    def test_poll_is_wrapped_in_asyncio_timeout(self):
        fn = _find_async_function(_parse("__init__.py"), "_async_poll_data")
        wrapped = [
            item
            for node in ast.walk(fn)
            if isinstance(node, ast.AsyncWith)
            for item in node.items
            if isinstance(item.context_expr, ast.Call)
            and ast.unparse(item.context_expr.func) == "asyncio.timeout"
        ]
        assert wrapped, (
            "_async_poll_data 는 async_poll 을 asyncio.timeout 으로 감싸야 한다. "
            "감싸지 않으면 무경계 GATT 대기가 session_lock 을 영구 점유한다."
        )

    def test_poll_timeout_constant_is_positive(self):
        tree = _parse("__init__.py")
        values = [
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "POLL_TIMEOUT_SECONDS"
                for target in node.targets
            )
        ]
        assert values, "POLL_TIMEOUT_SECONDS 상수가 모듈 최상단에 있어야 한다"
        assert values[0] > 0


class TestCancellationEscapesPoll:
    """데드라인이 보낸 취소는 async_poll 밖으로 빠져나가야 한다."""

    def test_async_poll_does_not_swallow_cancellation(self):
        fn = _find_async_function(_parse("omron_ble/parser.py"), "async_poll")

        swallowed: list[str] = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.ExceptHandler):
                continue
            # 다시 raise 하는 핸들러는 취소가 그대로 전파되므로 문제없다.
            if any(isinstance(stmt, ast.Raise) for stmt in ast.walk(node)):
                continue
            swallowed += [
                name
                for name in _caught_names(node)
                if name
                in ("BaseException", "<bare except>", "CancelledError",
                    "asyncio.CancelledError")
            ]

        assert not swallowed, (
            f"async_poll 이 {swallowed} 을(를) 삼키고 계속 진행한다. "
            "CancelledError 를 삼키면 폴 데드라인이 무력화된다 — Exception 으로 좁힐 것."
        )
