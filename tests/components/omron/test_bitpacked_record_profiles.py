"""HEM-6320T / HEM-6321T / HEM-7320T use the same bit-packed record as HEM-7322T."""

import datetime

import pytest

from custom_components.omron.omron_ble.devices import (
    RecordParser,
    TimeSyncLayout,
    get_device_config,
)
from custom_components.omron.omron_ble.record_parsers import parse_classic_vital_14


def _build_record(*, year_byte_extra: int = 0) -> bytes:
    """sys 128, dia 82, bpm 67, 2026-11-10 08:15:30, ihb 1, mov 0, cuff 1, pos 0."""
    sys_, dia, bpm = 128, 82, 67
    year, month, day, hour, minute, second = 2026, 11, 10, 8, 15, 30
    mov, ihb, pos, cuff = 0, 1, 0, 1

    word_date = (mov << 15) | (ihb << 14) | (month << 10) | (day << 5) | hour
    word_time = (pos << 14) | (cuff << 12) | (minute << 6) | second

    raw = bytearray(14)
    raw[0] = dia
    raw[1] = sys_ - 25
    raw[2] = (year - 2000) | year_byte_extra
    raw[3] = bpm
    raw[4:6] = word_date.to_bytes(2, "big")
    raw[6:8] = word_time.to_bytes(2, "big")
    return bytes(raw)


def _assert_decoded(record: dict) -> None:
    assert record["sys"] == 128
    assert record["dia"] == 82
    assert record["bpm"] == 67
    assert record["ihb"] == 1
    assert record["mov"] == 0
    assert record["cuff"] == 1
    assert record["pos"] == 0
    assert record["datetime"] == datetime.datetime(2026, 11, 10, 8, 15, 30)


@pytest.mark.parametrize("model", ["HEM-6320T-Z", "HEM-6321T-Z", "HEM-7320T-ZV"])
def test_profile_matches_hem_7322t(model):
    config = get_device_config(model)
    reference = get_device_config("HEM-7322T-D")

    assert config.record_parser is RecordParser.CLASSIC_VITAL_14_BITPACKED
    assert config.resolved_time_sync_layout() is TimeSyncLayout.AT_2_SWAPPED
    assert config.record_parser == reference.record_parser
    assert config.resolved_time_sync_layout() == reference.resolved_time_sync_layout()


def test_record_decodes_through_profile():
    _assert_decoded(get_device_config("HEM-7320T-ZV").parse_record(_build_record()))


def test_year_ignores_top_two_bits_of_year_byte():
    raw = _build_record(year_byte_extra=0xC0)
    assert raw[2] & 0xC0 == 0xC0

    _assert_decoded(get_device_config("HEM-7320T-ZV").parse_record(raw))


def test_byte_aligned_parser_does_not_decode_this_record():
    try:
        record = parse_classic_vital_14(_build_record(), "big")
    except ValueError:
        return  # invalid date fields are also a mismatch
    assert (record["sys"], record["dia"]) != (128, 82)
