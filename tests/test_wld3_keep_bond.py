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


def test_connect_time_bonding_is_fenced_to_a_local_adapter_and_the_pairing_session():
    """연결 시점 본딩은 돌아왔지만, #142 가 막으려던 것은 그대로 막아야 한다.

    ESP-IDF 의 ``btc_dm_ble_auth_cmpl_evt`` 는 SMP_CONN_TOUT(102) 을 default
    분기로 흘려 ``btc_dm_remove_ble_bonding_keys()`` 를 부른다. 프록시에서
    완료되지 않은 pair 요청 하나가 저장된 본드를 영구히 날린다. 그리고 2.8.3 이
    이 플래그를 달고도 ESP 에서 실패했으니, 거기서는 얻는 것도 없었다.

    반대로 재접속 본드가 유지된 유일한 실행(2.7.8-beta.15, 로컬 BlueZ)은 연결
    시점에, 디스커버리보다 먼저 본딩했다.

    그래서 세 가지 울타리를 친다: 로컬 어댑터일 때만, 본드를 만드는 세션에서만,
    그리고 실패하면 평범한 연결로 물러난다.
    """
    import inspect

    from custom_components.omron.omron_ble.omron_driver import (
        establish_connection_with_bond_settle,
    )

    source = inspect.getsource(establish_connection_with_bond_settle)
    assert "_bluez_device_path" in source, (
        "로컬 어댑터 여부를 확인하지 않는다 — 프록시에서 pair 요청이 나가면 "
        "본드를 잃는다"
    )
    assert "pair=True" in source, "연결 시점 본딩 경로가 없다"
    assert source.count("establish_connection(BleakClient") >= 2, (
        "연결 시점 본딩이 실패했을 때 물러날 경로가 없다"
    )
    assert "_bluez_pairing_agent" in source, (
        "agent 없이 Device1.Pair() 를 부르면 BlueZ 5.72+ 는 Just Works 확인을 "
        "응답하지 않고 AuthenticationFailed 로 떨어진다 — 그러면 폴백이 타서 "
        "디스커버리 뒤 pair() 와 구분되지 않는다"
    )
    assert "TimeoutError" in source, (
        "bleak_retry_connector 가 소진 후 던지는 TimeoutError 에 폴백이 없다"
    )
    assert "pair_this_attempt = False" in source, (
        "거절이 매 시도마다 반복된다 — 한 번 거절되면 남은 시도는 평범한 연결이어야 한다"
    )


def test_a_second_bonding_is_skipped_only_when_the_connect_actually_bonded():
    """플래그가 아니라 결과로 판단해야 한다 — 폴백이 탄 뒤에도 본드는 만들어져야."""
    import inspect

    from custom_components.omron.omron_ble.omron_driver import OmronDeviceSession

    source = inspect.getsource(OmronDeviceSession.pair)
    assert "_omron_bonded_at_connect" in source, (
        "connect 가 실제로 본딩했는지 보지 않는다 — 같은 링크에서 두 번 본딩하면 "
        "방금 만든 키를 돌린다"
    )
    assert "pair_on_connect" not in source, (
        "프로파일 플래그로 건너뛰면 폴백이 탄 뒤에도 건너뛰어 본드가 없이 끝난다"
    )


def test_only_the_pairing_session_bonds_at_connect():
    """재접속이 pair 요청을 보내면 안 된다 — 그게 프록시에서 본드를 날린 경로다."""
    import inspect

    from custom_components.omron.omron_ble.omron_driver import OmronDeviceSession

    source = inspect.getsource(OmronDeviceSession.connect)
    assert "self._pairing_session and self._config.pair_on_connect" in source, (
        "페어링 세션 여부로 걸러내지 않는다"
    )


def test_only_os_bonding_profiles_bond_at_connect():
    """커스텀 키 프로파일은 OS 본드를 만들지 않는다."""
    assert get_device_config("HEM-7386T1").pair_on_connect is True
    assert get_device_config("HEM-7155T").pair_on_connect is False


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


def test_the_bonding_result_is_scoped_to_the_connection_it_describes():
    """settle 중 끊기고 재시도하면, 앞 시도의 결과가 남아 있으면 안 된다.

    1차에서 pair=True 가 성공하고 settle 중 링크가 끊긴 뒤 2차가 폴백하면,
    이번 연결은 본딩하지 않았는데 pair() 가 건너뛴다 — 본드 없이 끝난다.
    connect_settle_attempts 가 1 인 프로파일은 해당 없지만, 3 인 OS 본딩
    프로파일에서는 실재한다.
    """
    import ast
    import inspect

    from custom_components.omron.omron_ble.omron_driver import (
        establish_connection_with_bond_settle,
    )

    tree = ast.parse(inspect.getsource(establish_connection_with_bond_settle).lstrip())
    fn = tree.body[0]
    loop = next(node for node in ast.walk(fn) if isinstance(node, ast.For))

    def _assigned(scope) -> set[str]:
        names = set()
        for node in ast.walk(scope):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
        return names

    stashed = "bonded_this_client"
    assert stashed in _assigned(loop), (
        f"{stashed} 가 루프 안에서 초기화되지 않는다 — 앞 시도의 결과가 "
        "다음 연결에 딸려간다"
    )
    before_loop = ast.Module(
        body=[node for node in fn.body if node is not loop], type_ignores=[]
    )
    assert stashed not in _assigned(before_loop), (
        f"{stashed} 를 루프 밖에서도 대입한다 — 시도별 결과가 아니게 된다"
    )
