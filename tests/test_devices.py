"""devices.py / device_catalog.py 단위 테스트."""
from custom_components.omron.omron_ble.const import DEFAULT_DEVICE_MODEL
from custom_components.omron.omron_ble.devices import (
    ConnectType,
    Endianness,
    TimeSyncLayout,
    get_device_config,
    get_supported_models,
    infer_model_id_from_local_name,
    resolve_profile_model_id,
)


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
        assert cfg.time_sync_layout == TimeSyncLayout.AT_8
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


class TestNoDeadConfigSurface:
    """설정처럼 보이지만 아무도 안 읽는 값이 다시 생기지 않게 한다."""

    def test_removed_fields_are_gone(self):
        """카탈로그 40곳이 채우던 값을 읽는 코드가 없었다.

        ``settings_unread_records_bytes`` 는 ``supports_unread_counter`` 만
        읽었고, 그 속성을 읽는 곳은 없었다. 미읽음 카운터 기능이 만들어지다
        만 흔적이다.
        """
        cfg = get_device_config("HEM-7386T1")
        for name in (
            "settings_unread_records_bytes",
            "supports_unread_counter",
            "unlock_uuid",
        ):
            assert not hasattr(cfg, name), name

    def test_the_unlock_characteristic_is_one_constant(self):
        """40개 프로필이 전부 같은 값을 쓰던 필드였다 — 상수가 맞다."""
        from custom_components.omron.omron_ble.const import UNLOCK_CHARACTERISTIC_UUID

        assert UNLOCK_CHARACTERISTIC_UUID == "b305b680-aee7-11e1-a730-0002a5d5c51b"

    def test_every_field_is_read_somewhere(self):
        """dataclass 필드는 코드나 테스트 어딘가에서 읽혀야 한다.

        테스트까지 세는 이유는 ``connect_type`` 같은 분류용 필드 때문이다.
        런타임 분기가 읽지 않아도(프로토콜 계열을 나누는 메타데이터다) 테스트가
        단언한다면 조용히 죽은 것이 아니다. 정말 죽은 필드는 카탈로그가 채우기만
        하고 어느 쪽에서도 안 읽는다.
        """
        import dataclasses
        import pathlib

        from custom_components.omron.omron_ble.devices import DeviceConfig

        sources = (
            *pathlib.Path("custom_components/omron").rglob("*.py"),
            *pathlib.Path("tests").rglob("*.py"),
        )
        read_by = "".join(
            p.read_text(encoding="utf-8")
            for p in sources
            if p.name not in ("device_catalog.py", "test_devices.py")
        )
        unread = [
            f.name
            for f in dataclasses.fields(DeviceConfig)
            if f".{f.name}" not in read_by and f'"{f.name}"' not in read_by
        ]
        assert not unread, f"카탈로그만 채우고 아무도 안 읽는 필드: {unread}"
