"""Metrics derived from one blood-pressure reading.

Pure functions, no Home Assistant imports. The device's own sensors
(``parser.py``) and the sensors of a registered user (``sensor.py``) both call
these, so the two cannot disagree. Each function returns ``None`` when the
reading cannot give the metric (a missing value, or a guard failing).
"""

from __future__ import annotations

from typing import Any

# Enum values of the blood-pressure category, mildest first.
BLOOD_PRESSURE_CATEGORIES = (
    "normal",
    "elevated",
    "hypertension_stage_1",
    "hypertension_stage_2",
    "hypertensive_crisis",
)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float))


def _has_pressure_pair(sys_val: Any, dia_val: Any) -> bool:
    """Both pressures are numbers and systolic is above diastolic."""
    return _is_number(sys_val) and _is_number(dia_val) and sys_val > dia_val


def pulse_pressure(sys_val: Any, dia_val: Any) -> float | None:
    """Systolic minus diastolic, mmHg."""
    if not _has_pressure_pair(sys_val, dia_val):
        return None
    return round(float(sys_val - dia_val), 1)


def mean_arterial_pressure_estimated(sys_val: Any, dia_val: Any) -> float | None:
    """Diastolic plus a third of the pulse pressure, mmHg."""
    if not _has_pressure_pair(sys_val, dia_val):
        return None
    return round(float(dia_val + (float(sys_val - dia_val) / 3)), 1)


def classify_blood_pressure_category(sys_val: float, dia_val: float) -> str:
    """Category by the ACC/AHA 2017 thresholds."""
    if sys_val > 180 or dia_val > 120:
        return "hypertensive_crisis"
    if sys_val >= 140 or dia_val >= 90:
        return "hypertension_stage_2"
    if sys_val >= 130 or dia_val >= 80:
        return "hypertension_stage_1"
    if sys_val >= 120 and dia_val < 80:
        return "elevated"
    return "normal"


def blood_pressure_category(sys_val: Any, dia_val: Any) -> str | None:
    """ACC/AHA category of a reading, under the same guard as the pressures."""
    if not _has_pressure_pair(sys_val, dia_val):
        return None
    return classify_blood_pressure_category(float(sys_val), float(dia_val))


def shock_index(sys_val: Any, bpm_val: Any) -> float | None:
    """Pulse over systolic pressure; needs a positive systolic."""
    if not (_is_number(sys_val) and sys_val > 0 and _is_number(bpm_val)):
        return None
    return round(float(bpm_val) / float(sys_val), 2)


def rate_pressure_product(sys_val: Any, bpm_val: Any) -> float | None:
    """Systolic times pulse, mmHg*bpm."""
    if not (_is_number(sys_val) and _is_number(bpm_val)):
        return None
    return round(float(sys_val) * float(bpm_val), 1)
