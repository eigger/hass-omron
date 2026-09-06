"""페어링 창에서는 남아 있는 호스트 본드를 먼저 지운다 (#24, #67, #91, #92).

호스트에 본드가 있으면 스택은 SMP 를 돌리지 않고 저장된 LTK 로 **암호화를
재개**한다. BlueZ 는 한 술 더 떠 ``Device1.Paired`` 가 참이면 ``Pair()`` 가
아무것도 주고받지 않고 즉시 돌아온다. 커프가 그 키를 이미 버렸다면 (다른 폰
재등록, 리셋, 본드 슬롯 축출) 커프는 "PIN or Key Missing" 으로 끊는다. 이때는
사용자가 -P- 를 눌러도 소용이 없다 — 페어링 요청 자체가 나가지 않기 때문이다.

그래서 **페어링 플로우에서만** 본드를 먼저 지워 SMP 경로를 되살린다. 평범한
재연결에서 지우면 멀쩡한 본드를 버리는 것이므로 반대로 해롭다.

conftest 가 homeassistant/bleak 를 MagicMock 으로 치환해 실제로 돌릴 수 없어
(test_stale_bond_guard.py 와 같은 이유) AST 로 검사한다.
"""
import ast
from pathlib import Path

_DRIVER = (
    Path(__file__).resolve().parent.parent
    / "custom_components" / "omron" / "omron_ble" / "omron_driver.py"
)


def _tree() -> ast.AST:
    return ast.parse(_DRIVER.read_text(encoding="utf-8"))


def _find_function(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} 을(를) 찾지 못했다 — 이름을 바꿨다면 이 테스트도 갱신할 것")


def _calls(fn: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == name
    ]


def _enclosing_ifs(fn: ast.AST, target_lineno: int) -> list[ast.If]:
    """해당 줄을 몸통에 품고 있는 if 문들."""
    found = []
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and node.lineno < target_lineno:
            if target_lineno <= getattr(node, "end_lineno", node.lineno):
                found.append(node)
    return found


def test_the_bond_is_cleared_in_the_pairing_flow():
    fn = _find_function(_tree(), "establish_connection_with_bond_settle")
    assert _calls(fn, "_clear_bond_before_pairing"), (
        "페어링 전에 본드를 지우지 않는다 — 오래된 본드가 남아 있으면 스택이 "
        "SMP 를 아예 시작하지 않는다"
    )


def test_an_ordinary_reconnect_keeps_its_bond():
    """가드가 없으면 매 재연결마다 본드를 버려 커프를 못 쓰게 만든다."""
    fn = _find_function(_tree(), "establish_connection_with_bond_settle")
    for call in _calls(fn, "_clear_bond_before_pairing"):
        guards = " ".join(ast.unparse(node.test) for node in _enclosing_ifs(fn, call.lineno))
        assert "pair_this_attempt" in guards, (
            f"본드 제거가 pair_this_attempt 안에 있지 않다 (guards={guards!r}). "
            "평범한 재연결에서 지우면 멀쩡한 본드를 버린다."
        )


def test_the_bond_is_cleared_before_the_link_is_opened():
    """연결한 뒤에 지우면 이미 암호화 재개 경로를 탄 뒤라 늦는다."""
    fn = _find_function(_tree(), "establish_connection_with_bond_settle")
    clears = [call.lineno for call in _calls(fn, "_clear_bond_before_pairing")]
    connects = [call.lineno for call in _calls(fn, "establish_connection")]
    assert clears and connects, "호출이 사라졌다 — 테스트가 낡았다"
    assert min(clears) < min(connects), (
        "본드 제거가 첫 establish_connection 뒤에 있다"
    )


def test_nothing_is_removed_when_no_bond_exists():
    """없는 본드를 지우겠다고 기기 객체를 날리면 페어링 창만 낭비한다 (#92)."""
    fn = _find_function(_tree(), "_clear_bond_before_pairing")
    probes = [call.lineno for call in _calls(fn, "_bluez_is_paired")]
    removals = [call.lineno for call in _calls(fn, "_bluez_remove_device")]
    assert probes and removals, "호출이 사라졌다 — 테스트가 낡았다"
    assert min(probes) < min(removals), "본드 존재 여부를 묻기 전에 지운다"
    assert any(
        isinstance(node, ast.Return) and node.value is None for node in ast.walk(fn)
    ), "본드가 없을 때 그냥 빠져나오는 경로가 없다"


def test_the_daemon_is_given_time_to_forget():
    """RemoveDevice 는 즉시 반영되지 않는다 — 바로 연결하면 그대로 재개 경로다."""
    fn = _find_function(_tree(), "_clear_bond_before_pairing")
    assert any(isinstance(node, ast.While) for node in ast.walk(fn)), (
        "제거 완료를 기다리지 않는다"
    )
