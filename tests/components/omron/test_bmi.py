"""BMI from weight and height, and its WHO adult category (#236)."""

from __future__ import annotations

import pytest

from custom_components.omron.omron_ble.body_metrics import (
    BMI_CATEGORIES,
    classify_bmi,
    compute_bmi,
)


def test_bmi_is_rounded_to_one_decimal() -> None:
    assert compute_bmi(70, 175) == 22.9
    assert compute_bmi(102.8, 180) == 31.7


@pytest.mark.parametrize(
    ("weight", "height"),
    [
        (None, 175),
        (70, None),
        (0, 175),
        (70, 0),
        (-70, 175),
        (70, -175),
        (1.9, 175),  # below the weight floor
        (300.1, 175),  # above the weight ceiling
        (70, 99.9),  # below the height floor
        (70, 220.1),  # above the height ceiling
        ("abc", 175),
    ],
)
def test_implausible_inputs_give_no_bmi(weight, height) -> None:
    assert compute_bmi(weight, height) is None


def test_a_result_outside_the_plausible_band_is_dropped() -> None:
    # 2 kg at 220 cm is 0.4; 300 kg at 100 cm is 300.
    assert compute_bmi(2, 220) is None
    assert compute_bmi(300, 100) is None


def test_the_input_bounds_are_inclusive() -> None:
    assert compute_bmi(30, 100) == 30.0
    assert compute_bmi(120, 220) == 24.8


@pytest.mark.parametrize(
    ("bmi", "category"),
    [
        (10.0, "underweight"),
        (18.4, "underweight"),
        (18.5, "normal"),
        (24.9, "normal"),
        (25.0, "overweight"),
        (29.9, "overweight"),
        (30.0, "obesity_class_1"),
        (34.9, "obesity_class_1"),
        (35.0, "obesity_class_2"),
        (39.9, "obesity_class_2"),
        (40.0, "obesity_class_3"),
        (80.0, "obesity_class_3"),
    ],
)
def test_who_bands(bmi, category) -> None:
    assert classify_bmi(bmi) == category


def test_classification_uses_the_rounded_bmi() -> None:
    # 76.5 kg at 175 cm is 24.979..., shown as 25.0, so it is overweight.
    bmi = compute_bmi(76.5, 175)
    assert bmi == 25.0
    assert classify_bmi(bmi) == "overweight"
    # The unrounded 24.95 itself would still be normal.
    assert classify_bmi(24.95) == "normal"


def test_no_bmi_has_no_category() -> None:
    assert classify_bmi(None) is None


def test_categories_are_the_six_who_bands() -> None:
    assert BMI_CATEGORIES == (
        "underweight",
        "normal",
        "overweight",
        "obesity_class_1",
        "obesity_class_2",
        "obesity_class_3",
    )
