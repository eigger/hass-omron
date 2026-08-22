"""예약 폴을 커프의 신호에 묶는 게이트 회귀 테스트.

실기기 결과(이슈 #92, HEM-7188T1-LEO on 2.7.8-beta.1): 본드를 유지해도 다음
연결은 다시 본딩을 요구받고 거부당한다. 읽기에는 본드가 필요하고 본드에는
페어링 모드가 필요하므로, 스캔 간격이 지났다는 이유만으로 도는 폴은 시작 전에
실패가 확정돼 있다 — connect 와 세션 락을 태우고 경고를 남긴 뒤.

게이트가 삼키면 안 되는 경로가 세 가지 있고, 전부 여기서 고정한다:
  * 광고가 요청한 폴 (``async_request_refresh`` 는 ~10초 디바운스라, 폴이
    실행될 때쯤 커프가 비트를 내렸을 수 있다)
  * 사람이 누른 Refresh (세션 락에 막혀도 요청이 사라지면 안 된다)
  * 주차된 페어링 세션 (이미 열려 있고 본딩된 링크)

그리고 반대 방향 — 오래된 True 가 얼어붙어 실패 연결을 다시 여는 것 — 도
막는다.
"""
import pytest

from custom_components.omron.ble_session import (
    ADVERT_FLAG_FRESHNESS_SECONDS,
    POLL_REQUEST_TTL_SECONDS,
    request_poll,
    should_skip_scheduled_poll,
    peek_poll_request,
)
from custom_components.omron.omron_ble.device_catalog import (
    CANONICAL_DEVICE_PROFILES,
)
from custom_components.omron.omron_ble.devices import BondPolicy, ConnectType

GATED = CANONICAL_DEVICE_PROFILES["HEM-7188T1"]       # WLD4.0, PER_SESSION
UNGATED_REUSE = CANONICAL_DEVICE_PROFILES["HEM-7142T2"]   # 본드 유지
UNGATED_WLD3 = CANONICAL_DEVICE_PROFILES["HEM-7380T1"]    # PER_SESSION 이지만 미검증


def _skip(config, **kw):
    """게이트를 실제로 호출한다 — 조건식을 테스트에 복제하지 않는다."""
    args = dict(
        pairing_mode=False,
        forced_transfer=False,
        flags_age=1.0,
        poll_request=None,
        handoff_parked=False,
    )
    args.update(kw)
    return should_skip_scheduled_poll(config, **args)


# ── 기본 동작 ──────────────────────────────────────────────────────────────

def test_timer_only_poll_is_skipped():
    """아무 신호도 없으면 건너뛴다 — 실패가 확정된 연결이다."""
    assert _skip(GATED) is True


@pytest.mark.parametrize("flag", ["pairing_mode", "forced_transfer"])
def test_cuff_signalling_lets_the_poll_through(flag):
    assert _skip(GATED, **{flag: True}) is False


def test_reuse_profile_is_never_gated():
    """본드를 유지하는 기기는 종전대로 스캔 간격에 읽는다.

    여기 번지면 멀쩡히 동작하던 기기가 버튼을 눌러야만 읽히게 된다 — 이
    변경에서 가장 비싼 회귀다.
    """
    assert _skip(UNGATED_REUSE) is False


# ── 삼키면 안 되는 세 경로 ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    "source", ["forced-transfer advertisement", "button press"]
)
def test_a_requested_poll_survives_the_flag_going_down(source):
    """요청이 래치되지 않으면 디바운스 구간에서 통째로 사라진다.

    광고 경로는 connect 를 직접 열지 않고 ``async_request_refresh()`` 만
    부르는데 이 호출은 ~10초 디바운스다. 그 사이 커프가 비트를 내리면 폴은
    플래그가 False 인 상태로 실행되고, 래치가 없으면 게이트가 삼킨다 —
    버튼 없는 자동 수집이 정확히 이 경로다.
    """
    assert _skip(GATED, pairing_mode=False, forced_transfer=False, poll_request=source) is False


def test_parked_pairing_session_is_never_skipped():
    """주차된 링크는 이미 열려 있고 본딩돼 있다 — 건너뛰면 방치된다.

    페어링 직후 폴이 이 경로다. 여기서 걸리면 페어링은 됐는데 첫 측정값이
    안 들어오는, 이 이슈의 원래 증상으로 되돌아간다.
    """
    assert _skip(GATED, handoff_parked=True) is False


# ── 반대 방향: 얼어붙은 플래그 ─────────────────────────────────────────────

def test_stale_true_flag_does_not_reopen_doomed_connects():
    """플래그는 MSD 디코드가 성공할 때만 갱신된다.

    길이 불일치로 패킷을 버리거나 커프가 광고를 멈추면 이전 True 가 그대로
    남고, 그 True 는 게이트가 없애려던 실패 연결을 다시 연다.
    """
    fresh = _skip(GATED, forced_transfer=True, flags_age=1.0)
    stale = _skip(GATED, forced_transfer=True, flags_age=ADVERT_FLAG_FRESHNESS_SECONDS + 1)

    assert fresh is False
    assert stale is True


