"""devices.py / device_catalog.py 단위 테스트."""
from custom_components.omron.omron_ble.devices import (
    BondPolicy,
    ConnectType,
    DeviceConfig,
    HostPairingMode,
    UnlockMode,
    get_device_config,
    resolve_profile_model_id,
)


class TestBondPolicy:
    """세션 종료 시 본드 삭제 여부는 bond_policy 하나로 결정된다."""

    def test_per_session_drops_the_bond(self):
        cfg = DeviceConfig(
            model="test",
            host_pairing_mode=HostPairingMode.OS_BONDING,
            unlock_mode=UnlockMode.TOKEN_KEY,
            bond_policy=BondPolicy.PER_SESSION,
        )
        assert cfg.unpair_after_session is True

    def test_reuse_keeps_the_bond(self):
        cfg = DeviceConfig(
            model="test",
            host_pairing_mode=HostPairingMode.OS_BONDING,
            unlock_mode=UnlockMode.TOKEN_KEY,
            bond_policy=BondPolicy.REUSE,
        )
        assert cfg.unpair_after_session is False

    def test_reuse_is_the_default(self):
        cfg = DeviceConfig(model="test")
        assert cfg.bond_policy == BondPolicy.REUSE
        assert cfg.unpair_after_session is False

    def test_non_os_bonding_never_unpairs(self):
        # 클래식(커스텀 키) 기기에는 지울 OS 본드가 없다.
        cfg = DeviceConfig(model="test", bond_policy=BondPolicy.PER_SESSION)
        assert cfg.host_pairing_mode == HostPairingMode.CUSTOM_KEY
        assert cfg.unpair_after_session is False

    def test_per_session_always_pairs_on_connect(self):
        # 본드를 버리는 프로파일은 connect 에서 다시 본딩하는 게 파생으로 보장된다
        # (connect_type 이 WLD3.0 이 아니어도). 본드도 없고 재페어링도 없는 조합은
        # 설정 자체가 불가능해야 한다.
        cfg = DeviceConfig(
            model="test",
            connect_type=ConnectType.WLD1_0,
            host_pairing_mode=HostPairingMode.OS_BONDING,
            unlock_mode=UnlockMode.TOKEN_KEY,
            bond_policy=BondPolicy.PER_SESSION,
        )
        assert cfg.unpair_after_session is True
        assert cfg.pair_on_connect is True


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

        wld3_4 = [
            c
            for c in CANONICAL_DEVICE_PROFILES.values()
            if c.connect_type in (ConnectType.WLD3_0, ConnectType.WLD4_0)
        ]
        assert wld3_4, "카탈로그에 WLD3.0/WLD4.0 프로파일이 있어야 한다"
        assert all(c.pair_on_connect for c in wld3_4)


class TestCatalogResolution:
    """카탈로그 변이(equivalent_model_ids) -> 캐노니컬 프로파일 매핑."""

    def test_hem7188t1_leo_resolves_to_own_profile(self):
        # HEM-7188T1-LEO ("X2+ Connect") keeps its own dedicated profile
        # (distinct connect_type WLD4.0 vs HEM-7142T2's WLD1.0) with corrected
        # memory map layout.
        assert resolve_profile_model_id("HEM-7188T1-LEO") == "HEM-7188T1"
        cfg = get_device_config("HEM-7188T1-LEO")
        assert cfg.model == "HEM-7188T1-LEO"
        assert cfg.unlock_mode.value == "token_key"
        assert cfg.connect_type == ConnectType.WLD4_0
        assert cfg.user_start_addresses == [0x01C4]
        assert cfg.per_user_records_count == [30]
        assert cfg.record_byte_size == 0x10
        assert cfg.settings_read_address == 0x0010
        assert cfg.settings_write_address == 0x0054
        assert cfg.index_pointer_layout["index_region_byte_size"] == 0x18
        assert cfg.index_pointer_layout["users"][0]["slot_index_max"] == 29

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
