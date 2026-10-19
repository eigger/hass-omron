"""기기 이름에 소매 모델명이 새어나가지 않게 한다 (이슈 #91).

BP5465 는 카탈로그에서 유일하게 HEM- 규칙 밖에 있는 이름이고, 커프가 GATT
Model Number String 으로 답하는 값이기도 하다. 모델명으로 프로파일을 고를 수
있게 된 뒤(#137) 그 이름이 그대로 기기 등록부까지 흘러가 ``BP5465 D5BB`` 로
표시됐다.

소매명 -> HEM 변형 대응표는 없다. ``equivalent_model_ids`` 는 평평한 목록이라
BP5465 가 HEM-7382T1-AZAZ 인지 HEM-7388T1-AJF3 인지 카탈로그는 모른다. 그래서
추측하지 않고 프로파일 키를 쓴다 — 실제로 아는 사실이고, 진짜 HEM 표기다.
"""

from datetime import timedelta
import asyncio

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from tests.components.omron.bt import service_info
from custom_components.omron.const import CONF_DEVICE_MODEL, DOMAIN
from custom_components.omron.omron_ble import OmronBluetoothDeviceData
from custom_components.omron.omron_ble.const import OMRON_MANUFACTURER_ID
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


def test_every_catalog_name_displays_in_its_model_family():
    """Catalog model names retain their HEM, HN, or HBF family prefix."""
    off_convention = []
    for name in sorted(set(CANONICAL_DEVICE_PROFILES) | set(MODEL_VARIANT_MAP)):
        shown = get_device_config(name).display_model
        if not shown.upper().startswith(("HEM-", "HN-", "HBF-")):
            off_convention.append((name, shown))
    assert not off_convention, off_convention


def test_the_device_name_path_reads_display_model():
    """표시 경로가 실제로 display_model 을 쓰는지 광고 업데이트 출력으로 검증한다."""
    data = OmronBluetoothDeviceData(device_model="BP5465")
    update = data.update(
        service_info(
            "C1:8D:32:97:D5:BB",
            name="BP5465",
            manufacturer_data={OMRON_MANUFACTURER_ID: bytes([0x01, 0, 0, 0])},
        )
    )

    device_info = update.devices[None]
    assert device_info.name == "HEM-7382T1-AZAZ D5BB"
    assert "BP5465" not in (device_info.name or "")


async def test_both_naming_paths_agree(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """설정 시점(device registry)과 광고 처리 시점(parser)이 같은 이름을 써야 한다 (#91)."""
    address = "C1:8D:32:97:D5:BB"
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=address,
        title="BP5465 D5BB",
        data={CONF_DEVICE_MODEL: "BP5465"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    async with asyncio.timeout(5):
        while entry.runtime_data.background_tasks:
            await asyncio.sleep(0.01)

    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert (CONNECTION_BLUETOOTH, address) in device.connections
    assert device.model == "HEM-7382T1-AZAZ"
    assert device.name == "HEM-7382T1-AZAZ D5BB"

    adv_update = entry.runtime_data.device_data.update(
        service_info(
            address,
            name="BP5465",
            manufacturer_data={OMRON_MANUFACTURER_ID: bytes([0x01, 0, 0, 0])},
        )
    )
    assert adv_update.devices[None].name == device.name

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
