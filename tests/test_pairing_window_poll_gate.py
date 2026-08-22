"""예약 폴을 커프의 신호에 묶는 게이트 회귀 테스트.

실기기 결과(이슈 #92, HEM-7188T1-LEO on 2.7.8-beta.1): 본드를 유지해도 다음
연결은 다시 본딩을 요구받고 거부당한다. 즉 읽기에는 본드가 필요하고 본드에는
페어링 모드가 필요하므로, 스캔 간격이 지났다는 이유만으로 도는 폴은 시작 전에
이미 실패가 확정돼 있다 — connect 와 세션 락을 10초쯤 태우고 경고를 남긴 뒤.

그래서 커프가 스스로 말할 때만 폴한다:
  * ``pairing_mode``   — 지금이면 본드를 만들 수 있다
  * ``forced_transfer`` — 측정값이 대기 중이다

게이트가 REUSE 프로필까지 번지면 멀쩡한 기기가 버튼을 눌러야만 읽히게 되므로,
그 경계도 함께 고정한다.
"""
import pytest

from custom_components.omron.omron_ble.device_catalog import (
    CANONICAL_DEVICE_PROFILES,
)


def _should_poll(
    config, *, pairing_mode, forced_transfer, handoff_parked, user_requested=False
):
    """__init__.py `_async_poll_data` 게이트 조건의 거울.

    조건식을 테스트에 복제하는 것은 보통 피하지만, 여기서는 HA 런타임 전체를
    띄우지 않고 경계를 고정하기 위한 의도적 선택이다. 실제 게이트가 바뀌면
    아래 test_gate_condition_matches_the_source 가 어긋난 것을 잡는다.
    """
    if user_requested:
        return True
    if not config.poll_requires_pairing_window:
        return True
    if handoff_parked:
        return True
    return pairing_mode or forced_transfer


PER_SESSION_PROFILE = CANONICAL_DEVICE_PROFILES["HEM-7188T1"]
REUSE_PROFILE = CANONICAL_DEVICE_PROFILES["HEM-7142T2"]


@pytest.mark.parametrize(
    "pairing_mode,forced_transfer,expected",
    [
        (False, False, False),  # 타이머만으로 도는 폴 — 실패가 확정된 경우
        (True, False, True),    # 페어링 모드: 본드를 만들 수 있다
        (False, True, True),    # 측정값 대기: 가져올 것이 있다
        (True, True, True),
    ],
)
def test_per_session_profile_polls_only_when_the_cuff_signals(
    pairing_mode, forced_transfer, expected
):
    assert (
        _should_poll(
            PER_SESSION_PROFILE,
            pairing_mode=pairing_mode,
            forced_transfer=forced_transfer,
            handoff_parked=False,
        )
        is expected
    )


def test_parked_pairing_session_is_never_skipped():
    """주차된 링크는 이미 열려 있고 본딩돼 있다 — 건너뛰면 그대로 방치된다.

    페어링 직후 폴이 정확히 이 경로다. 게이트가 여기서 걸리면 페어링은 됐는데
    첫 측정값이 안 들어오는, 이 이슈의 원래 증상으로 되돌아간다.
    """
    assert _should_poll(
        PER_SESSION_PROFILE,
        pairing_mode=False,
        forced_transfer=False,
        handoff_parked=True,
    )


@pytest.mark.parametrize(
    "pairing_mode,forced_transfer", [(False, False), (True, False), (False, True)]
)
def test_reuse_profile_keeps_polling_on_the_timer(pairing_mode, forced_transfer):
    """본드를 유지하는 기기는 종전대로 스캔 간격에 읽는다."""
    assert _should_poll(
        REUSE_PROFILE,
        pairing_mode=pairing_mode,
        forced_transfer=forced_transfer,
        handoff_parked=False,
    )


def test_gate_condition_matches_the_source():
    """위 거울 함수가 실제 게이트와 같은 신호를 보는지 확인한다.

    조건식을 복제한 대가로, 원본이 바뀌었는데 테스트만 옛 규칙을 지키고 있는
    상황을 막는다.
    """
    import pathlib

    source = pathlib.Path("custom_components/omron/__init__.py").read_text()
    start = source.index("poll_requires_pairing_window")
    gate = source[start - 200 : start + 400]

    assert "not user_requested" in gate
    assert "not device_data.pairing_mode" in gate
    assert "not device_data.forced_transfer" in gate
    assert "not has_handoff_session(hass, address)" in gate


def test_user_pressing_refresh_is_never_skipped():
    """사람이 누른 요청은 시계가 아니다 — 게이트를 통과해야 한다.

    조용히 건너뛰면 버튼이 아무 일도 안 하는 것처럼 보인다. 실패하더라도
    사용자는 실제 에러를 봐야 한다.
    """
    assert _should_poll(
        PER_SESSION_PROFILE,
        pairing_mode=False,
        forced_transfer=False,
        handoff_parked=False,
        user_requested=True,
    )


def test_refresh_button_arms_the_flag_and_the_poll_consumes_it():
    """플래그는 일회성이어야 한다.

    소비되지 않고 남으면, 다음 예약 폴이 아무도 요청하지 않았는데 게이트를
    통과한다 — 이 변경이 없애려던 바로 그 폴이다.
    """
    import pathlib

    button = pathlib.Path("custom_components/omron/button.py").read_text()
    init = pathlib.Path("custom_components/omron/__init__.py").read_text()

    assert '["user_requested_poll"] = True' in button
    # pop 이어야 한다: get 이면 플래그가 남아 다음 폴까지 열어 준다.
    assert 'entry_data.pop("user_requested_poll", False)' in init
    assert 'entry_data.get("user_requested_poll"' not in init