def test_flags_never_seen_are_not_treated_as_permission():
    assert _skip(GATED, forced_transfer=True, flags_age=None) is True


def test_a_request_overrides_even_stale_flags():
    """명시적 요청은 플래그 신선도와 무관하다."""
    assert _skip(GATED, flags_age=None, poll_request="button press") is False


# ── 요청 래치의 수명 ───────────────────────────────────────────────────────

def test_request_is_returned_until_taken():
    entry_data: dict = {}
    request_poll(entry_data, "button press", now=1000.0)

    # peek 은 소비하지 않는다 — connect 를 실제로 시도하기 전에 사라지면
    # 세션 락에 막힌 버튼 누름이 통째로 없어진다.
    assert peek_poll_request(entry_data, now=1000.0) == "button press"
    assert peek_poll_request(entry_data, now=1001.0) == "button press"


def test_request_expires_so_it_cannot_open_an_unrelated_poll_later():
    entry_data: dict = {}
    request_poll(entry_data, "forced-transfer advertisement", now=1000.0)

    assert peek_poll_request(entry_data, now=1000.0 + POLL_REQUEST_TTL_SECONDS - 1)
    assert peek_poll_request(entry_data, now=1000.0 + POLL_REQUEST_TTL_SECONDS + 1) is None
    assert "poll_request" not in entry_data


# ── 적용 범위 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("profile", ["HEM-7188T1", "HEM-7191T1", "HEM-7196T1"])
def test_wld4_is_gated(profile):
    config = CANONICAL_DEVICE_PROFILES[profile]
    assert config.connect_type == ConnectType.WLD4_0
    assert config.bond_policy == BondPolicy.PER_SESSION
    assert config.poll_requires_pairing_window is True


def test_wld3_is_not_gated_without_evidence():
    """증거는 WLD4.0 한 대뿐이다.

    비용이 비대칭이다: 타이머 폴이 실제로 동작하는 패밀리를 게이트하면 읽기가
    조용히 멈추고, 게이트를 안 하면 이미 낭비되던 connect 가 계속 낭비될 뿐이다.
    WLD3.0 실기기로 확인한 뒤에 넓힌다.
    """
    wld3 = [
        (n, c) for n, c in CANONICAL_DEVICE_PROFILES.items()
        if c.connect_type == ConnectType.WLD3_0
    ]
    assert wld3
    for name, config in wld3:
        assert config.unpair_after_session is True, name
        assert config.poll_requires_pairing_window is False, name
    assert _skip(UNGATED_WLD3) is False


# ── 소스 배선 (함수 밖에서만 확인 가능한 것) ───────────────────────────────

def test_advert_and_button_paths_latch_a_request():
    """디바운스되는 refresh 를 부르기 전에 요청이 기록돼야 한다."""
    import pathlib

    init = pathlib.Path("custom_components/omron/__init__.py").read_text()
    button = pathlib.Path("custom_components/omron/button.py").read_text()

    advert = init.index('request_poll(entry_data, "forced-transfer advertisement")')
    refresh = init.index("await coordinator.poll_coordinator.async_request_refresh()")
    assert advert < refresh, "디바운스된 refresh 뒤에 래치하면 이미 늦다"

    assert 'request_poll(self.hass.data[DOMAIN][self._entry_id], "button press")' in button


def test_request_is_consumed_only_after_committing_to_connect():
    """세션 락에 막혀 되돌아가는 경로에서 요청이 소비되면 안 된다."""
    import pathlib

    init = pathlib.Path("custom_components/omron/__init__.py").read_text()

    lock_skip = init.index('"Skipping scheduled poll: BLE session lock held for %s"')

    # 「뒤에 소비가 하나 있다」가 아니라 「앞에 소비가 하나도 없다」를 본다.
    # 앞의 것만 보면 소비를 하나 더 끼워 넣는 변경을 놓친다.
    consumes = [
        i for i in range(len(init))
        if init.startswith('entry_data.pop("poll_request"', i)
    ]
    assert consumes, "요청을 소비하는 곳이 없다"
    assert min(consumes) > lock_skip, (
        "락 때문에 되돌아가는 경로보다 먼저 요청이 소비된다 — 버튼 누름이 사라진다"
    )


def test_advert_flags_are_logged_before_both_early_returns():
    """플래그 로그가 두 early return 보다 앞에 있어야 한다(이슈 #92 진단)."""
    import pathlib

    source = pathlib.Path("custom_components/omron/__init__.py").read_text()
    body = source[source.index("def process_service_info") :]
    body = body[: body.index("\nasync def ")]

    log_at = body.index('"Advertisement flags for %s')
    assert log_at < body.index("if not service_info.connectable:")
    assert log_at < body.index("if not is_sync_needed:")
    assert "connectable=%s" in body
    assert "msd=%s (format=%s decoded=%s)" in body


