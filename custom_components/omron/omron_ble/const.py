"""Constants for the Omron BLE module."""
from __future__ import annotations

from sensor_state_data import BaseDeviceClass

CTS_CHARACTERISTIC_UUID = "00002a2b-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
FIRMWARE_REVISION_UUID = "00002a26-0000-1000-8000-00805f9b34fb"
HARDWARE_REVISION_UUID = "00002a27-0000-1000-8000-00805f9b34fb"
MANUFACTURER_NAME_UUID = "00002a29-0000-1000-8000-00805f9b34fb"
MODEL_NUMBER_UUID = "00002a24-0000-1000-8000-00805f9b34fb"
LOCAL_TIME_INFO_UUID = "00002a0f-0000-1000-8000-00805f9b34fb"

class ExtendedSensorDeviceClass(BaseDeviceClass):
    """Device class for additional sensors (compared to sensor-state-data)."""

    # Blood Pressure (Systolic & Diastolic)
    BLOOD_PRESSURE_SYSTOLIC = "blood_pressure_systolic"
    BLOOD_PRESSURE_DIASTOLIC = "blood_pressure_diastolic"

    # Heart Rate
    HEART_RATE = "heart_rate"

    # Derived blood pressure health metrics
    PULSE_PRESSURE = "pulse_pressure"
    MEAN_ARTERIAL_PRESSURE_ESTIMATED = "mean_arterial_pressure_estimated"
    SHOCK_INDEX = "shock_index"
    RATE_PRESSURE_PRODUCT = "rate_pressure_product"
    BLOOD_PRESSURE_CATEGORY = "blood_pressure_category"


class ExtendedBinarySensorDeviceClass(BaseDeviceClass):
    """Device class for additional binary sensors."""
    BODY_MOVEMENT = "body_movement"
    CUFF_FIT = "cuff_fit"
    IRREGULAR_PULSE = "irregular_pulse"
    IMPROPER_POSITION = "improper_position"
