"""Blood Pressure Service measurement decode (GATT 0x2A35)."""
from __future__ import annotations

import datetime as dt
from typing import Any


def _decode_sfloat_le(raw: bytes) -> float:
    """Decode IEEE-11073 16-bit SFLOAT (little-endian)."""
    if len(raw) != 2:
        raise ValueError("SFLOAT requires 2 bytes")
    val = int.from_bytes(raw, "little", signed=False)
    mantissa = val & 0x0FFF
    exponent = (val >> 12) & 0x0F
    if mantissa >= 0x0800:
        mantissa -= 0x1000
    if exponent >= 0x0008:
        exponent -= 0x0010
    return float(mantissa) * (10.0 ** exponent)


def _parse_bp_measurement(payload: bytes) -> dict[str, Any] | None:
    """Parse BLE Blood Pressure Measurement characteristic (0x2A35)."""
    if not payload or len(payload) < 7:
        return None
    flags = payload[0]
    idx = 1
    unit_kpa = bool(flags & 0x01)
    has_timestamp = bool(flags & 0x02)
    has_pulse = bool(flags & 0x04)
    has_user_id = bool(flags & 0x08)
    has_status = bool(flags & 0x10)

    sys_val = _decode_sfloat_le(payload[idx:idx + 2])
    idx += 2
    dia_val = _decode_sfloat_le(payload[idx:idx + 2])
    idx += 2
    _ = _decode_sfloat_le(payload[idx:idx + 2])  # MAP
    idx += 2

    if unit_kpa:
        # Convert kPa to mmHg for HA entities.
        sys_mmhg = int(round(sys_val * 7.50062))
        dia_mmhg = int(round(dia_val * 7.50062))
    else:
        sys_mmhg = int(round(sys_val))
        dia_mmhg = int(round(dia_val))

    measured_dt: dt.datetime | None = None
    if has_timestamp and len(payload) >= idx + 7:
        year = int.from_bytes(payload[idx:idx + 2], "little")
        month = payload[idx + 2]
        day = payload[idx + 3]
        hour = payload[idx + 4]
        minute = payload[idx + 5]
        second = payload[idx + 6]
        idx += 7
        try:
            measured_dt = dt.datetime(year, month, day, hour, minute, second)
        except ValueError:
            measured_dt = None

    pulse: int | None = None
    if has_pulse and len(payload) >= idx + 2:
        pulse = int(round(_decode_sfloat_le(payload[idx:idx + 2])))
        idx += 2

    if has_user_id and len(payload) > idx:
        idx += 1

    status_flags = {}
    if has_status and len(payload) >= idx + 2:
        status_val = int.from_bytes(payload[idx:idx + 2], "little")
        status_flags["body_movement"] = bool(status_val & 0x01)
        status_flags["cuff_fit"] = bool(status_val & 0x02)
        status_flags["irregular_pulse"] = bool(status_val & 0x04)
        status_flags["improper_position"] = bool(status_val & 0x20)
        idx += 2

    return {
        "sys": sys_mmhg,
        "dia": dia_mmhg,
        "bpm": pulse,
        "datetime": measured_dt,
        "status_flags": status_flags,
    }
