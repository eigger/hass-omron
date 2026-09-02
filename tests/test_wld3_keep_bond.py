"""WLD3.0/WLD4.0 본드 유지 회귀 테스트 (#91).

본드를 지키는 것은 세션 종료 시 CCCD 를 끄지 않는 것
(``keep_notify_subscriptions``) 하나다. 이 파일은 그 설정과, 그것을
무용지물로 만드는 주변 동작들을 고정한다.

이슈 #91 의 BP5465 폰 HCI 캐처에서 공식 앱은 평상 동기화에서
페어링을 전혀 하지 않는다 — 커프가 연결 후 ~53ms 에 SMP Security
Request 를 올리고, 폰은 이미 들고 있는 본드로 암호화를 재개한다.
"""
import pytest

from custom_components.omron.omron_ble.device_catalog import (
    CANONICAL_DEVICE_PROFILES,
)
from custom_components.omron.omron_ble.devices import (
    ConnectType,
    get_device_config,
    resolve_profile_model_id,
)


def test_reporter_device_maps_to_the_profile_under_test():
    """BP5465 는 자기 프로필이 없다 — 바꾸는 곳이 실제로 그 기기에 닿아야 한다."""
    assert resolve_profile_model_id("HEM-7382T1-AZAZ") == "HEM-7386T1"


def test_hem_7386t1_keeps_its_bond():
    config = CANONICAL_DEVICE_PROFILES["HEM-7386T1"]

    assert config.keep_notify_subscriptions is True, (
        "세션 종료 시 본드를 지우면 다음 연결이 암호화를 재개할 게 없다"
    )
    # 프록시에서 재개를 일으키는 것이 이것이다: 이미 본딩된 상대에게 보내는
    # ESPHome 의 pair 요청은 재페어링이 아니라 저장된 키로 암호화를 시작한다 —
    # 캡처 속 폰의 동작과 같다.


@pytest.mark.parametrize(
    "variant", ["HEM-7382T1-AZAZ", "HEM-7382T1", "HEM-7386T1-AJF3", "HEM-7381T1-AZ"]
)
def test_variants_inherit_it(variant):
    config = get_device_config(variant)
    assert config.keep_notify_subscriptions is True


def test_both_families_are_on_the_confirmed_bond_settings():
    """실험이 끝났으므로 계열 전체가 같은 설정을 쓴다.

    원인이 CCCD 로 확정되고(#91) PER_SESSION 이 이 계열에서 성립 불가임이
    확인된 뒤(#133 의 AuthenticationCanceled — 커프는 -P- 밖에서 새 페어링을
    거부한다) 두 프로필에만 걸려 있던 본드 설정을 WLD3.0/WLD4.0 전체로 옮겼다.
    """
    for name, config in CANONICAL_DEVICE_PROFILES.items():
        if config.connect_type not in (ConnectType.WLD3_0, ConnectType.WLD4_0):
            continue
        assert config.keep_notify_subscriptions is True, name
        assert config.keep_notify_subscriptions is True, name


def test_connect_logs_the_path_that_owns_the_bond():
    """본드를 유지하면 "어느 라디오가 그걸 갖고 있나" 가 진단의 핵심이 된다.

    habluetooth 는 연결마다 점수로 프록시를 고른다. 본딩한 세션과 재연결하는
    세션이 다른 라디오에 안착하면, 유지한 본드는 있으나 마나다.
    """
    import pathlib

    source = pathlib.Path(
        "custom_components/omron/omron_ble/omron_driver.py"
    ).read_text(encoding="utf-8")

    assert "def _connected_path" in source
    assert "advertised by source=%s, connected via " in source
    assert "connected via" in source


