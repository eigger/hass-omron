"""Restored sensor attributes must resolve the most specific user alias."""

from types import SimpleNamespace

from custom_components.omron.sensor import OmronBluetoothSensorEntity


def test_alias_suffix_overlap_resolves_to_the_longer_alias():
    sensor = object.__new__(OmronBluetoothSensorEntity)
    object.__setattr__(
        sensor,
        "_device_key",
        SimpleNamespace(key="blood_pressure_systolic_step_dad"),
    )
    object.__setattr__(
        sensor,
        "_omron_device_data",
        SimpleNamespace(_user_aliases={1: "Dad", 2: "Step Dad"}),
    )

    assert sensor._resolve_user_id_from_key() == "user_2"


def test_alias_matching_does_not_consume_part_of_the_sensor_base():
    sensor = object.__new__(OmronBluetoothSensorEntity)
    object.__setattr__(
        sensor,
        "_device_key",
        SimpleNamespace(key="blood_pressure_systolic_dad"),
    )
    object.__setattr__(
        sensor,
        "_omron_device_data",
        SimpleNamespace(_user_aliases={1: "Dad", 2: "Systolic Dad"}),
    )

    assert sensor._resolve_user_id_from_key() == "user_1"
