"""GATT Model Number String 을 카탈로그 id 로 잇는 별칭 (이슈 #91).

커프는 자기 카톤에 적힌 이름을 Model Number String 으로 답한다. 그 이름이
프로파일 키도, 이미 등록된 변형도 아닌 경우가 많다 — BP5465 는
HEM-7382T1-AZAZ 이고, 스스로를 HEM-7140T1 이라 부르는 기기는 HEM-7142T2 프로파일로 읽는다.
매핑이 없으면 아무것도 안 걸려 기본 프로파일로 폴백하고, 그러면 EEPROM 을 엉뚱한
레이아웃으로 읽는다.

표는 OMRON connect 안드로이드 앱이 들고 있는 기기 목록에서 뽑았다.
"""

import re

from custom_components.omron.omron_ble.device_catalog import (
    CANONICAL_DEVICE_PROFILES,
)
from custom_components.omron.omron_ble.devices import (
    MODEL_VARIANT_MAP,
    ambiguous_model_candidates,
    get_device_config,
    get_supported_model_stats,
    get_supported_models,
    infer_model_id_from_local_name,
    resolve_profile_model_id,
)
from custom_components.omron.omron_ble.model_aliases import (
    AMBIGUOUS_MODEL_NAMES,
    MODEL_NUMBER_ALIASES,
)


_MODEL_CODE_PREFIX = re.compile(r"(HEM|HCR|HN|HBF)-", re.IGNORECASE)


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


def test_aliases_are_names_not_model_codes():
    """별칭은 BP7000·Evolv 같은 제품명을 모델 코드로 바꾸는 표다.

    HEM-/HCR- 같은 모델 코드가 키로 오면 그건 별칭이 아니라 프로파일의
    equivalent_model_ids 다 — 거기 넣어야 드롭다운에도 나온다.
    """
    codes = [a for a in MODEL_NUMBER_ALIASES if _MODEL_CODE_PREFIX.match(a)]
    assert not codes, f"move to equivalent_model_ids: {codes}"


def test_moved_model_codes_stay_in_their_profiles():
    expected = {
        "HEM-6161T-D/E": None,
        "HEM-6232T-D/E": None,
        "HEM-7140T1": "HEM-7142T2",
        "HEM-7141T1": "HEM-7142T2",
        "HEM-7142T1": "HEM-7142T2",
        "HEM-7143T1": "HEM-7146T",
        "HEM-7143T1-EBK": "HEM-7146T",
        "HEM-7143T2": "HEM-7146T",
        "HEM-7143T2-ESL": "HEM-7146T",
        "HEM-7144T1": "HEM-7146T",
        "HEM-7155T-AP": "HEM-7155T",
        "HEM-7156T": "HEM-7150T",
        "HEM-7156T-A": "HEM-7150T",
        "HEM-7156T-AAP": "HEM-7150T",
        "HEM-7280T": "HEM-7322T",
        "HEM-9601T-E3": "HEM-9601T",
    }
    for code, profile in expected.items():
        if profile is None:
            assert code not in MODEL_VARIANT_MAP, code
        else:
            assert MODEL_VARIANT_MAP[code] == profile, code


def test_a_model_code_is_never_both_variant_and_alias():
    assert not set(MODEL_NUMBER_ALIASES) & set(MODEL_VARIANT_MAP)


def test_aliases_do_not_grow_the_dropdown():
    """설정 화면은 HEM 표기로 유지한다 — 별칭은 해석용이다."""
    added = set(MODEL_NUMBER_ALIASES) & set(get_supported_models())

    # BP5465 는 원래 카탈로그 변형으로 등록돼 있어 예외다.
    assert added <= {"BP5465"}, added


def test_dropdown_counts_match_the_stats_shown_in_the_flow():
    stats = get_supported_model_stats()
    assert stats["total"] == len(get_supported_models())
    assert stats["profiles"] + stats["extra_variants"] == stats["total"]


def test_hcr_names_are_selectable_variants():
    """박스에 HCR-* 만 적힌 기기: 드롭다운에서 고를 수 있고 프로파일로 풀린다."""
    expected = {
        "HCR-1901T2": "HEM-1026T2",
        "HCR-1902T2": "HEM-1026T2",
        "HCR-7206T2": "HEM-7146T",
        "HCR-7308T2": "HEM-7146T",
        "HCR-7501T": "HEM-7157T",
        "HCR-7502T": "HEM-7342T",
        "HCR-750AT": "HEM-7342T",
        "HCR-7601T": "HEM-7342T",
        "HCR-7602T": "HEM-7342T",
        "HCR-7608T2": "HEM-7600T",
        "HCR-7612T2": "HEM-7342T",
        "HCR-761AT2": "HEM-7342T",
        "HCR-7711T2": "HEM-7342T",
        "HCR-7712T2": "HEM-7342T",
        "HCR-7800T": "HEM-7530T",
    }
    offered = set(get_supported_models())
    for name, profile in expected.items():
        assert name in offered, name
        assert resolve_profile_model_id(name) == profile, name
        assert get_device_config(name).display_model == profile, name
        # A cuff reporting the carton name is stored under it, not under a
        # HEM variant, so entity ids follow the name the owner sees.
        assert infer_model_id_from_local_name(name) == name, name


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

    # Three stacks, not two: the modern-fe4a firmware is a third X4 Smart and
    # the only one that reads records on that firmware. Leaving it out of the
    # candidates sent its owners to a profile that either cannot connect or
    # connects and transfers nothing (#67). It goes first because it is the one
    # with reports behind it.
    assert ambiguous_model_candidates("X4 Smart") == (
        "HEM-7155T_ESL1",
        "HEM-7155T_ESL",
        "HEM-7155T_K4-ESL",
    )
    assert ambiguous_model_candidates("HEM-7155T_ESL")[0] == "HEM-7155T_ESL1"


def test_the_x4_smart_candidates_are_three_different_profiles() -> None:
    """세 후보가 같은 프로파일로 모이면 고를 이유가 없다."""
    from custom_components.omron.omron_ble.devices import (
        get_device_config,
        resolve_profile_model_id,
    )

    profiles = {
        candidate: resolve_profile_model_id(candidate)
        for candidate in ambiguous_model_candidates("X4 Smart")
    }
    assert len(set(profiles.values())) == 3, profiles
    modern = get_device_config("HEM-7155T_ESL1")
    # 이 펌웨어는 -P- 창 밖에서 메모리를 읽으려면 토큰 핸드셰이크가 필요하다.
    # 나머지 두 후보에는 그것이 없어서 연결은 되고 데이터는 오지 않는다.
    assert modern.unlock_mode.value == "token_key"
    assert modern.settings_read_address == 0x0260


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


def test_the_china_market_u705t_reads_through_the_hem_7142t2_map():
    """U705T reports its retail code; its memory matches the HEM-7142T2 layout."""
    assert resolve_profile_model_id("U705T") == "HEM-7142T2"

    config = get_device_config("U705T")
    assert config.user_start_addresses == [0x02E8]
    assert config.per_user_records_count == [14]
    assert config.record_byte_size == 0x0E
    assert config.settings_read_address == 0x0260
