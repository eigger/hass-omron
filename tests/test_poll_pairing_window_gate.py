"""세션마다 본드를 버리는 커프의 예약 폴 게이트 (#129 에서 재작성).

이 프로파일들은 세션이 끝나면 자기 쪽 본드를 버리고, 페어링 모드를 벗어나면
새 본드를 거부한다. 그래서 "주기가 됐다"는 이유만으로 폴을 쏘면 연결 · 본딩
시도 · 세션 락을 다 쓰고 시작 전부터 확정돼 있던 실패에 도달한다. 그 실패는
로그에서 고장과 구별되지 않는다.

커프가 스스로 말할 때만 읽는다 — pairing_mode(지금 본드를 만들 수 있음) 또는
forced_transfer(측정값이 대기 중). 사람이 Refresh Data 를 누른 건 시계가 아니
므로 게이트를 통과시킨다. 그러지 않으면 버튼이 아무 말 없이 아무것도 안 한다.

conftest 가 homeassistant/bleak 를 MagicMock 으로 치환해 폴을 실제로 돌릴 수
없어(test_poll_deadline.py 와 같은 이유) 배선은 AST 로 검사한다.
"""
import ast
from pathlib import Path

from custom_components.omron.omron_ble.devices import (
    BondPolicy,
    ConnectType,
    get_device_config,
)

_COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "omron"


def _parse(relative_path: str) -> ast.Module:
    return ast.parse((_COMPONENT / relative_path).read_text(encoding="utf-8"))


class TestGateDerivation:
    """게이트 대상은 본드 정책에서 파생된다 — 프로파일마다 따로 켜지 않는다."""

    def test_per_session_profiles_are_gated(self):
        config = get_device_config("HEM-7380T1")
        assert config.bond_policy is BondPolicy.PER_SESSION
        assert config.poll_requires_pairing_window is True

    def test_wld4_is_gated_on_the_same_terms_as_wld3(self):
        """WLD4.0 도 같은 본드 정책을 쓰므로 같은 게이트가 걸린다."""
        config = get_device_config("HEM-7188T1")
        assert config.connect_type is ConnectType.WLD4_0
        assert config.bond_policy is BondPolicy.PER_SESSION
        assert config.poll_requires_pairing_window is True

    def test_a_kept_bond_is_not_gated(self):
        """본드를 유지하는 프로파일은 커프의 허락 없이도 읽을 수 있다."""
        config = get_device_config("HEM-7142T2")
        assert config.bond_policy is BondPolicy.REUSE
        assert config.poll_requires_pairing_window is False

    def test_moving_the_bond_policy_moves_the_gate(self):
        """두 설정이 따로 놀 수 없어야 한다 — 그래서 파생으로 둔다."""
        from dataclasses import replace

        kept = replace(get_device_config("HEM-7380T1"), bond_policy=BondPolicy.REUSE)
        assert kept.poll_requires_pairing_window is False


class TestGateWiring:
    """게이트는 폴 안에 있고, 버튼은 그것을 통과할 수 있어야 한다."""

    def test_the_poll_consults_the_gate(self):
        source = (_COMPONENT / "__init__.py").read_text(encoding="utf-8")
        assert "poll_requires_pairing_window" in source, (
            "_async_poll_data 가 게이트를 보지 않는다"
        )
        assert "has_handoff_session" in source, (
            "주차된 페어링 세션은 게이트에서 면제돼야 한다 — 아니면 그 링크가 버려진다"
        )

    def test_the_refresh_button_arms_a_request(self):
        source = (_COMPONENT / "button.py").read_text(encoding="utf-8")
        assert "request_poll(" in source, (
            "Refresh Data 가 요청을 표시하지 않으면 게이트가 버튼을 삼킨다"
        )

    def test_the_request_is_consumed_before_any_early_return(self):
        """건너뛴 요청이 다음 예약 폴을 무장시키면 안 된다."""
        tree = _parse("__init__.py")
        fn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_async_poll_data"
        )
        body = ast.unparse(fn)
        take = body.index("take_poll_request")
        first_return = body.index("return ")
        assert take < first_return, (
            "take_poll_request 가 첫 return 뒤에 있다 — 어떤 경로로 빠져나가든 "
            "플래그는 소비돼야 한다"
        )
