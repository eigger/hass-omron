"""HEM-7386T1 본드 유지 실험 회귀 테스트.

이슈 #91 의 BP5465(HEM-7382T1-AZAZ, 이 프로필 아래) 폰 HCI 캡처 보고에 따르면
공식 앱은 평상시 동기화에서 **페어링을 전혀 하지 않습니다**: 커프가 연결 후
~53ms 에 SMP Security Request 를 올리고, 폰은 이미 들고 있는 본드로 LE Start
Encryption 을 시작하며, 데이터는 우리가 쓰는 것과 같은 경로 — 핸들 0x001E
쓰기 / 0x0020 알림 — 로 흐릅니다.

PER_SESSION 은 매 세션이 끝날 때 바로 그 크레덴셜을 지웁니다.

WLD4.0 실험의 재탕이 아닙니다. 그쪽은 HEM-7188T1 을 옮겼고 음성이었지만,
7188T1 은 앱 레이어 secure session 을 쓰고 이 프로필은 안 씁니다. 그리고 이
프로필은 REUSE + ``pair_on_connect`` 조합을 한 번도 써 본 적이 없습니다 —
2.6.0 전까지 ``os_bond_once`` 로 본드를 유지했지만 그 시절엔 ``pair_on_connect``
가 존재하지 않았습니다.
"""
import pytest

from custom_components.omron.omron_ble.device_catalog import (
    CANONICAL_DEVICE_PROFILES,
)
from custom_components.omron.omron_ble.devices import (
    BondPolicy,
    ConnectType,
    get_device_config,
    resolve_profile_model_id,
)


def test_reporter_device_maps_to_the_profile_under_test():
    """BP5465 는 자기 프로필이 없다 — 바꾸는 곳이 실제로 그 기기에 닿아야 한다."""
    assert resolve_profile_model_id("HEM-7382T1-AZAZ") == "HEM-7386T1"


def test_hem_7386t1_keeps_its_bond():
    config = CANONICAL_DEVICE_PROFILES["HEM-7386T1"]

    assert config.bond_policy == BondPolicy.REUSE
    assert config.unpair_after_session is False, (
        "세션 종료 시 본드를 지우면 다음 연결이 암호화를 재개할 게 없다"
    )
    # 프록시에서 재개를 일으키는 것이 이것이다: 이미 본딩된 상대에게 보내는
    # ESPHome 의 pair 요청은 재페어링이 아니라 저장된 키로 암호화를 시작한다 —
    # 캡처 속 폰의 동작과 같다.
    assert config.pair_on_connect is True


@pytest.mark.parametrize(
    "variant", ["HEM-7382T1-AZAZ", "HEM-7382T1", "HEM-7386T1-AJF3", "HEM-7381T1-AZ"]
)
def test_variants_inherit_it(variant):
    config = get_device_config(variant)
    assert config.bond_policy == BondPolicy.REUSE
    assert config.unpair_after_session is False


def test_the_rest_of_the_wld3_family_is_untouched():
    """증거는 이 프로필 하나뿐이다. 나머지를 같이 옮기면 실험이 아니라 도박이다."""
    others = [
        (name, cfg)
        for name, cfg in CANONICAL_DEVICE_PROFILES.items()
        if cfg.connect_type == ConnectType.WLD3_0 and name != "HEM-7386T1"
    ]
    assert others, "비교 대상이 없으면 이 가드는 의미가 없다"
    for name, cfg in others:
        assert cfg.bond_policy == BondPolicy.PER_SESSION, name
        assert cfg.unpair_after_session is True, name


def test_wld4_experiment_is_not_repeated_here():
    """7188T1 은 secure session 을 쓴다 — 그 음성 결과는 여기 적용되지 않는다."""
    assert (
        CANONICAL_DEVICE_PROFILES["HEM-7188T1"].bond_policy == BondPolicy.PER_SESSION
    )


def test_connect_logs_the_path_that_owns_the_bond():
    """본드를 유지하면 "어느 라디오가 그걸 갖고 있나" 가 진단의 핵심이 된다.

    habluetooth 는 연결마다 점수로 프록시를 고른다. 본딩한 세션과 재연결하는
    세션이 다른 라디오에 안착하면, 유지한 본드는 있으나 마나다.
    """
    import pathlib

    source = pathlib.Path(
        "custom_components/omron/omron_ble/omron_driver.py"
    ).read_text()

    assert "def _connected_path" in source
    assert "advertised by source=%s, connected via " in source
    assert "bonded_this_connect=%s" in source


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
