"""Constants for the Omron BLE module."""
from __future__ import annotations

import datetime as dt

from sensor_state_data import BaseDeviceClass

TIMEOUT_1DAY = 86400
TIMEOUT_5MIN = 5 * 60
CTS_CHARACTERISTIC_UUID = "00002a2b-0000-1000-8000-00805f9b34fb"


def build_cts_payload(now: dt.datetime) -> bytearray:
    """Build Bluetooth CTS payload (10 bytes) from timezone-aware datetime."""
    payload = bytearray()
    payload += int(now.year).to_bytes(2, "little")
    payload += bytes(
        [
            now.month,
            now.day,
            now.hour,
            now.minute,
            now.second,
            now.isoweekday(),  # Monday=1 ... Sunday=7 (CTS format)
            0x00,  # Fractions256
            0x01,  # Adjust reason: manual time update
        ]
    )
    return payload


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

