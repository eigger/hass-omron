"""Constants for the Omron BLE module."""
from __future__ import annotations

DEFAULT_DEVICE_MODEL = "HEM-7142T2"

from sensor_state_data import BaseDeviceClass

CTS_CHARACTERISTIC_UUID = "00002a2b-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
FIRMWARE_REVISION_UUID = "00002a26-0000-1000-8000-00805f9b34fb"
HARDWARE_REVISION_UUID = "00002a27-0000-1000-8000-00805f9b34fb"
MANUFACTURER_NAME_UUID = "00002a29-0000-1000-8000-00805f9b34fb"
MODEL_NUMBER_UUID = "00002a24-0000-1000-8000-00805f9b34fb"
LOCAL_TIME_INFO_UUID = "00002a0f-0000-1000-8000-00805f9b34fb"

BP_MEASUREMENT_CHAR_UUID = "00002a35-0000-1000-8000-00805f9b34fb"
BP_RACP_CHAR_UUID = "00002a52-0000-1000-8000-00805f9b34fb"

# Bluetooth SIG company identifier for Omron Healthcare (matches manifest.json bluetooth manufacturer_id)
OMRON_MANUFACTURER_ID = 526

# --- BLE UUID Constants ---
CLASSIC_STACK_PARENT_SERVICE_UUID = "ecbe3980-c9a2-11e1-b1bd-0002a5d5c51b"
MODERN_STACK_PARENT_SERVICE_UUID = "0000fe4a-0000-1000-8000-00805f9b34fb"
# Bluetooth SIG Blood Pressure Service — often the only UUID in passive scan advertisements.
STANDARD_BLOOD_PRESSURE_SERVICE_UUID = "00001810-0000-1000-8000-00805f9b34fb"

CLASSIC_STACK_RX_CHARACTERISTIC_UUIDS = [
    "49123040-aee8-11e1-a74d-0002a5d5c51b",
    "4d0bf320-aee8-11e1-a0d9-0002a5d5c51b",
    "5128ce60-aee8-11e1-b84b-0002a5d5c51b",
    "560f1420-aee8-11e1-8184-0002a5d5c51b",
]
CLASSIC_STACK_TX_CHARACTERISTIC_UUIDS = [
    "db5b55e0-aee7-11e1-965e-0002a5d5c51b",
    "e0b8a060-aee7-11e1-92f4-0002a5d5c51b",
    "0ae12b00-aee8-11e1-a192-0002a5d5c51b",
    "10e1ba60-aee8-11e1-89e5-0002a5d5c51b",
]
CLASSIC_STACK_UNLOCK_CHARACTERISTIC_UUID = "b305b680-aee7-11e1-a730-0002a5d5c51b"

DISCOVERABLE_PARENT_SERVICE_UUIDS = [
    CLASSIC_STACK_PARENT_SERVICE_UUID,
    MODERN_STACK_PARENT_SERVICE_UUID,
]

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
