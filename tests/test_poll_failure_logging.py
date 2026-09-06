"""평범한 연결 실패가 빨간 트레이스백으로 올라가지 않게 한다 (#133).

``async_poll`` 은 예외를 두 갈래로 나눈다.

- ``ConnectionError`` → warning 한 줄. 커프가 꺼져 있거나, 범위 밖이거나,
  세션 도중 링크가 끊긴 평범한 경우.
- 그 외 ``Exception`` → ``exc_info`` 를 붙인 ERROR. Home Assistant 가
  "This error originated from a custom integration" 배너와 트레이스백으로
  띄운다.

settle-drop 은 전자인데 ``BleakError`` 로 던져지고 있었다. ``BleakError`` 는
``Exception`` 을 직접 상속하므로 후자로 떨어졌고, 서랍에 들어 있는 커프가
매 폴마다 오류로 보고됐다. 어느 쪽이든 예외는 위로 가지 않고
``_finish_update()`` 가 마지막 값을 돌려주므로, 이건 분류의 문제다.

conftest 가 homeassistant/bleak 를 MagicMock 으로 치환해 실제로 돌릴 수 없어
(test_stale_bond_guard.py 와 같은 이유) AST 로 검사한다.
"""
import ast
from pathlib import Path

_COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "omron"
_DRIVER = _COMPONENT / "omron_ble" / "omron_driver.py"
_PARSER = _COMPONENT / "omron_ble" / "parser.py"


def _function(path: Path, name: str):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} 을(를) 찾지 못했다 — 이름을 바꿨다면 이 테스트도 갱신할 것")


def test_a_settle_drop_is_a_connection_error():
    fn = _function(_DRIVER, "establish_connection_with_bond_settle")
    raised = {
        node.exc.func.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
    }
    assert "ConnectionError" in raised, (
        "settle-drop 이 ConnectionError 가 아니다 — 서랍 속 커프가 매 폴마다 "
        "트레이스백을 남긴다"
    )
    assert "BleakError" not in raised


def test_the_poll_sorts_the_ordinary_case_from_the_unexpected_one():
    """두 갈래가 남아 있어야 위 구분이 의미를 갖는다."""
    fn = _function(_PARSER, "async_poll")
    handlers = [
        ast.unparse(h.type) if h.type is not None else "bare"
        for node in ast.walk(fn)
        if isinstance(node, ast.Try)
        for h in node.handlers
    ]
    assert "ConnectionError" in handlers
    assert "Exception" in handlers


def test_a_failed_poll_still_returns_the_last_values():
    """예외를 위로 보내면 엔티티가 unavailable 이 된다 — 처음부터의 설계다."""
    fn = _function(_PARSER, "async_poll")
    tries = [node for node in ast.walk(fn) if isinstance(node, ast.Try)]
    assert tries, "async_poll 에 try 가 없다"
    outermost = min(tries, key=lambda node: node.lineno)
    handled = {
        ast.unparse(h.type) if h.type is not None else "bare" for h in outermost.handlers
    }
    assert "Exception" in handled, "가장 바깥 try 가 예외를 삼키지 않는다"
    # 반환은 핸들러 뒤, try 바깥에 있어야 실패한 폴도 캐시를 돌려준다.
    returns = [
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Return)
        and node.value is not None
        and "_finish_update" in ast.unparse(node.value)
    ]
    assert returns, "_finish_update() 반환이 없다"
    assert max(returns) > getattr(outermost, "end_lineno", outermost.lineno), (
        "_finish_update() 가 try 안에 있다 — 예외 경로가 값을 돌려주지 못한다"
    )
