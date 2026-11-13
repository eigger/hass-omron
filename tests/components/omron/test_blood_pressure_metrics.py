"""The metrics derived from one reading, and the flags a record parser fills in."""

from __future__ import annotations

import pytest

from custom_components.omron.omron_ble import blood_pressure_metrics as bp
from custom_components.omron.omron_ble.devices import (
    CANONICAL_DEVICE_PROFILES,
    MeasurementKind,
    RecordParser,
)
from custom_components.omron.omron_ble.record_parsers import recorded_pressure_flags


def test_the_metrics_of_a_normal_reading() -> None:
    assert bp.pulse_pressure(120, 80) == 40.0
    assert bp.mean_arterial_pressure_estimated(120, 80) == 93.3
    assert bp.blood_pressure_category(120, 79) == "elevated"
    assert bp.shock_index(120, 60) == 0.5
    assert bp.rate_pressure_product(120, 60) == 7200.0


@pytest.mark.parametrize("sys_val, dia_val", [(80, 80), (70, 90), (0, 0), (0, 50)])
def test_pressure_metrics_need_systolic_above_diastolic(sys_val, dia_val) -> None:
    assert bp.pulse_pressure(sys_val, dia_val) is None
    assert bp.mean_arterial_pressure_estimated(sys_val, dia_val) is None
    assert bp.blood_pressure_category(sys_val, dia_val) is None


@pytest.mark.parametrize("bad", [None, "120", [], {}])
def test_non_numbers_give_nothing(bad) -> None:
    assert bp.pulse_pressure(bad, 80) is None
    assert bp.pulse_pressure(120, bad) is None
    assert bp.blood_pressure_category(bad, bad) is None
    assert bp.shock_index(bad, 60) is None
    assert bp.shock_index(120, bad) is None
    assert bp.rate_pressure_product(bad, 60) is None
    assert bp.rate_pressure_product(120, bad) is None


def test_a_missing_pulse_only_costs_the_pulse_metrics() -> None:
    assert bp.shock_index(120, None) is None
    assert bp.rate_pressure_product(120, None) is None
    assert bp.pulse_pressure(120, 80) == 40.0


def test_shock_index_needs_a_positive_systolic() -> None:
    assert bp.shock_index(0, 60) is None
    assert bp.shock_index(-1, 60) is None
    # The product has no such guard, as on the device.
    assert bp.rate_pressure_product(0, 60) == 0.0


def test_rounding() -> None:
    assert bp.shock_index(130, 71) == 0.55
    assert bp.mean_arterial_pressure_estimated(125, 80) == 95.0
    assert bp.mean_arterial_pressure_estimated(122, 80) == 94.0
    assert bp.mean_arterial_pressure_estimated(121, 80) == 93.7


@pytest.mark.parametrize(
    "sys_val, dia_val, category",
    [
        (119, 79, "normal"),
        (120, 79, "elevated"),
        (129, 79, "elevated"),
        (130, 70, "hypertension_stage_1"),
        (110, 80, "hypertension_stage_1"),
        (140, 70, "hypertension_stage_2"),
        (110, 90, "hypertension_stage_2"),
        (181, 70, "hypertensive_crisis"),
        (150, 121, "hypertensive_crisis"),
        (180, 90, "hypertension_stage_2"),
    ],
)
def test_the_category_thresholds(sys_val, dia_val, category) -> None:
    assert bp.blood_pressure_category(sys_val, dia_val) == category
    assert category in bp.BLOOD_PRESSURE_CATEGORIES


# Records the parsers can decode, one per layout, with the flag bits all set.
def _sample(parser: RecordParser) -> bytes:
    if parser == RecordParser.CLASSIC_VITAL_16_6401_FAMILY:
        raw = bytearray(16)
        raw[0:6] = bytes([26, 6, 17, 19, 35, 30])
        raw[6:9] = bytes([100, 80, 70])
        raw[11] = 0xFF
        return bytes(raw)
    if parser == RecordParser.CLASSIC_VITAL_24_HEARTGUIDE:
        raw = bytearray(24)
        raw[0:4] = bytes([100, 80, 70, 26])
        raw[4:6] = (55859).to_bytes(2, "little")
        raw[6:8] = (6366).to_bytes(2, "little")
        return bytes(raw)
    return bytes.fromhex("5b781a4819d71b42000014006996")


def test_the_flag_table_matches_what_the_record_parsers_decode() -> None:
    by_parser: dict[RecordParser, object] = {}
    for config in CANONICAL_DEVICE_PROFILES.values():
        if config.measurement_kind == MeasurementKind.BLOOD_PRESSURE:
            by_parser.setdefault(config.record_parser, config)
    assert RecordParser.CLASSIC_VITAL_16_6401_FAMILY in by_parser
    keys_of = {"ihb": "irregular_pulse", "mov": "body_movement"}
    keys_of |= {"cuff": "cuff_loose", "pos": "improper_position"}
    for parser, config in by_parser.items():
        record = config.parse_record(_sample(parser))
        decoded = {name for key, name in keys_of.items() if key in record}
        assert set(recorded_pressure_flags(config.record_parser)) == decoded, parser


def test_the_6401_family_has_no_cuff_or_position_flag() -> None:
    assert recorded_pressure_flags(RecordParser.CLASSIC_VITAL_16_6401_FAMILY) == (
        "irregular_pulse",
        "body_movement",
    )
    assert recorded_pressure_flags("nothing") == ()
