"""Constants for the Omron BLE module."""
from sensor_state_data import BaseDeviceClass

TIMEOUT_1DAY = 86400
TIMEOUT_5MIN = 5 * 60


class ExtendedSensorDeviceClass(BaseDeviceClass):
    """Device class for additional sensors (compared to sensor-state-data)."""

    # Channel
    CHANNEL = "channel"

    # Raw hex data
    RAW = "raw"

    # Text
    TEXT = "text"

    # Volume storage
    VOLUME_STORAGE = "volume_storage"

    # Direction
    DIRECTION = "direction"

    # Precipitation
    PRECIPITATION = "precipitation"

    # Movement detected during measurement
    MOVEMENT = "movement"

    # Irregular heartbeat detected
    IRREGULAR_HEARTBEAT = "irregular_heartbeat"

    # Blood Pressure (Systolic & Diastolic)
    BLOOD_PRESSURE_SYSTOLIC = "blood_pressure_systolic"
    BLOOD_PRESSURE_DIASTOLIC = "blood_pressure_diastolic"

    # Heart Rate
    HEART_RATE = "heart_rate"

