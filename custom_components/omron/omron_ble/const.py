"""Constants for the Omron BLE module."""
from sensor_state_data import BaseDeviceClass

TIMEOUT_1DAY = 86400
TIMEOUT_5MIN = 5 * 60


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

