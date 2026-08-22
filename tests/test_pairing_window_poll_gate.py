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


def test_advert_flags_are_logged_before_both_early_returns():
    """플래그 로그가 두 early return 보다 앞에 있어야 한다.

    이슈 #92 에서 진단이 막힌 지점이다. 로그가 뒤에 있으면 "Advertisement
    flags" 줄이 없다는 사실이 세 가지를 동시에 뜻하게 된다:

      * 커프가 아무 플래그도 안 올렸다
      * 올렸는데 non-connectable 광고라 connectable 체크에서 버려졌다
      * 우리가 트리거하지 않는 플래그만 올렸다

    자동 수집이 가능한지에 대해 정반대 답이 나오는 세 경우인데, 로그로는
    구분할 수 없었다. connectable 값도 같은 줄에 있어야 두 번째가 갈린다.
    """
    import pathlib

    source = pathlib.Path("custom_components/omron/__init__.py").read_text()
    body = source[source.index("def process_service_info") :]
    body = body[: body.index("\nasync def ")]

    log_at = body.index('"Advertisement flags for %s')
    connectable_return_at = body.index("if not service_info.connectable:")
    sync_needed_return_at = body.index("if not is_sync_needed:")

    assert log_at < connectable_return_at, (
        "non-connectable 광고의 플래그가 로그 없이 버려진다"
    )
    assert log_at < sync_needed_return_at
    assert "connectable=%s" in body


def test_advert_flags_log_is_transition_only():
    """광고마다 찍으면(초당 1회 수준) 정작 볼 것이 묻힌다."""
    import pathlib

    source = pathlib.Path("custom_components/omron/__init__.py").read_text()

    assert 'entry_data.get("last_advert_flags") != flags' in source
    assert 'entry_data["last_advert_flags"] = flags' in source


def test_advert_log_carries_the_raw_msd_and_format():
    """디코드된 플래그만으로는 네 경우가 구분되지 않는다.

    ``forced_transfer=False`` 는 다음을 전부 뜻할 수 있다:
      * 커프가 안 올렸다                     ← 유일한 하드웨어 한계
      * MSD 포맷 0x03 이라 비트가 없다        ← 우리가 다른 신호를 찾아야 함
      * length contract 불일치로 통째 무시    ← 포맷 지원을 넓히면 됨

    앞의 둘만 놓고 "하드웨어가 못 한다"고 결론내면 고칠 수 있는 문제를 접는
    것이므로, 포맷 바이트와 원문이 같은 줄에 있어야 한다.
    """
    import pathlib

    source = pathlib.Path("custom_components/omron/__init__.py").read_text()

    assert "msd=%s (format=%s decoded=%s)" in source
    # 플래그가 안 변해도 MSD 가 변하면 보여야 한다 — 인식 못 한 측정이 그 모습이다.
    assert "msd_hex,\n    )" in source or "        msd_hex,\n    )" in source


def test_parser_retains_the_raw_msd_and_whether_it_decoded():
    import pathlib

    source = pathlib.Path("custom_components/omron/omron_ble/parser.py").read_text()

    assert "self.last_msd = bytes(payload)" in source
    assert "self.last_msd_decoded = fields is not None" in source
    # 원문은 디코드 시도 전에 잡아야 한다: 실패한 페이로드가 정확히 필요한 것이다.
    assert source.index("self.last_msd = bytes(payload)") < source.index(
        "fields = self._decode_omron_msd_fields(payload)"
    )
