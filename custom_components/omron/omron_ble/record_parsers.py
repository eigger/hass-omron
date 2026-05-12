"""Omron EEPROM record byte-layout parsers."""

from __future__ import annotations

import datetime
from typing import Any


def _bytearray_bits_to_int(
    bytes_array: bytes | bytearray,
    endianness: str,
    first_bit: int,
    last_bit: int,
) -> int:
    """Extract an integer from a bit range within a byte array."""
    big_int = int.from_bytes(bytes_array, endianness)
    num_valid_bits = (last_bit - first_bit) + 1
    shifted = big_int >> (len(bytes_array) * 8 - (last_bit + 1))
    bitmask = (2**num_valid_bits) - 1
    return shifted & bitmask


def parse_classic_vital_14_7322_family(
    data: bytes | bytearray, endianness: str
) -> dict[str, Any]:
    """Classic 14-byte parser for HEM-7322T family."""
    record: dict[str, Any] = {}
    record["dia"] = _bytearray_bits_to_int(data, endianness, 0, 7)
    record["sys"] = _bytearray_bits_to_int(data, endianness, 8, 15) + 25
    year = _bytearray_bits_to_int(data, endianness, 16, 23) + 2000
    record["bpm"] = _bytearray_bits_to_int(data, endianness, 24, 31)
    # JSON memory-map: 0006 ihb = byte[5] startBit=6 (LSB) → BE bit 41
    #                  0007 mov = byte[5] startBit=7 (LSB) → BE bit 40
    # NOTE: unconfirmed on hardware — corrected from previous BE bits 32/33 (byte[4]) per JSON spec.
    record["ihb"] = _bytearray_bits_to_int(data, endianness, 41, 41)
    record["mov"] = _bytearray_bits_to_int(data, endianness, 40, 40)
    month = _bytearray_bits_to_int(data, endianness, 34, 37)
    day = _bytearray_bits_to_int(data, endianness, 38, 42)
    hour = _bytearray_bits_to_int(data, endianness, 43, 47)
    minute = _bytearray_bits_to_int(data, endianness, 52, 57)
    second = min(_bytearray_bits_to_int(data, endianness, 58, 63), 59)
    # JSON memory-map (vitalDataIndex):
    #   0008 cuff:  byte[7] startBit=4 (LSB), bitSize=1 → BE bit 59
    #   000a pos:   byte[7] startBit=6 (LSB), bitSize=2 → BE bits 56-57
    #   battery is NOT defined in the device JSON spec for this family.
    record["cuff"] = _bytearray_bits_to_int(data, endianness, 59, 59)
    record["pos"] = _bytearray_bits_to_int(data, endianness, 56, 57)
    record["datetime"] = datetime.datetime(year, month, day, hour, minute, second)
    return record


def parse_classic_vital_14_6232_family(
    data: bytes | bytearray, endianness: str
) -> dict[str, Any]:
    """Classic 14-byte parser for HEM-6232T family."""
    record: dict[str, Any] = {}
    record["dia"] = _bytearray_bits_to_int(data, endianness, 0, 7)
    record["sys"] = _bytearray_bits_to_int(data, endianness, 8, 15) + 25
    year = _bytearray_bits_to_int(data, endianness, 18, 23) + 2000
    record["bpm"] = _bytearray_bits_to_int(data, endianness, 24, 31)
    # JSON memory-map: 0006 ihb = bytes[4:5] LE-16 startBit=14 → byte[5] bit 6 (LSB) → BE bit 41
    #                  0007 mov = bytes[4:5] LE-16 startBit=15 → byte[5] bit 7 (LSB) → BE bit 40
    # NOTE: unconfirmed on hardware — corrected from previous BE bits 32/33 (byte[4]) per JSON spec.
    record["ihb"] = _bytearray_bits_to_int(data, endianness, 41, 41)
    record["mov"] = _bytearray_bits_to_int(data, endianness, 40, 40)
    month = _bytearray_bits_to_int(data, endianness, 34, 37)
    day = _bytearray_bits_to_int(data, endianness, 38, 42)
    hour = _bytearray_bits_to_int(data, endianness, 43, 47)
    minute = _bytearray_bits_to_int(data, endianness, 52, 57)
    second = min(_bytearray_bits_to_int(data, endianness, 58, 63), 59)
    # JSON memory-map (vitalDataIndex):
    #   0008 cuff:  byte[7] startBit=4 (LSB), bitSize=1 → BE bit 59
    #   000a pos:   byte[7] startBit=6 (LSB), bitSize=2 → BE bits 56-57
    #   battery is NOT defined in the device JSON spec for this family.
    record["cuff"] = _bytearray_bits_to_int(data, endianness, 59, 59)
    record["pos"] = _bytearray_bits_to_int(data, endianness, 56, 57)
    record["datetime"] = datetime.datetime(year, month, day, hour, minute, second)
    return record