def test_driver_module_has_no_undefined_names():
    """소스 문자열만 보는 단언은 NameError 를 못 잡는다 — 실제로 겪었다."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "custom_components/"],
        capture_output=True,
        text=True,
    )
    if "No module named" in result.stderr:
        pytest.skip("ruff not installed")
    assert result.returncode == 0, result.stdout + result.stderr


def test_nothing_asks_to_bond_at_connect_time():
    """연결 시점 pair 요청은 사라졌다 — 커프가 Security Request 로 몰고 간다.

    보내서 얻는 게 없고 잃을 게 있었다. ESP-IDF 의 ``btc_dm_ble_auth_cmpl_evt``
    는 SMP_CONN_TOUT(102) 을 default 분기로 흘려 ``btc_dm_remove_ble_bonding_keys()``
    를 부른다. 이슈 #91 의 프록시 로그에서 페어링 직후 ``bonded=YES`` 였던 본드가
    몇 분 뒤 사라진 게 그것이고, ``pair_only_when_pairing`` 은 그 손실을 줄이려고
    있던 플래그였다. 요청 자체를 없애면 둘 다 필요 없다.

    본드를 만들어야 하는 프로필은 디스커버리 뒤 ``pair()`` 에서 만든다.
    """
    import inspect

    from custom_components.omron.omron_ble.omron_driver import (
        establish_connection_with_bond_settle,
    )

    source = inspect.getsource(establish_connection_with_bond_settle)
    assert "pair=" not in source, "연결 경로가 다시 본딩을 요청하면 안 된다"

    config = get_device_config("HEM-7386T1")
    assert not hasattr(config, "pair_on_connect")
    assert not hasattr(config, "pair_on_connect_for")
    assert not hasattr(config, "pair_only_when_pairing")
    assert not hasattr(config, "os_bond_once")


def test_one_poll_is_one_connection_attempt_on_the_profile_under_test():
    """본드가 걸려 있으면 재시도는 방어가 아니라 추가 위험이다."""
    assert get_device_config("HEM-7386T1").connect_settle_attempts == 1
    # 다른 프로필의 기존 동작은 그대로.
    assert get_device_config("HEM-7376T1").connect_settle_attempts == 3


def test_pairing_flows_are_the_ones_marked_as_pairing_sessions():
    """플래그를 안 넘기면 config flow 도 조용히 폴처럼 붙는다."""
    import pathlib

    flow = pathlib.Path("custom_components/omron/config_flow.py").read_text(
        encoding="utf-8"
    )
    parser = pathlib.Path(
        "custom_components/omron/omron_ble/parser.py"
    ).read_text(encoding="utf-8")

    assert "OmronDeviceSession(ble_device, config, pairing_session=True)" in flow
    # async_retry_pairing 도 본드를 만드는 자리다.
    assert "pairing_session=True" in parser


def test_the_model_probe_can_hand_its_link_to_pairing():
    """모델 조회 연결이 본드를 맺는 자리다 — 거기서 끊으면 키 배포가 잘린다.

    이슈 #91 의 프록시 트레이스: 커프의 Encryption Information(LTK) 은 도착했고
    EDIV/Rand 를 담은 Master Identification 은 오지 않은 채, SMP 가
    ``BOND_PENDING`` 인 상태에서 우리가 링크를 끊었다. LE legacy 는 나중에
    암호화를 재개하려면 셋이 다 필요하므로, 저장된 본드는 못 쓰는 물건이 되고
    첫 폴이 ``SMP_ENC_FAIL``(97) 로 떨어진다.
    """
    import inspect
    import pathlib

    from custom_components.omron.omron_ble.setup import (
        async_fetch_device_model_number,
    )

    sig = inspect.signature(async_fetch_device_model_number)
    assert "keep_session_open" in sig.parameters

    flow = pathlib.Path("custom_components/omron/config_flow.py").read_text(
        encoding="utf-8"
    )
    # 조회 단계는 링크를 세워두고,
    assert "keep_session_open=True" in flow
    assert "stash_probe_session(" in flow
    # 페어링 단계는 그 링크를 이어받는다.
    assert "take_probe_session(" in flow


def test_an_adopted_link_can_be_marked_as_the_pairing_session():
    """adopt() 는 __init__ 을 우회한다 — 플래그를 손으로 안 넣으면 폴처럼 붙는다."""
    import inspect

    from custom_components.omron.omron_ble.omron_driver import OmronDeviceSession

    sig = inspect.signature(OmronDeviceSession.adopt)
    assert sig.parameters["pairing_session"].default is False


def test_a_parked_probe_link_is_closed_when_the_entry_unloads():
    """아무도 이어받지 않은 링크가 언로드 뒤까지 살아 있으면 슬롯을 잡아먹는다."""
    import pathlib

    init = pathlib.Path("custom_components/omron/__init__.py").read_text(
        encoding="utf-8"
    )
    assert "discard_probe_session(hass, address)" in init


def test_the_single_attempt_throttle_did_not_spread():
    """한 번만 시도하는 제한은 캡처가 있는 두 프로필에만 남는다."""
    for model in ("HEM-7376T1", "HEM-7377T1", "HEM-7155T-MW3", "HEM-7191T1", "HEM-7196T1"):
        assert get_device_config(model).connect_settle_attempts == 3, model
    for model in ("HEM-7386T1", "HEM-7380T1"):
        assert get_device_config(model).connect_settle_attempts == 1, model


def test_the_two_profiles_under_test_stay_comparable():
    """7380T1 과 7386T1 프로필이 갈리면 두 기기의 보고를 비교할 수 없다."""
    a = get_device_config("HEM-7386T1")
    b = get_device_config("HEM-7380T1")
    for field in ("connect_settle_attempts", "keep_notify_subscriptions"):
        assert getattr(a, field) == getattr(b, field), field
