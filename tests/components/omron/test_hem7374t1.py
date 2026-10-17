"""HEM-7374T1 discovery and vendor-backed profile resolution."""

import pytest

from custom_components.omron.omron_ble.devices import (
    ConnectType,
    HostPairingMode,
    RecordParser,
    UnlockMode,
    get_device_config,
    get_supported_models,
    infer_model_id_from_local_name,
    resolve_profile_model_id,
)
from custom_components.omron.omron_ble.settings_mirror import SettingsMirrorLayout


@pytest.mark.parametrize(
    "model",
    ("HEM-7374T1", "HEM-7374T1-A2SHBK", "HEM-7374T1-A2SHDS"),
)
def test_model_names_resolve_without_falling_back_to_default(model):
    """The GATT name and both selectable variants must use the two-user map."""
    assert model in get_supported_models()
    assert infer_model_id_from_local_name(model) == model
    assert resolve_profile_model_id(model) == "HEM-7380T1"
    config = get_device_config(model)
    assert config.model == model
    assert config.connect_type == ConnectType.WLD3_0
    assert config.host_pairing_mode == HostPairingMode.OS_BONDING
    assert config.unlock_mode == UnlockMode.TOKEN_KEY
    assert config.record_parser == RecordParser.CLASSIC_VITAL_14
    assert config.user_start_addresses == [0x01C4, 0x0804]
    assert config.per_user_records_count == [100, 100]
    assert config.record_byte_size == 16
    assert config.user_start_addresses[0] + 100 * 16 == config.user_start_addresses[1]


def test_registration_writes_match_the_vendor_settings_regions():
    """A wrong family can read plausible data yet write to the wrong mirror."""
    config = get_device_config("HEM-7374T1")
    layout = SettingsMirrorLayout(config)
    registration = config.pairing_registration
    assert registration is not None
    assert layout.head_read_address == 0x0010
    assert layout.head_write_address == 0x0054
    assert layout.head_write_size == 24
    assert registration.slot_offset == 24
    assert registration.slot_size == 10
    assert layout.clock_write_address == 0x0080
    assert layout.clock_write_size == 16
    # SettingWriteIndex puts the year at +52; AT_8's block begins 8 bytes earlier.
    assert layout.clock_read_address + 8 == layout.head_read_address + 52
    assert layout.clock_write_address + layout.clock_write_size < min(
        config.user_start_addresses
    )
    assert config.keep_notify_subscriptions is True