def parse_classic_vital_14(data: bytes | bytearray, endianness: str) -> dict[str, Any]:
    """Classic Omron memory-map vital record (14-byte / 0x0E slots).

    Byte layout:
      [0]   sys - 25 (>0xE1 means empty slot)
      [1]   dia
      [2]   bpm
      [3]   year - 2000 (lower 6 bits)
      [4:5] flags1 (hour, day, month, ihb, mov)
      [6:7] flags2 (second, minute)
    """
    # NOTE:
    # This format is byte/bit packed in a fixed little-endian on-wire layout.
    # Keep explicit byte slicing here; generic bit-range extraction across the full
    # buffer can change semantics and break existing devices.
    raw_sys = data[0]
    if raw_sys > 0xE1:
        raise ValueError("record slot is empty")

    record: dict[str, Any] = {}
    record["sys"] = raw_sys + 25
    record["dia"] = data[1]
    record["bpm"] = data[2]

    year = 2000 + (data[3] & 0x3F)
    flags1 = data[4] | (data[5] << 8)
    flags2 = data[6] | (data[7] << 8)

    hour = flags1 & 0x1F
    day = (flags1 >> 5) & 0x1F
    month = (flags1 >> 10) & 0x0F
    record["ihb"] = (flags1 >> 14) & 0x01
    record["mov"] = (flags1 >> 15) & 0x01
    second = min(flags2 & 0x3F, 59)
    minute = min((flags2 >> 6) & 0x3F, 59)
    # Modern stack flags2 bits: bit 12=Cuff, bit 13=Battery, bits 14-15=Position
    record["cuff"] = (flags2 >> 12) & 0x01
    record["battery"] = (flags2 >> 13) & 0x01
    record["pos"] = (flags2 >> 14) & 0x03

    # Devices may return partially initialized entries that are not 0xFF-filled.
    # Treat obviously empty placeholders as invalid slots.
    if (
        data[1] == 0
        and data[2] == 0
        and (data[3] & 0x3F) == 0
        and flags1 == 0
        and flags2 == 0
    ):
        raise ValueError("record slot is empty")

    # 714x family records appear to carry a trailing record sequence/id field.
    # Keep it for latest-record selection heuristics.
    if len(data) >= 2:
        record["_record_id"] = int.from_bytes(bytes(data[-2:]), "little")

    try:
        record["datetime"] = datetime.datetime(year, month, day, hour, minute, second)
    except ValueError:
        # Keep record usable for slot-based latest selection.
        record["datetime"] = None
    return record


def parse_classic_vital_16_6401_family(
    data: bytes | bytearray, endianness: str
) -> dict[str, Any]:
    """Classic 16-byte parser for HEM-6401 family."""
    if len(data) < 16:
        raise ValueError("record too short")

    year_off = int(data[0])
    month = int(data[1])
    day = int(data[2])
    hour = int(data[3])
    minute = int(data[4])
    second = min(int(data[5]), 59)

    raw_sys = int(data[6])
    dia = int(data[7])
    bpm = int(data[8])

    # Empty-slot guard for partially initialized records.
    if (
        year_off == 0
        and month == 0
        and day == 0
        and hour == 0
        and minute == 0
        and int(data[5]) == 0
        and raw_sys == 0
        and dia == 0
        and bpm == 0
    ):
        raise ValueError("record slot is empty")

    if raw_sys > 0xE1:
        raise ValueError("record slot is empty")

    flags = int(data[11])
    record: dict[str, Any] = {
        "sys": raw_sys + 25,
        "dia": dia,
        "bpm": bpm,
        # Map status flags to existing integration keys.
        "ihb": flags & 0x03,
        "mov": (flags >> 2) & 0x03,
        "datetime": datetime.datetime(year_off + 2000, month, day, hour, minute, second),
    }
    return record
