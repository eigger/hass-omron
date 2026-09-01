"""기기 이름에 소매 모델명이 새어나가지 않게 한다 (이슈 #91).

BP5465 는 카탈로그에서 유일하게 HEM- 규칙 밖에 있는 이름이고, 커프가 GATT
Model Number String 으로 답하는 값이기도 하다. 모델명으로 프로파일을 고를 수
있게 된 뒤(#137) 그 이름이 그대로 기기 등록부까지 흘러가 ``BP5465 D5BB`` 로
표시됐다.

소매명 -> HEM 변형 대응표는 없다. ``equivalent_model_ids`` 는 평평한 목록이라
BP5465 가 HEM-7382T1-AZAZ 인지 HEM-7388T1-AJF3 인지 카탈로그는 모른다. 그래서
추측하지 않고 프로파일 키를 쓴다 — 실제로 아는 사실이고, 진짜 HEM 표기다.
"""
from custom_components.omron.omron_ble.device_catalog import (
    CANONICAL_DEVICE_PROFILES,
)
from custom_components.omron.omron_ble.devices import (
    MODEL_VARIANT_MAP,
    get_device_config,
)


def test_the_retail_name_is_shown_in_hem_form():
    """앱 자체 데이터가 대응을 알려주므로 프로파일 키가 아니라 정확한 변형이다."""
    config = get_device_config("BP5465")

    assert config.model == "BP5465", "로그에는 실제로 해석된 이름이 남아야 한다"
    assert config.display_model == "HEM-7382T1-AZAZ"


def test_a_name_with_no_mapping_at_all_is_left_alone():
    """모르는 이름에 프로파일명을 붙이면 식별하지 못한 기기에 이름을 지어주는 셈이다."""
    from dataclasses import replace

    unmapped = replace(get_device_config("HEM-7386T1"), model="BP-UNMAPPED")
    assert unmapped.display_model == "BP-UNMAPPED"


def test_hem_names_are_left_exactly_as_chosen():
    """사용자가 고른 변형은 프로파일 키보다 정확하다 — 뭉개면 안 된다."""
    for model in ("HEM-7382T1-AZAZ", "HEM-7386T1", "HEM-7142T2", "HEM-7380T1"):
        assert get_device_config(model).display_model == model, model


def test_an_unknown_name_is_not_renamed_to_the_fallback_profile():
    """모르는 기기에 HEM-7142T2 라는 이름을 붙이면 거짓말이 된다."""
    config = get_device_config("SOME-UNLISTED-THING")

    assert config.model == "SOME-UNLISTED-THING"
    assert config.display_model == "SOME-UNLISTED-THING"


def test_every_catalog_name_displays_as_hem():
    """카탈로그에서 고를 수 있는 이름은 전부 HEM- 로 표시돼야 한다."""
    off_convention = []
    for name in sorted(set(CANONICAL_DEVICE_PROFILES) | set(MODEL_VARIANT_MAP)):
        shown = get_device_config(name).display_model
        if not shown.upper().startswith("HEM-"):
            off_convention.append((name, shown))
    assert not off_convention, off_convention


def test_the_device_name_path_reads_display_model():
    """표시 경로가 실제로 그 값을 쓰는지 — 여기서 갈라지면 설정만 고친 셈이다.

    소스 텍스트를 보는 이유: conftest 가 ``BluetoothData`` 를 MagicMock 으로
    갈아끼우기 때문에 ``OmronBluetoothDeviceData`` 는 클래스 객체조차 만들어지지
    않는다(그 자체가 Mock 이다). 인스턴스화도, 메서드 리플렉션도 불가능하므로
    이 하네스에서 이 배선을 지킬 방법은 이것뿐이다.
    """
    import pathlib

    source = pathlib.Path(
        "custom_components/omron/omron_ble/parser.py"
    ).read_text(encoding="utf-8")
    head = source.split("def _setup_device_info(")[1]
    body = head.split("self.pending = False")[0]

    assert "self._device_config.display_model" in body
    assert "self._device_config.model" not in body, (
        "설정 필드를 그대로 읽으면 소매명이 다시 새어나간다"
    )
