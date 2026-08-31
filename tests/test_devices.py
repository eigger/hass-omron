"""devices.py / device_catalog.py 단위 테스트."""
from custom_components.omron.omron_ble.const import DEFAULT_DEVICE_MODEL
from custom_components.omron.omron_ble.devices import (
    BondPolicy,
    ConnectType,
    DeviceConfig,
    Endianness,
    HostPairingMode,
    TimeSyncLayout,
    UnlockMode,
    get_device_config,
    get_supported_models,
    infer_model_id_from_local_name,
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
        assert resolve_profile_model_id("HEM-7183T1-AP") == "HEM-7188T1"
        assert resolve_profile_model_id("HEM-7183T1_FLIN") == "HEM-7188T1"
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

    def test_hem7191t1_resolves_to_own_profile(self):
        assert resolve_profile_model_id("HEM-7191T1-LZ") == "HEM-7191T1"
        cfg = get_device_config("HEM-7191T1-LZ")
        assert cfg.connect_type == ConnectType.WLD4_0
        assert cfg.user_start_addresses == [0x01C4]
        assert cfg.per_user_records_count == [60]
        assert cfg.index_pointer_layout["users"][0]["slot_index_max"] == 59

    def test_hem7196t1_resolves_to_own_profile(self):
        assert resolve_profile_model_id("HEM-7196T1-FLE") == "HEM-7196T1"
        assert resolve_profile_model_id("HEM-7196T1-FLEO") == "HEM-7196T1"
        assert resolve_profile_model_id("HEM-7194T1-FLAP") == "HEM-7196T1"
        assert resolve_profile_model_id("HEM-7194T1_FLIN") == "HEM-7196T1"
        cfg = get_device_config("HEM-7196T1-FLE")
        assert cfg.connect_type == ConnectType.WLD4_0
        assert cfg.user_start_addresses == [0x01C4, 0x0584]
        assert cfg.per_user_records_count == [60, 60]
        assert cfg.index_pointer_layout["users"][0]["slot_index_max"] == 59

    def test_hem7376t1_has_own_profile_with_shifted_time_section(self):
        assert resolve_profile_model_id("HEM-7376T1-Z") == "HEM-7376T1"
        assert resolve_profile_model_id("HEM-7376T1-ACACD6") == "HEM-7376T1"
        assert resolve_profile_model_id("HEM-7385T1-AJAZ3") == "HEM-7376T1"
        cfg = get_device_config("HEM-7376T1-Z")
        assert cfg.model == "HEM-7376T1-Z"
        assert cfg.user_start_addresses == [0x080C, 0x0BCC]
        assert cfg.per_user_records_count == [60, 60]
        assert cfg.settings_write_address == 0x0058
        assert cfg.settings_time_sync_bytes == [0x30, 0x40]

    def test_hem7377t1_resolves_to_own_profile(self):
        assert resolve_profile_model_id("HEM-7377T1-ZAZ") == "HEM-7377T1"
        cfg = get_device_config("HEM-7377T1-ZAZ")
        assert cfg.user_start_addresses == [0x080C, 0x0D0C]
        assert cfg.per_user_records_count == [80, 80]
        assert cfg.settings_write_address == 0x0058
        assert cfg.index_pointer_layout["users"][0]["slot_index_max"] == 79

    def test_hem7386t1_resolves_to_own_profile(self):
        assert resolve_profile_model_id("HEM-7382T1") == "HEM-7386T1"
        assert resolve_profile_model_id("HEM-7382T1-AZAZ") == "HEM-7386T1"
        assert resolve_profile_model_id("HEM-7381T1-AZ") == "HEM-7386T1"
        assert resolve_profile_model_id("HEM-7386T1-AJF3") == "HEM-7386T1"
        cfg = get_device_config("HEM-7382T1-AZAZ")
        assert cfg.user_start_addresses == [0x080C, 0x0E4C]
        assert cfg.per_user_records_count == [100, 100]
        assert cfg.settings_write_address == 0x0058

    def test_hem6320t_and_6321t_resolution(self):
        assert resolve_profile_model_id("HEM-6320T-SH") == "HEM-6320T"
        assert resolve_profile_model_id("HEM-6322T-SH") == "HEM-6320T"
        assert resolve_profile_model_id("HEM-6323T") == "HEM-6320T"
        assert resolve_profile_model_id("HEM-6325T") == "HEM-6320T"
        assert resolve_profile_model_id("HEM-6324T") == "HEM-6321T"
        cfg = get_device_config("HEM-6324T")
        assert cfg.user_start_addresses == [0x0370, 0x08E8]
        assert cfg.per_user_records_count == [100, 100]

    def test_hem1026t2_resolves_to_own_profile(self):
        assert resolve_profile_model_id("HEM-1026T2-AJC") == "HEM-1026T2"
        assert resolve_profile_model_id("HEM-1026T2-AJE") == "HEM-1026T2"
        assert resolve_profile_model_id("HEM-1026T2-AKA") == "HEM-1026T2"
        cfg = get_device_config("HEM-1026T2-AJC")
        assert cfg.user_start_addresses == [0x02E8, 0x0928]
        assert cfg.per_user_records_count == [100, 100]
        assert cfg.record_byte_size == 0x10
        assert cfg.connect_type == ConnectType.WLD2_0
        assert cfg.endianness == Endianness.LITTLE
        assert cfg.time_sync_layout == TimeSyncLayout.MODERN_OFFSET8
        assert cfg.index_pointer_layout["endianness"] == "little"

    def test_hem7511t_and_8732_resolution(self):
        assert resolve_profile_model_id("HEM-7511T") == "HEM-7511T"
        cfg = get_device_config("HEM-7511T")
        assert cfg.user_start_addresses == [0x02AC, 0x0798]
        assert cfg.per_user_records_count == [90, 90]

        assert resolve_profile_model_id("HEM-8732T-SH") == "HEM-8732T"
        cfg = get_device_config("HEM-8732T-SH")
        assert cfg.user_start_addresses == [0x02AC, 0x05F4]
        assert cfg.per_user_records_count == [60, 60]

        assert resolve_profile_model_id("HEM-8732K-SH") == "HEM-8732K"
        cfg = get_device_config("HEM-8732K-SH")
        assert cfg.user_start_addresses == [0x02AC, 0x0522]
        assert cfg.per_user_records_count == [45, 45]

    def test_hem7325t_and_6231t_slot_counts(self):
        assert resolve_profile_model_id("HEM-7325T") == "HEM-7325T"
        cfg = get_device_config("HEM-7325T")
        assert cfg.per_user_records_count == [90]
        assert cfg.index_pointer_layout["users"][0]["slot_index_max"] == 89

        assert resolve_profile_model_id("HEM-6231T-SH") == "HEM-6231T"
        cfg = get_device_config("HEM-6231T-SH")
        assert cfg.per_user_records_count == [100]
        assert cfg.index_pointer_layout["users"][0]["slot_index_max"] == 99

        assert resolve_profile_model_id("HEM-6231T_Z") == "HEM-6231T_Z"
        cfg_z = get_device_config("HEM-6231T_Z")
        assert cfg_z.per_user_records_count == [90]
        assert cfg_z.index_pointer_layout["users"][0]["slot_index_max"] == 89

    def test_hem715x_and_716x_slot_counts(self):
        assert resolve_profile_model_id("HEM-7153JT_ASH") == "HEM-7153JT"
        cfg = get_device_config("HEM-7153JT_ASH")
        assert cfg.per_user_records_count == [30]
        assert cfg.index_pointer_layout["users"][0]["slot_index_max"] == 29

        assert resolve_profile_model_id("HEM-7157T-AP") == "HEM-7157T"
        assert resolve_profile_model_id("HEM-7158T-JC") == "HEM-7157T"
        assert resolve_profile_model_id("HEM-7158T_AP3") == "HEM-7157T"
        cfg = get_device_config("HEM-7157T-AP")
        assert cfg.per_user_records_count == [100]
        assert cfg.index_pointer_layout["users"][0]["slot_index_max"] == 99

        assert resolve_profile_model_id("HEM-716BT2-ZAZ") == "HEM-716BT2"
        assert resolve_profile_model_id("HEM-716CT2-Z") == "HEM-716BT2"
        cfg = get_device_config("HEM-716BT2-ZAZ")
        assert cfg.per_user_records_count == [30]
        assert cfg.index_pointer_layout["users"][0]["slot_index_max"] == 29

    def test_hem6401t_and_hem6410t_profiles(self):
        cfg_6401 = get_device_config("HEM-6401T-Z")
        assert cfg_6401.index_pointer_layout["users"][0]["write_cursor_mask"] == 0x3FFF
        assert cfg_6401.user_start_addresses == [0x1350]
        assert cfg_6401.settings_write_address == 0x0160

        assert resolve_profile_model_id("HEM-6410T-Z") == "HEM-6410T"
        assert resolve_profile_model_id("HEM-6410T-Z_BP") == "HEM-6410T"
        assert resolve_profile_model_id("HEM-6410T-Z_BP+EV") == "HEM-6410T"
        assert resolve_profile_model_id("HEM-6411T-MAE") == "HEM-6410T"

        cfg_6410 = get_device_config("HEM-6410T-Z")
        assert cfg_6410.user_start_addresses == [0x5590]
        assert cfg_6410.record_byte_size == 0x20
        assert cfg_6410.settings_read_address == 0x0100
        assert cfg_6410.settings_write_address == 0x0170
        assert cfg_6410.index_pointer_layout["users"][0]["write_cursor_mask"] == 0x3FFF
        assert cfg_6410.index_pointer_layout["users"][0]["write_cursor_offset"] == 0x06
        assert cfg_6410.index_pointer_layout["users"][0]["unread_counter_offset"] == 0x0E

    def test_additional_equivalent_models_resolved(self):
        assert resolve_profile_model_id("HEM-7280T-D") == "HEM-7322T"
        assert resolve_profile_model_id("HEM-6161T-D") == "HEM-6161T"
        assert resolve_profile_model_id("HEM-7149T2-E") == "HEM-716BT2"

    def test_all_readme_supported_models_are_resolvable_and_in_dropdown(self):
        readme_models = [
            "HEM-6161T",
            "HEM-6232T",
            "HEM-7142T2",
            "HEM-7146T2",
            "HEM-7151T",
            "HEM-7155T",
            "HEM-716BT2",
            "HEM-7320T",
            "HEM-7322T",
            "HEM-7343T",
            "HEM-7530T",
            "HEM-7600T",
        ]
        supported = get_supported_models()
        for m in readme_models:
            assert m in supported, f"{m} from README should be in UI dropdown"
            profile = resolve_profile_model_id(m)
            assert profile != DEFAULT_DEVICE_MODEL or m == DEFAULT_DEVICE_MODEL
            cfg = get_device_config(m)
            assert cfg is not None
            assert cfg.model == m

    def test_transmission_block_size_limits(self):
        assert get_device_config("HEM-7600T").transmission_block_size == 0x2C
        assert get_device_config("HEM-6232T").transmission_block_size == 0x2C
        assert get_device_config("HEM-7142T2").transmission_block_size == 0x2C
        assert get_device_config("HEM-6320T").transmission_block_size == 0x2C
        assert get_device_config("HEM-7322T").transmission_block_size == 0x2C
        assert get_device_config("HEM-7155T-MW").transmission_block_size == 0x2C
        assert get_device_config("HEM-7188T1").transmission_block_size == 0x38
        assert get_device_config("HEM-7196T1").transmission_block_size == 0x38
        assert get_device_config("HEM-7386T1").transmission_block_size == 0x38

    def test_heartguide_profile_configuration(self):
        from custom_components.omron.omron_ble.devices import Endianness, RecordParser

        cfg_9601 = get_device_config("HEM-9601T")
        assert cfg_9601.record_byte_size == 0x18
        assert cfg_9601.user_start_addresses == [0x041A]
        assert cfg_9601.per_user_records_count == [350]
        assert cfg_9601.settings_read_address == 0x0356
        assert cfg_9601.settings_time_sync_bytes == [0x50, 0x5A]
        assert cfg_9601.endianness == Endianness.BIG
        assert cfg_9601.index_pointer_layout.get("backtrack_slots") == 5
        assert cfg_9601.record_parser == RecordParser.CLASSIC_VITAL_24_HEARTGUIDE

        cfg_9601_variant = get_device_config("HEM-9601T-J3")
        assert cfg_9601_variant.record_byte_size == 0x18
        assert cfg_9601_variant.user_start_addresses == [0x041A]

        cfg_9700 = get_device_config("HEM-9700T")
        assert cfg_9700.record_byte_size == 0x18
        assert cfg_9700.per_user_records_count == [1000]
        assert cfg_9700.settings_time_sync_bytes == [0x50, 0x5A]
        assert cfg_9700.endianness == Endianness.BIG
        assert cfg_9700.index_pointer_layout.get("backtrack_slots") == 5
        assert cfg_9700.record_parser == RecordParser.CLASSIC_VITAL_24_HEARTGUIDE

    def test_unknown_model_falls_back_to_default(self):
        assert resolve_profile_model_id("NOT-A-REAL-MODEL") == DEFAULT_DEVICE_MODEL


def test_a_retail_model_number_resolves_to_its_profile():
    """BP5465 은 HEM-* 코드를 담지 않아 프로브가 읽어도 버려졌다 (#91)."""
    assert infer_model_id_from_local_name("BP5465") == "BP5465"
    assert resolve_profile_model_id("BP5465") == "HEM-7386T1"
    assert get_device_config("BP5465").settings_read_address == 0x0010


def test_the_hem_code_path_is_unchanged():
    """정규식이 맞으면 예전과 똑같이 그 코드를 쓴다."""
    assert infer_model_id_from_local_name("Omron HEM-7382T1-AZAZ") == "HEM-7382T1-AZAZ"
    assert infer_model_id_from_local_name("HEM-7386T1") == "HEM-7386T1"


def test_an_unknown_name_still_infers_nothing():
    """모르는 문자열에 억지로 프로파일을 붙이지 않는다."""
    for value in (None, "", "   ", "BP0000", "12345", "Omron BP5465 monitor"):
        assert infer_model_id_from_local_name(value) is None
