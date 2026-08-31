"""마지막 성공 판독 타임스탬프의 의미를 구조 수준에서 고정한다 (#91).

폴은 실패해도 예외를 던지지 않고 캐시를 돌려주도록 설계돼 있다 — 혈압계는
측정 사이에 대부분 꺼져 있고, 그때마다 엔티티를 unavailable 로 만들 수는 없기
때문이다. 대가로, 아무것도 못 읽은 실행과 조용한 실행이 밖에서 보면 똑같아진다.
이슈 #91 에서 제보자가 본 것이 정확히 그것이다: 메모리 세션 시도 3회가 전부
실패했는데 `Finished fetching ... (success: True)` 가 찍혔다.

Last Readout 센서는 그 구분을 위해 존재한다. 따라서 스탬프는 **레코드를 실제로
디코딩한 자리에서만** 찍혀야 한다. 폴 종료 지점이나 예외 핸들러로 옮기는 순간
센서는 "성공했다"는 거짓말을 하게 되고, 없느니만 못해진다.

conftest 가 homeassistant/bleak 를 MagicMock 으로 치환해 실제 인스턴스를 만들 수
없어(test_poll_deadline.py 와 같은 이유) AST 로 검사한다.
"""
import ast
from pathlib import Path

_COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "omron"

_ATTR = "_last_readout_at"


def _parse(relative_path: str) -> ast.Module:
    return ast.parse((_COMPONENT / relative_path).read_text(encoding="utf-8"))


def _assignments_to_attr(tree: ast.AST) -> list[ast.stmt]:
    """__init__ 의 타입 주석 대입(AnnAssign)도 함께 센다."""
    found: list[ast.stmt] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Attribute) and target.attr == _ATTR:
                found.append(node)
    return found


def _find_function(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} 을(를) 찾지 못했다 — 이름을 바꿨다면 이 테스트도 갱신할 것")


class TestLastReadoutStamp:
    def test_the_stamp_lives_in_the_readout_path(self):
        """초기화 한 번, 그리고 판독 경로 한 번. 그 외의 자리는 허용하지 않는다."""
        parser = _parse("omron_ble/parser.py")
        readout = _find_function(parser, "_poll_device_readout")

        in_readout = _assignments_to_attr(readout)
        assert len(in_readout) == 1, (
            f"_poll_device_readout 안의 {_ATTR} 대입이 {len(in_readout)}개다. "
            "레코드를 디코딩한 자리 하나여야 한다."
        )

        everywhere = _assignments_to_attr(parser)
        assert len(everywhere) == 2, (
            f"parser.py 전체의 {_ATTR} 대입이 {len(everywhere)}개다. "
            "__init__ 의 None 초기화와 판독 경로, 둘뿐이어야 한다."
        )

    def test_the_stamp_is_not_set_from_a_failure_handler(self):
        """예외 핸들러에서 찍으면 실패한 폴이 성공으로 보인다."""
        async_poll = _find_function(_parse("omron_ble/parser.py"), "async_poll")
        for node in ast.walk(async_poll):
            if isinstance(node, ast.ExceptHandler):
                assert not _assignments_to_attr(node), (
                    f"async_poll 의 예외 핸들러가 {_ATTR} 을 찍는다 — "
                    "그러면 센서가 실패를 성공으로 보고한다."
                )

    def test_the_poll_publishes_it_to_its_own_coordinator(self):
        """센서는 readout_coordinator 를 통해서만 값을 받는다."""
        source = (_COMPONENT / "__init__.py").read_text(encoding="utf-8")
        assert "readout_coordinator" in source
        assert "last_readout_at" in source, (
            "_async_poll_data 가 파서의 last_readout_at 을 코디네이터로 넘겨야 한다"
        )