def test_parser_retains_the_raw_msd_its_freshness_and_whether_it_decoded():
    import pathlib

    source = pathlib.Path("custom_components/omron/omron_ble/parser.py").read_text()

    assert "self.last_msd = bytes(payload)" in source
    assert "self.last_msd_decoded = fields is not None" in source
    assert "self.last_msd_monotonic = time.monotonic()" in source
    # 원문은 디코드 시도 전에 잡아야 한다: 실패한 페이로드가 정확히 필요한 것이다.
    assert source.index("self.last_msd = bytes(payload)") < source.index(
        "fields = self._decode_omron_msd_fields(payload)"
    )


def test_observer_listens_to_non_connectable_advertisements_too():
    """진단 관찰자는 connectable=False 로 등록돼야 한다.

    처리 코디네이터는 ``connectable=True`` 로 등록돼 있어서 HA 가 연결 가능한
    광고만 넘겨준다. 커프가 "측정 대기" 를 non-connectable 광고로 알린다면
    ``process_service_info`` 에는 **아예 도달하지 않는다** — 그 안의 connectable
    체크 때문이 아니라, 우리 코드가 돌기 전에 한 단계 위에서 걸러지기 때문이다.

    이슈 #92 는 지금 "이 하드웨어가 못 한다" 와 "우리가 그 채널을 안 듣고 있다"
    를 구분하지 못한다. 관찰자가 이걸 가른다.
    """
    import pathlib

    source = pathlib.Path("custom_components/omron/__init__.py").read_text()

    # 처리 코디네이터는 연결 가능한 광고만 받는다(연결 라우팅 때문에 유지).
    assert "connectable=True," in source
    # 관찰자는 전부 받는다.
    assert "BluetoothCallbackMatcher(address=address, connectable=False)" in source
    # 그리고 원문 MSD 와 connectable 을 같이 찍는다.
    assert "Observed advertisement from %s: connectable=%s" in source


def test_observer_never_touches_device_state():
    """진단이 동작을 바꾸면 그건 더 이상 진단이 아니다."""
    import pathlib

    source = pathlib.Path("custom_components/omron/__init__.py").read_text()
    body = source[source.index("def _register_advertisement_observer") :]
    body = body[: body.index("\ndef _merge_poll_sensor_update")]

    for forbidden in (
        "data.update(",
        "request_poll(",
        "async_request_refresh",
        "async_retry_pairing",
        "session_lock",
    ):
        assert forbidden not in body, f"관찰자가 {forbidden} 를 건드린다"


def test_forced_transfer_is_latched_before_the_lock_and_cooldown_returns():
    """락/쿨다운에 걸린 광고도 요청으로 남아야 한다.

    트리거는 세션 락과 60초 쿨다운에서 그냥 return 한다. 래치가 그 뒤에 있으면
    이런 순서로 측정값이 통째로 유실된다:

      1. 다른 BLE 세션이 락을 잡고 있다 (최대 180초)
      2. 측정 후 forced_transfer 광고가 온다
      3. 트리거가 return 하고 래치도 안 한다
      4. 세션이 끝난다
      5. 다음 예약 폴은 기본 300초 뒤 — 플래그는 이미 60초 신선도를 넘겼다
      6. 게이트가 skip 한다. 측정값이 커프에 남는다
    """
    import pathlib

    source = pathlib.Path("custom_components/omron/__init__.py").read_text()

    latch = source.index('request_poll(entry_data, "forced-transfer advertisement")')
    lock_return = source.index("if session_lock.locked():")
    cooldown_return = source.index("if now - last_attempt < POLL_COOLDOWN_SECONDS:")

    assert latch < lock_return, "락에 막힌 광고가 래치되지 않는다"
    assert latch < cooldown_return, "쿨다운에 걸린 광고가 래치되지 않는다"
    # 래치 지점은 하나여야 한다 — 여러 곳이면 어느 것이 유효한지 흐려진다.
    assert source.count("request_poll(entry_data") == 1


def test_entering_the_waiting_state_is_visible_at_info():
    """DEBUG 만으로는 기본 로그에서 센서가 그냥 멈춘 것처럼 보인다."""
    import pathlib

    source = pathlib.Path("custom_components/omron/__init__.py").read_text()

    assert 'entry_data.get("poll_gate_waiting")' in source
    assert "_LOGGER.info" in source
    # 유휴 상태가 이어지는 동안 스캔 간격마다 INFO 를 반복하지는 않는다.
    assert "_LOGGER.debug\n                    if entry_data.get" in source
