"""A restored weight is the native kg value whatever unit the user displays."""


import pytest

from custom_components.omron.omron_ble.const import ExtendedSensorDeviceClass
from custom_components.omron.sensor import (
    SENSOR_DESCRIPTIONS,
    OmronBluetoothSensorEntity,
)


def _sensor(device_class, unit):
    sensor = object.__new__(OmronBluetoothSensorEntity)
    object.__setattr__(
        sensor, "entity_description", SENSOR_DESCRIPTIONS[(device_class, unit)]
    )
    return sensor


def _weight():
    return _sensor(ExtendedSensorDeviceClass.WEIGHT, "kg")


def test_pounds_are_converted_back_to_kilograms():
    value = _weight()._restored_value("226.6", {"unit_of_measurement": "lb"})
    assert value == pytest.approx(102.784, abs=0.001)


def test_the_native_unit_is_left_alone():
    assert _weight()._restored_value("102.8", {"unit_of_measurement": "kg"}) == 102.8
    assert _weight()._restored_value("102.8", {}) == 102.8


def test_a_non_numeric_state_is_not_converted():
    assert _weight()._restored_value("abc", {"unit_of_measurement": "lb"}) == "abc"


def test_an_unknown_unit_restores_nothing():
    assert _weight()._restored_value("5", {"unit_of_measurement": "bananas"}) is None


def test_a_pressure_sensor_is_untouched():
    sensor = _sensor(ExtendedSensorDeviceClass.BLOOD_PRESSURE_SYSTOLIC, "mmHg")
    assert sensor._restored_value("126", {"unit_of_measurement": "kPa"}) == 126
