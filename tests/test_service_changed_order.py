"""Service Changed 구독은 본딩 뒤에 와야 한다 (#91, #92).

페리페럴은 이 CCCD 설정을 **본드된 클라이언트별로** 보존한다
(Vol 3 Part G, 3.3.3.3). 본드가 아직 없을 때 쓰면 붙일 클라이언트가 없다.

이슈 #67 폰 캡처의 페어링 연결이 그 순서를 보여준다:

    15:23:49.156  LE_Start_Encryption
    15:23:49.540  SMP 0x06/0x07/0x08/0x09   키 배포 완료
    15:23:50.476  WriteReq 0x000B = 0200    ← 0.94초 뒤
    15:23:52.071  WriteReq 0x0021 = 0100    벤더 CCCD 는 그 뒤

두 번째 페어링(19:13)도 같다. 암호화 전에 ATT 가 아예 없는 건 아니지만
(15:23:47.075 에 서비스 디스커버리 읽기가 있다) 그건 읽기고, CCCD 쓰기는
본딩이 끝난 뒤에만 나온다.

우리가 성공했던 실행들도 같은 순서였다 — pair_on_connect 가 연결 시점에
본딩을 끝냈고 구독은 그 1~2초 뒤였다. #142 가 pair_on_connect 를 없앤 뒤
구독이 pair() 앞에 남아 있으면, 한 번도 존재한 적 없는 순서가 된다.

conftest 가 homeassistant/bleak 를 MagicMock 으로 치환해 실제로 돌릴 수 없어
(test_poll_deadline.py 와 같은 이유) AST 로 검사한다.
"""
import ast
from pathlib import Path

_COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "omron"


def _call_order(fn: ast.AST, names: set[str]) -> list[str]:
    """이름이 호출되는 순서 (소스 위치 기준)."""
    found: list[tuple[int, int, str]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
        if name in names:
            found.append((node.lineno, node.col_offset, name))
    return [name for _l, _c, name in sorted(found)]


def _find_function(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} 을(를) 찾지 못했다 — 이름을 바꿨다면 이 테스트도 갱신할 것")


def test_the_bond_comes_before_the_service_changed_subscribe():
    tree = ast.parse((_COMPONENT / "omron_ble" / "setup.py").read_text(encoding="utf-8"))
    fn = _find_function(tree, "async_pair_and_sync_device")

    order = _call_order(fn, {"pair", "subscribe_service_changed"})
    assert order[:2] == ["pair", "subscribe_service_changed"], (
        f"호출 순서가 {order} 다. Service Changed 는 본드된 클라이언트별로 "
        "보존되므로 pair() 뒤에 와야 한다."
    )


def test_the_subscribe_still_precedes_the_vendor_traffic():
    """앱도 벤더 CCCD 보다 먼저 쓴다 — 본딩 뒤, 벤더 앞."""
    tree = ast.parse((_COMPONENT / "omron_ble" / "setup.py").read_text(encoding="utf-8"))
    fn = _find_function(tree, "async_pair_and_sync_device")

    order = _call_order(fn, {"subscribe_service_changed", "async_sync_device_time"})
    assert order == ["subscribe_service_changed", "async_sync_device_time"], (
        f"호출 순서가 {order} 다. 시간 동기(첫 벤더 트래픽) 보다 앞이어야 한다."
    )
