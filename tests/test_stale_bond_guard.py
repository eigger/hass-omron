"""본드가 없으면 "오래된 본드"도 없다 (#92).

AuthenticationFailed 는 두 가지를 뜻할 수 있다.

- 로컬에 본드가 있는데 기기가 그걸 잊었다 → 지우고 다음 연결에서 다시 맺는
  것이 맞다 (#83).
- 본드가 아예 없는데 상대가 페어링을 거절했다 → 지울 것이 없고, 여기서
  포기하면 사용자가 버튼을 눌러 연 페어링 창을 그대로 버린다. 예전 코드가
  안내하던 "retry the poll" 은 그 창이 닫힌 뒤에 도착한다.

둘을 구분하지 않으면 후자가 전자로 처리된다. BlueZ 에 Paired 를 직접 물어서
가른다.

conftest 가 homeassistant/bleak 를 MagicMock 으로 치환해 실제로 돌릴 수 없어
(test_poll_deadline.py 와 같은 이유) AST 로 검사한다.
"""
import ast
from pathlib import Path

_DRIVER = (
    Path(__file__).resolve().parent.parent
    / "custom_components" / "omron" / "omron_ble" / "omron_driver.py"
)


def _find_function(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} 을(를) 찾지 못했다 — 이름을 바꿨다면 이 테스트도 갱신할 것")


def _enclosing_ifs(fn: ast.AST, target_lineno: int) -> list[ast.If]:
    """해당 줄을 몸통에 품고 있는 if 문들."""
    found = []
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and node.lineno < target_lineno:
            end = getattr(node, "end_lineno", node.lineno)
            if target_lineno <= end:
                found.append(node)
    return found


def test_the_bond_is_only_removed_when_one_exists():
    tree = ast.parse(_DRIVER.read_text(encoding="utf-8"))
    fn = _find_function(tree, "_pair_os_bonding")

    removals = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "_bluez_remove_device"
    ]
    assert removals, "_pair_os_bonding 이 본드를 지우지 않는다 — 테스트가 낡았다"

    for call in removals:
        guards = " ".join(ast.unparse(node.test) for node in _enclosing_ifs(fn, call.lineno))
        assert "had_bond" in guards, (
            "본드 제거가 had_bond 검사 안에 있지 않다. 본드가 없는데 지우면 "
            "페어링 창을 버린다 (#92)."
        )


def test_the_bond_state_is_read_before_removing():
    tree = ast.parse(_DRIVER.read_text(encoding="utf-8"))
    fn = _find_function(tree, "_pair_os_bonding")

    def _first_lineno(name: str) -> int:
        lines = [
            node.lineno
            for node in ast.walk(fn)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == name
        ]
        assert lines, f"{name} 호출이 없다"
        return min(lines)

    assert _first_lineno("_bluez_is_paired") < _first_lineno("_bluez_remove_device"), (
        "본드 존재 여부를 묻기 전에 지운다"
    )


def test_a_refused_first_pairing_retries_inside_the_window():
    """거절이 곧 포기가 되면 -P- 창을 한 번밖에 못 쓴다."""
    source = _DRIVER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = _find_function(tree, "_pair_os_bonding")
    assert any(isinstance(node, ast.Continue) for node in ast.walk(fn)), (
        "거절 경로에 continue 가 없다 — 아래 비치명 분기가 첫 거절에서 "
        "성공으로 반환해 버린다"
    )


def test_the_probe_answers_none_when_it_cannot_tell():
    """모르면 모른다고 해야 예전 동작으로 안전하게 떨어진다."""
    tree = ast.parse(_DRIVER.read_text(encoding="utf-8"))
    fn = _find_function(tree, "_bluez_is_paired")
    returns = {
        ast.unparse(node.value) if node.value is not None else "None"
        for node in ast.walk(fn)
        if isinstance(node, ast.Return)
    }
    assert "None" in returns, "판단 불가를 None 으로 돌려주지 않는다"
