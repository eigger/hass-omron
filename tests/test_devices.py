"""devices.py / device_catalog.py 단위 테스트."""
from custom_components.omron.omron_ble.devices import (
    ConnectType,
    DeviceConfig,
    HostPairingMode,
    UnlockMode,
    get_device_config,
    resolve_profile_model_id,
)


class TestUnpairAfterSession:
    """unpair_after_session 은 os_bond_once=True 인 기기에서는 항상 False여야 한다.

    두 플래그가 같이 켜져 있으면 (HEM-7380T1/HEM-7382T1) 세션이
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


class TestPairOnConnect:
    """WLD3.0 + OS 본딩 기기만 connect 단계에서 본딩해야 한다."""

    def test_wld3_os_bonding_pairs_on_connect(self):
        cfg = DeviceConfig(
            model="test",
            connect_type=ConnectType.WLD3_0,
            host_pairing_mode=HostPairingMode.OS_BONDING,
            unlock_mode=UnlockMode.TOKEN_KEY,
        )
        assert cfg.pair_on_connect is True

    def test_bond_once_still_pairs_on_connect(self):
        # os_bond_once 는 "광고 트리거마다 재페어링하지 말라"는 뜻이지
        # connect 시 암호화 확립까지 막으라는 뜻이 아니다. 본드가 이미 있으면
        # 백엔드가 저장된 LTK 로 재암호화하므로 본드가 churn 되지 않는다.
        cfg = DeviceConfig(
            model="test",
            connect_type=ConnectType.WLD3_0,
            host_pairing_mode=HostPairingMode.OS_BONDING,
            unlock_mode=UnlockMode.TOKEN_KEY,
            os_bond_once=True,
        )
        assert cfg.pair_on_connect is True

    def test_non_wld3_does_not_pair_on_connect(self):
        cfg = DeviceConfig(
            model="test",
            connect_type=ConnectType.WLD1_0,
            host_pairing_mode=HostPairingMode.OS_BONDING,
            unlock_mode=UnlockMode.TOKEN_KEY,
        )
        assert cfg.pair_on_connect is False

    def test_custom_key_profile_does_not_pair_on_connect(self):
        # 클래식(커스텀 키) 기기는 SMP 본딩 자체를 쓰지 않는다.
        cfg = DeviceConfig(model="test", connect_type=ConnectType.WLD3_0)
        assert cfg.host_pairing_mode == HostPairingMode.CUSTOM_KEY
        assert cfg.pair_on_connect is False

    def test_secure_session_also_pairs_on_connect(self):
        # 언락 방식과 무관하게 WLD3.0 + OS 본딩이면 connect 에서 본딩한다.
        # ECDH 기기에서 SMP 본딩을 건너뛰던 예외가 있었지만, 건너뛴 빌드에서도
        # 커프가 그대로 pairing request 를 거부해(0xff26) 근거가 없어졌다.
        cfg = DeviceConfig(
            model="test",
            connect_type=ConnectType.WLD3_0,
            host_pairing_mode=HostPairingMode.OS_BONDING,
            unlock_mode=UnlockMode.SECURE_SESSION,
        )
        assert cfg.pair_on_connect is True

    def test_all_wld3_catalog_profiles_pair_on_connect(self):
        from custom_components.omron.omron_ble.device_catalog import (
            CANONICAL_DEVICE_PROFILES,
        )

        wld3 = [
            c
            for c in CANONICAL_DEVICE_PROFILES.values()
            if c.connect_type == ConnectType.WLD3_0
        ]
        assert wld3, "카탈로그에 WLD3.0 프로파일이 있어야 한다"
        assert all(c.pair_on_connect for c in wld3)


class TestCatalogResolution:
    """카탈로그 변이(equivalent_model_ids) -> 캐노니컬 프로파일 매핑."""

    def test_hem7188t1_leo_resolves_to_own_profile(self):
        # HEM-7188T1-LEO ("X2+ Connect") keeps its own profile (distinct
        # connect_type WLD3.0 vs HEM-7142T2's WLD1.0), but currently uses the
        # plaintext token-key transport operationally, not the ECDH secure
        # session that the device rejects with 0xff26 (see hass-omron#92).
        assert resolve_profile_model_id("HEM-7188T1-LEO") == "HEM-7188T1"
        cfg = get_device_config("HEM-7188T1-LEO")
        assert cfg.model == "HEM-7188T1-LEO"
        assert cfg.unlock_mode.value == "token_key"
        assert cfg.connect_type == ConnectType.WLD3_0

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
