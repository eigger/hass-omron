"""Regression tests for Bluetooth parser sentinel and length handling."""

from custom_components.omron.omron_ble.advertisement import _decode_omron_msd_fields
from custom_components.omron.omron_ble.bls import (
    _decode_sfloat_le,
    _parse_bp_measurement,
)


def _sfloat(value: int) -> bytes:
    return value.to_bytes(2, "little")


def test_bls_sfloat_special_values_are_not_measurements():
    for raw in (0x07FE, 0x07FF, 0x0800, 0x0801, 0x0802):
        assert _decode_sfloat_le(_sfloat(raw)) is None


def test_bls_missing_pressure_or_pulse_does_not_become_a_number():
    # Flags indicate pulse is present; pressure is valid and pulse is NaN.
    payload = bytes([0x04]) + _sfloat(120) + _sfloat(80) + _sfloat(90) + _sfloat(0x07FF)
    parsed = _parse_bp_measurement(payload)
    assert parsed is not None
    assert parsed["sys"] == 120
    assert parsed["dia"] == 80
    assert parsed["bpm"] is None

    # A pressure sentinel means the notification carries no usable reading.
    invalid_pressure = (
        bytes([0x00])
        + _sfloat(0x07FF)
        + _sfloat(80)
        + _sfloat(90)
    )
    assert _parse_bp_measurement(invalid_pressure) is None


def test_legacy_single_user_advertisement_accepts_five_byte_payload():
    parsed = _decode_omron_msd_fields(bytes([0x01, 0x01, 0x12, 0x34, 0x56]))
    assert parsed is not None
    assert parsed["user_register_count"] == 1


def test_legacy_advertisement_rejects_truncated_user_sequence():
    assert _decode_omron_msd_fields(bytes([0x01, 0x01, 0x12, 0x34])) is None
