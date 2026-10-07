"""Body metrics derived from a scale reading (BMI and its WHO adult category).

Pure functions, no Home Assistant imports. The bands are the WHO adult
criteria; they do not apply to children, pregnancy or very muscular adults.
"""

from __future__ import annotations

BMI_CATEGORIES: tuple[str, ...] = (
    "underweight",
    "normal",
    "overweight",
    "obesity_class_1",
    "obesity_class_2",
    "obesity_class_3",
)

HEIGHT_MIN_CM = 100.0
HEIGHT_MAX_CM = 220.0
WEIGHT_MIN_KG = 2.0
WEIGHT_MAX_KG = 300.0
BMI_MIN = 10.0
BMI_MAX = 80.0

# Upper bound (exclusive) of each band but the last.
_WHO_UPPER_BOUNDS: tuple[float, ...] = (18.5, 25.0, 30.0, 35.0, 40.0)


def compute_bmi(weight_kg: float | None, height_cm: float | None) -> float | None:
    """BMI rounded to one decimal, or None when an input or the result is implausible."""
    if weight_kg is None or height_cm is None:
        return None
    try:
        weight = float(weight_kg)
        height = float(height_cm)
    except (TypeError, ValueError):
        return None
    if weight <= 0 or height <= 0:
        return None
    if not WEIGHT_MIN_KG <= weight <= WEIGHT_MAX_KG:
        return None
    if not HEIGHT_MIN_CM <= height <= HEIGHT_MAX_CM:
        return None
    bmi = round(weight / (height / 100) ** 2, 1)
    if not BMI_MIN <= bmi <= BMI_MAX:
        return None
    return bmi


def classify_bmi(bmi: float | None) -> str | None:
    """WHO adult category for a BMI; classify the rounded value ``compute_bmi`` returns."""
    if bmi is None:
        return None
    for category, upper in zip(BMI_CATEGORIES, _WHO_UPPER_BOUNDS):
        if bmi < upper:
            return category
    return BMI_CATEGORIES[-1]
