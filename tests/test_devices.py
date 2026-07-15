"""devices.py / device_catalog.py 단위 테스트."""
from custom_components.omron.omron_ble.devices import (
    ConnectType,
    DeviceConfig,
    get_device_config,
    resolve_profile_model_id,
)


class TestUnpairAfterSession:
    """unpair_after_session 은 os_bond_once=True 인 기기에서는 항상 False여야 한다.

    두 플래그가 같이 켜져 있으면 (HEM-7380T1/HEM-7382T1/HEM-7188T1) 세션이
    끝날 때마다 os_bond_once가 재사용하려는 본드를 unpair()가 지워버려
    다음 연결이 post-connect settle에서 실패하는 회귀가 있었다.
    """

    def test_wld3_without_bond_once_unpairs(self):
        cfg = DeviceConfig(model="test", connect_type=ConnectType.WLD3_0)
        assert cfg.unpair_after_session is True

    def test_wld3_with_bond_once_never_unpairs(self):
        cfg = DeviceConfig(
            model="test", connect_type=ConnectType.WLD3_0, os_bond_once=True
        )
        assert cfg.unpair_after_session is False

    def test_non_wld3_never_unpairs(self):
        cfg = DeviceConfig(model="test", connect_type=ConnectType.UNKNOWN)
        assert cfg.unpair_after_session is False

    def test_non_wld3_with_bond_once_never_unpairs(self):
        cfg = DeviceConfig(
            model="test", connect_type=ConnectType.UNKNOWN, os_bond_once=True
        )
        assert cfg.unpair_after_session is False


class TestCatalogResolution:
    """카탈로그 변이(equivalent_model_ids) -> 캐노니컬 프로파일 매핑."""

    def test_hem7188t1_leo_resolves_to_hem7188t1_profile(self):
        assert resolve_profile_model_id("HEM-7188T1-LEO") == "HEM-7188T1"
        # get_device_config keeps the requested variant string as .model (so
        # logs/UI show "HEM-7188T1-LEO"), while every other field is copied
        # from the canonical "HEM-7188T1" profile.
        cfg = get_device_config("HEM-7188T1-LEO")
        assert cfg.model == "HEM-7188T1-LEO"
        assert cfg.per_user_records_count == [30]

    def test_hem7188t1_le_resolves_to_same_profile(self):
        assert resolve_profile_model_id("HEM-7188T1-LE") == "HEM-7188T1"

    def test_hem7155t_esl_is_classic_not_modern(self):
        # HEM-7155T_ESL (classic stack) must NOT resolve to the modern
        # HEM-7155T-MW3 profile that HEM-7155T_ESL1 uses.
        assert resolve_profile_model_id("HEM-7155T_ESL") == "HEM-7155T"
        assert resolve_profile_model_id("HEM-7155T_ESL1") == "HEM-7155T-MW3"

    def test_hem7382t1_has_own_profile_with_shifted_time_section(self):
        cfg = get_device_config("HEM-7382T1")
        assert cfg.model == "HEM-7382T1"
        assert cfg.settings_time_sync_bytes == [0x30, 0x40]

    def test_unknown_model_falls_back_to_default(self):
        from custom_components.omron.omron_ble.const import DEFAULT_DEVICE_MODEL

        assert resolve_profile_model_id("NOT-A-REAL-MODEL") == DEFAULT_DEVICE_MODEL
