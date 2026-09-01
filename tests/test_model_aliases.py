"""GATT Model Number String 을 카탈로그 id 로 잇는 별칭 (이슈 #91).

커프는 자기 카톤에 적힌 이름을 Model Number String 으로 답한다. 그 이름이
프로파일 키도, 이미 등록된 변형도 아닌 경우가 많다 — BP5465 는
HEM-7382T1-AZAZ 이고, 스스로를 HEM-7140T1 이라 부르는 기기는 HEM-7140T1-AP 다.
매핑이 없으면 아무것도 안 걸려 기본 프로파일로 폴백하고, 그러면 EEPROM 을 엉뚱한
레이아웃으로 읽는다.

표는 OMRON connect 안드로이드 앱이 들고 있는 기기 목록에서 뽑았다.
"""
from custom_components.omron.omron_ble.device_catalog import (
    CANONICAL_DEVICE_PROFILES,
)
from custom_components.omron.omron_ble.devices import (
    MODEL_VARIANT_MAP,
    ambiguous_model_candidates,
    get_device_config,
    get_supported_models,
    infer_model_id_from_local_name,
    resolve_profile_model_id,
)
from custom_components.omron.omron_ble.model_aliases import (
    AMBIGUOUS_MODEL_NAMES,
    MODEL_NUMBER_ALIASES,
)


def test_the_reported_cuff_shows_its_hem_designation():
    """이슈 #91 의 기기. 앱 자체 데이터가 BP5465 = HEM-7382T1-AZAZ 라고 한다."""
    config = get_device_config("BP5465")

    assert config.display_model == "HEM-7382T1-AZAZ"
    assert config.model == "BP5465", "로그에는 기기가 답한 이름이 남아야 한다"
    assert resolve_profile_model_id("BP5465") == "HEM-7386T1"


def test_a_retail_name_we_could_not_resolve_now_lands_on_the_right_profile():
    """전에는 어느 것도 안 걸려 기본 프로파일로 갔다 — 레코드 레이아웃이 다르다."""
    assert resolve_profile_model_id("BP7360") == "HEM-7376T1"
    assert resolve_profile_model_id("BP7465") == "HEM-7386T1"
    assert resolve_profile_model_id("BP7900") == "HEM-7530T"

    config = get_device_config("BP7360")
    assert config.model == "BP7360"
    assert config.display_model == "HEM-7376T1-Z"


def test_a_short_hem_form_also_resolves():
    """소매 코드만의 문제가 아니다 — 짧은 HEM 표기도 카탈로그에 없을 수 있다."""
    assert resolve_profile_model_id("HEM-7140T1") == "HEM-7142T2"
    # 이미 HEM 표기이므로 표시는 건드리지 않는다.
    assert get_device_config("HEM-7140T1").display_model == "HEM-7140T1"


def test_the_ambiguous_names_are_left_out():
    """같은 소매명이 두 하드웨어 리비전에 걸치면 고를 수 없다.

    BP5350 / BP7350 / BP7350CAN 은 HEM-7155T 와 HEM-7155T-K4 를 함께 가리킨다.
    레코드 포맷이 다르므로 하나를 찍으면 틀린 쪽을 읽는다.
    """
    for name in ("BP5350", "BP7350", "BP7350CAN"):
        assert name not in MODEL_NUMBER_ALIASES, name


def test_every_alias_points_at_something_the_catalog_knows():
    """표가 낡으면 조용히 기본 프로파일로 흘러간다 — 대상은 실재해야 한다."""
    known = set(CANONICAL_DEVICE_PROFILES) | set(MODEL_VARIANT_MAP)
    dangling = {a: t for a, t in MODEL_NUMBER_ALIASES.items() if t not in known}
    assert not dangling, dangling


def test_aliases_do_not_grow_the_dropdown():
    """설정 화면은 HEM 표기로 유지한다 — 별칭은 해석용이다."""
    added = set(MODEL_NUMBER_ALIASES) & set(get_supported_models())

    # BP5465 는 원래 카탈로그 변형으로 등록돼 있어 예외다.
    assert added <= {"BP5465"}, added


def test_marketing_names_resolve() -> None:
    # Some firmware answers the Model Number String with the carton's marketing
    # name rather than a code: a HEM-7188T1-LEO reports "X2+ Connect" (#92).
    assert infer_model_id_from_local_name("X2+ Connect") == "HEM-7188T1-LEO"
    assert infer_model_id_from_local_name("X2+") == "HEM-7188T1-LEO"
    assert infer_model_id_from_local_name("Evolv\u2122") == "HEM-7600T-Z"
    assert infer_model_id_from_local_name("3 Series Upper Arm") == "HEM-7142T2-Z"


def test_names_covering_several_stacks_are_not_guessed() -> None:
    # The app ships HEM-7155T_ESL and HEM-7155T_K4-ESL with identical name,
    # Bluetooth settings name and display name, separated only by a group id
    # the device never transmits. WLS3.0 with custom-key pairing vs WLD2.0 with
    # OS bonding -- guessing reads the device through the wrong stack.
    for name in ("X4 Smart", "HEM-7155T_ESL", "M4 Intelli IT", "BP5350"):
        assert infer_model_id_from_local_name(name) is None, name
        assert len(ambiguous_model_candidates(name)) > 1, name

    assert ambiguous_model_candidates("X4 Smart") == (
        "HEM-7155T_ESL",
        "HEM-7155T_K4-ESL",
    )


def test_a_resolvable_name_is_not_called_ambiguous() -> None:
    for name in ("HEM-7386T1", "BP5465", "X2+ Connect", "BLESmart_0000123"):
        assert ambiguous_model_candidates(name) == (), name


def test_every_candidate_is_selectable_in_the_dropdown() -> None:
    # Naming a model the user cannot then pick would be worse than saying
    # nothing.
    offered = set(get_supported_models())
    for name, candidates in AMBIGUOUS_MODEL_NAMES.items():
        for candidate in candidates:
            assert candidate in offered, f"{name} -> {candidate}"


def test_no_name_is_both_resolvable_and_ambiguous() -> None:
    for name in AMBIGUOUS_MODEL_NAMES:
        assert name not in MODEL_NUMBER_ALIASES, name
