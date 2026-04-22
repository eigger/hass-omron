"""Omron device definitions and record parsers."""
from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple

_LOGGER = logging.getLogger(__name__)

# --- BLE UUID Constants ---
CLASSIC_STACK_PARENT_SERVICE_UUID = "ecbe3980-c9a2-11e1-b1bd-0002a5d5c51b"
MODERN_STACK_PARENT_SERVICE_UUID = "0000fe4a-0000-1000-8000-00805f9b34fb"
# Bluetooth SIG Blood Pressure Service — often the only UUID in passive scan advertisements.
STANDARD_BLOOD_PRESSURE_SERVICE_UUID = "00001810-0000-1000-8000-00805f9b34fb"

CLASSIC_STACK_RX_CHARACTERISTIC_UUIDS = [
    "49123040-aee8-11e1-a74d-0002a5d5c51b",
    "4d0bf320-aee8-11e1-a0d9-0002a5d5c51b",
    "5128ce60-aee8-11e1-b84b-0002a5d5c51b",
    "560f1420-aee8-11e1-8184-0002a5d5c51b",
]
CLASSIC_STACK_TX_CHARACTERISTIC_UUIDS = [
    "db5b55e0-aee7-11e1-965e-0002a5d5c51b",
    "e0b8a060-aee7-11e1-92f4-0002a5d5c51b",
    "0ae12b00-aee8-11e1-a192-0002a5d5c51b",
    "10e1ba60-aee8-11e1-89e5-0002a5d5c51b",
]
CLASSIC_STACK_UNLOCK_CHARACTERISTIC_UUID = "b305b680-aee7-11e1-a730-0002a5d5c51b"

DISCOVERABLE_PARENT_SERVICE_UUIDS = [
    CLASSIC_STACK_PARENT_SERVICE_UUID,
    MODERN_STACK_PARENT_SERVICE_UUID,
]


# --- Bit-level parsing utility ---
def bytearray_bits_to_int(
    bytes_array: bytes | bytearray, endianness: str,
    first_bit: int, last_bit: int,
) -> int:
    """Extract an integer from a bit range within a byte array."""
    big_int = int.from_bytes(bytes_array, endianness)
    num_valid_bits = (last_bit - first_bit) + 1
    shifted = big_int >> (len(bytes_array) * 8 - (last_bit + 1))
    bitmask = (2 ** num_valid_bits) - 1
    return shifted & bitmask


# --- Record Parsers ---
def parse_record_format_a(data: bytes | bytearray, endianness: str) -> dict[str, Any]:
    """Parse format A: HEM-7320T, HEM-7322T, HEM-7600T, HEM-6232T style (big-endian).

    Bit layout:
      [0:7]   dia
      [8:15]  sys - 25
      [16:23] year - 2000
      [24:31] bpm
      [32]    mov
      [33]    ihb
      [34:37] month
      [38:42] day
      [43:47] hour
      [52:57] minute
      [58:63] second
    """
    record = {}
    record["dia"] = bytearray_bits_to_int(data, endianness, 0, 7)
    record["sys"] = bytearray_bits_to_int(data, endianness, 8, 15) + 25
    year = bytearray_bits_to_int(data, endianness, 16, 23) + 2000
    record["bpm"] = bytearray_bits_to_int(data, endianness, 24, 31)
    record["mov"] = bytearray_bits_to_int(data, endianness, 32, 32)
    record["ihb"] = bytearray_bits_to_int(data, endianness, 33, 33)
    month = bytearray_bits_to_int(data, endianness, 34, 37)
    day = bytearray_bits_to_int(data, endianness, 38, 42)
    hour = bytearray_bits_to_int(data, endianness, 43, 47)
    minute = bytearray_bits_to_int(data, endianness, 52, 57)
    second = min(bytearray_bits_to_int(data, endianness, 58, 63), 59)
    record["datetime"] = datetime.datetime(year, month, day, hour, minute, second)
    return record


def parse_record_format_a_alt(data: bytes | bytearray, endianness: str) -> dict[str, Any]:
    """Parse format A-alt: HEM-7530T, HEM-6232T style.

    Same as format A but year bits are [18:23] instead of [16:23],
    and ihb/mov may be swapped for HEM-6232T.
    """
    record = {}
    record["dia"] = bytearray_bits_to_int(data, endianness, 0, 7)
    record["sys"] = bytearray_bits_to_int(data, endianness, 8, 15) + 25
    year = bytearray_bits_to_int(data, endianness, 18, 23) + 2000
    record["bpm"] = bytearray_bits_to_int(data, endianness, 24, 31)
    record["mov"] = bytearray_bits_to_int(data, endianness, 32, 32)
    record["ihb"] = bytearray_bits_to_int(data, endianness, 33, 33)
    month = bytearray_bits_to_int(data, endianness, 34, 37)
    day = bytearray_bits_to_int(data, endianness, 38, 42)
    hour = bytearray_bits_to_int(data, endianness, 43, 47)
    minute = bytearray_bits_to_int(data, endianness, 52, 57)
    second = min(bytearray_bits_to_int(data, endianness, 58, 63), 59)
    record["datetime"] = datetime.datetime(year, month, day, hour, minute, second)
    return record


def parse_record_format_a_alt_6232(data: bytes | bytearray, endianness: str) -> dict[str, Any]:
    """Parse format for HEM-6232T (ihb/mov order swapped vs 7530T)."""
    record = {}
    record["dia"] = bytearray_bits_to_int(data, endianness, 0, 7)
    record["sys"] = bytearray_bits_to_int(data, endianness, 8, 15) + 25
    year = bytearray_bits_to_int(data, endianness, 18, 23) + 2000
    record["bpm"] = bytearray_bits_to_int(data, endianness, 24, 31)
    record["ihb"] = bytearray_bits_to_int(data, endianness, 32, 32)
    record["mov"] = bytearray_bits_to_int(data, endianness, 33, 33)
    month = bytearray_bits_to_int(data, endianness, 34, 37)
    day = bytearray_bits_to_int(data, endianness, 38, 42)
    hour = bytearray_bits_to_int(data, endianness, 43, 47)
    minute = bytearray_bits_to_int(data, endianness, 52, 57)
    second = min(bytearray_bits_to_int(data, endianness, 58, 63), 59)
    record["datetime"] = datetime.datetime(year, month, day, hour, minute, second)
    return record


def parse_record_format_b(data: bytes | bytearray, endianness: str) -> dict[str, Any]:
    """Parse format B: HEM-7150T, HEM-7155T, HEM-7342T, HEM-7361T (little-endian).

    Bit layout:
      [68:73]   minute
      [74:79]   second
      [80]      mov
      [81]      ihb
      [82:85]   month
      [86:90]   day
      [91:95]   hour
      [98:103]  year - 2000
      [104:111] bpm
      [112:119] dia
      [120:127] sys - 25
    """
    record = {}
    minute = bytearray_bits_to_int(data, endianness, 68, 73)
    second = min(bytearray_bits_to_int(data, endianness, 74, 79), 59)
    record["mov"] = bytearray_bits_to_int(data, endianness, 80, 80)
    record["ihb"] = bytearray_bits_to_int(data, endianness, 81, 81)
    month = bytearray_bits_to_int(data, endianness, 82, 85)
    day = bytearray_bits_to_int(data, endianness, 86, 90)
    hour = bytearray_bits_to_int(data, endianness, 91, 95)
    year = bytearray_bits_to_int(data, endianness, 98, 103) + 2000
    record["bpm"] = bytearray_bits_to_int(data, endianness, 104, 111)
    record["dia"] = bytearray_bits_to_int(data, endianness, 112, 119)
    record["sys"] = bytearray_bits_to_int(data, endianness, 120, 127) + 25
    record["datetime"] = datetime.datetime(year, month, day, hour, minute, second)
    return record


def parse_record_format_c(data: bytes | bytearray, endianness: str) -> dict[str, Any]:
    """Parse format C: HEM-7380T1 (little-endian, direct byte access).

    Byte layout:
      [0]   sys - 25 (>0xE1 means empty slot)
      [1]   dia
      [2]   bpm
      [3]   year - 2000 (lower 6 bits)
      [4:5] flags1 (hour, day, month, ihb, mov)
      [6:7] flags2 (second, minute)
    """
    raw_sys = data[0]
    if raw_sys > 0xE1:
        raise ValueError("record slot is empty")

    record = {}
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

    record["datetime"] = datetime.datetime(year, month, day, hour, minute, second)
    return record


def parse_record_format_c_7142(data: bytes | bytearray, endianness: str) -> dict[str, Any]:
    """Parse format C variant for HEM-7142T2.

    The first bytes follow the same SYS/DIA/BPM layout as other format C parsers, but
    timestamp fields can be partially inconsistent on some firmware dumps. Keep vitals and
    tolerate broken datetime fields so latest-slot selection can still work.
    """
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


# --- Device Configuration ---


class DeviceModelVariant(NamedTuple):
    """Alternate catalog model ID that shares EEPROM layout with a canonical profile."""

    model_id: str
    unverified: bool = False
    reason: str | None = None


@dataclass
class DeviceConfig:
    """Configuration for a specific Omron device model."""

    # Device identity
    model: str

    # BLE channel configuration
    parent_service_uuid: str = CLASSIC_STACK_PARENT_SERVICE_UUID
    rx_channel_uuids: list[str] = field(
        default_factory=lambda: list(CLASSIC_STACK_RX_CHARACTERISTIC_UUIDS)
    )
    tx_channel_uuids: list[str] = field(
        default_factory=lambda: list(CLASSIC_STACK_TX_CHARACTERISTIC_UUIDS)
    )
    unlock_uuid: str = CLASSIC_STACK_UNLOCK_CHARACTERISTIC_UUID
    requires_unlock: bool = True
    supports_pairing: bool = True
    supports_os_bonding_only: bool = False
    # True: faster GATT refresh / RX→unlock timing for classic custom-key pairing. False: conservative defaults.
    # HEM-7380T1 uses OS bonding only; stays False.
    legacy_pairing_workarounds: bool = False

    # EEPROM layout
    endianness: str = "big"
    user_start_addresses: list[int] = field(default_factory=list)
    per_user_records_count: list[int] = field(default_factory=list)
    record_byte_size: int = 0x0E
    transmission_block_size: int = 0x38

    # Settings addresses
    settings_read_address: int | None = None
    settings_write_address: int | None = None
    settings_unread_records_bytes: list[int] | None = None
    settings_time_sync_bytes: list[int] | None = None
    index_pointer_layout: dict[str, Any] | None = None

    # Record parser function name
    record_parser: str = "format_a"
    latest_selection_strategy: str = "datetime"
    # Behavior flags (avoid model-name checks in driver)
    enable_index_debug_logs: bool = False
    use_layout_fallback_scan: bool = False
    equivalent_model_ids: tuple[DeviceModelVariant, ...] = ()

    @property
    def num_users(self) -> int:
        """Return the number of users this device supports."""
        return len(self.user_start_addresses)

    @property
    def is_single_channel(self) -> bool:
        """Return True if the device uses a single BLE channel."""
        return len(self.tx_channel_uuids) == 1

    @property
    def supports_unread_counter(self) -> bool:
        """Return True if the device supports unread record counters."""
        return self.settings_unread_records_bytes is not None

    def parse_record(self, data: bytes | bytearray) -> dict[str, Any]:
        """Parse a single record using the device-specific parser."""
        parser_map = {
            "format_a": parse_record_format_a,
            "format_a_alt": parse_record_format_a_alt,
            "format_a_alt_6232": parse_record_format_a_alt_6232,
            "format_b": parse_record_format_b,
            "format_c": parse_record_format_c,
            "format_c_7142": parse_record_format_c_7142,
        }
        parser = parser_map.get(self.record_parser)
        if parser is None:
            raise ValueError(f"Unknown record parser: {self.record_parser}")
        return parser(data, self.endianness)

    def parent_service_stack(self) -> str:
        """Return which BLE parent-service layout this profile expects (classic vs modern)."""
        if self.parent_service_uuid == MODERN_STACK_PARENT_SERVICE_UUID:
            return "modern"
        return "classic"

    def is_service_compatible(self, service_uuids: list[str]) -> bool:
        """Check whether advertised GATT services match this profile's parent service."""
        if self.parent_service_stack() == "modern":
            return MODERN_STACK_PARENT_SERVICE_UUID in service_uuids
        return CLASSIC_STACK_PARENT_SERVICE_UUID in service_uuids

    def is_advertisement_compatible(self, service_uuids: list[str] | None) -> bool:
        """Whether scan-time service UUIDs are consistent enough to attempt pairing/poll.

        Passive advertisements often list only the standard Blood Pressure service (0x1810);
        the Omron parent service may appear only after GATT service discovery post-connection.
        """
        if not service_uuids:
            return True
        if self.is_service_compatible(service_uuids):
            return True
        advertised = {str(u).lower() for u in service_uuids}
        if STANDARD_BLOOD_PRESSURE_SERVICE_UUID.lower() in advertised:
            return True
        return False


# --- Canonical device profiles (one EEPROM layout per key; catalog variants map here) ---
CANONICAL_DEVICE_PROFILES: dict[str, DeviceConfig] = {
    "HEM-6320T": DeviceConfig(
        model="HEM-6320T",
        endianness="big",
        user_start_addresses=[0x0370],
        per_user_records_count=[100],
        record_byte_size=0x0E,
        transmission_block_size=0x38,
        settings_read_address=0x0F74,
        settings_write_address=0x0F9A,
        settings_unread_records_bytes=[0x00, 0x08],
        settings_time_sync_bytes=[0x14, 0x1E],
        index_pointer_layout={
            "index_region_byte_size": 0x08,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        record_parser="format_a",
        equivalent_model_ids=(
            DeviceModelVariant("HEM-6320T-Z", unverified=False),
        ),
    ),
    "HEM-6321T": DeviceConfig(
        model="HEM-6321T",
        endianness="big",
        user_start_addresses=[0x0370, 0x08E8],
        per_user_records_count=[100, 100],
        record_byte_size=0x0E,
        transmission_block_size=0x38,
        settings_read_address=0x0F74,
        settings_write_address=0x0F9A,
        settings_unread_records_bytes=[0x00, 0x08],
        settings_time_sync_bytes=[0x14, 0x1E],
        index_pointer_layout={
            "index_region_byte_size": 0x08,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        record_parser="format_a",
        equivalent_model_ids=(
            DeviceModelVariant("HEM-6321T-Z", unverified=False),
        ),
    ),
    "HEM-6401T": DeviceConfig(
        model="HEM-6401T",
        endianness="little",
        # HEM-6401T exposes multiple data types; only the BP data_5 area is mapped here.
        user_start_addresses=[0x1350],
        per_user_records_count=[100],
        record_byte_size=0x10,
        transmission_block_size=0x10,
        settings_read_address=0x0100,
        settings_write_address=0x0160,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=None,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x06, "unread_counter_offset": 0x0E, "write_cursor_mask": 0xFFFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": 0},
            ],
        },
        record_parser="format_b",
        equivalent_model_ids=(
            DeviceModelVariant("HEM-6401T-Z", unverified=True, reason="fallback"),
            DeviceModelVariant("HEM-6402T-Z", unverified=True),
            DeviceModelVariant("HEM-6410T-Z", unverified=True),
        ),
    ),
    "HEM-7320T": DeviceConfig(
        model="HEM-7320T",
        endianness="big",
        user_start_addresses=[0x02AC, 0x05F4],
        per_user_records_count=[60, 60],
        record_byte_size=0x0E,
        transmission_block_size=0x38,
        settings_read_address=0x0260,
        settings_write_address=0x0286,
        settings_unread_records_bytes=[0x00, 0x08],
        settings_time_sync_bytes=[0x14, 0x1E],
        index_pointer_layout={
            "index_region_byte_size": 0x08,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
            ],
        },
        record_parser="format_a",
        equivalent_model_ids=(
            DeviceModelVariant("HEM-7320T-CA", unverified=False),
            DeviceModelVariant("HEM-7320T-CACS", unverified=False),
            DeviceModelVariant("HEM-7320T-ZV", unverified=False),
            DeviceModelVariant("HEM-7320T_TI-CA", unverified=False),
            DeviceModelVariant("HEM-7320T_TI-Z", unverified=False),
            DeviceModelVariant("HEM-8725T-WM", unverified=True, reason="fallback"),
        ),
    ),
    "HEM-7322T": DeviceConfig(
        model="HEM-7322T",
        legacy_pairing_workarounds=True,
        endianness="big",
        user_start_addresses=[0x02AC, 0x0824],
        per_user_records_count=[100, 100],
        record_byte_size=0x0E,
        transmission_block_size=0x38,
        settings_read_address=0x0260,
        settings_write_address=0x0286,
        settings_unread_records_bytes=[0x00, 0x08],
        settings_time_sync_bytes=[0x14, 0x1E],
        index_pointer_layout={
            "index_region_byte_size": 0x08,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        record_parser="format_a",
        equivalent_model_ids=(
            DeviceModelVariant("HEM-7321T-CA", unverified=False),
            DeviceModelVariant("HEM-7321T_TI-CA", unverified=False),
            DeviceModelVariant("HEM-7321T_TI-Z", unverified=False),
            DeviceModelVariant("HEM-7280T-AP", unverified=False),
            DeviceModelVariant("HEM-7280T-E", unverified=False),
            DeviceModelVariant("HEM-7280T_TI-D", unverified=False),
            DeviceModelVariant("HEM-7280T_TI-E", unverified=False),
            DeviceModelVariant("HEM-7281T", unverified=False),
            DeviceModelVariant("HEM-7282T", unverified=False),
            DeviceModelVariant("HEM-7321T-ZV", unverified=False),
            DeviceModelVariant("HEM-7322T-D", unverified=False),
            DeviceModelVariant("HEM-7322T-E", unverified=False),
            DeviceModelVariant("HEM-7511T", unverified=True, reason="fallback"),
            DeviceModelVariant("HEM-8732K-SH", unverified=True, reason="fallback"),
            DeviceModelVariant("HEM-8732T-SH", unverified=True, reason="fallback"),
        ),
    ),
    "HEM-7600T": DeviceConfig(
        model="HEM-7600T",
        legacy_pairing_workarounds=True,
        endianness="big",
        user_start_addresses=[0x02AC],
        per_user_records_count=[100],
        record_byte_size=0x0E,
        transmission_block_size=0x38,
        settings_read_address=0x0260,
        settings_write_address=0x0286,
        settings_unread_records_bytes=[0x00, 0x08],
        settings_time_sync_bytes=[0x14, 0x1E],
        index_pointer_layout={
            "index_region_byte_size": 0x08,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        record_parser="format_a",
        equivalent_model_ids=(
            DeviceModelVariant("HEM-7270C", unverified=False),
            DeviceModelVariant("HEM-7271T", unverified=False),
            DeviceModelVariant("HEM-7325T", unverified=False),
            DeviceModelVariant("HEM-7600T", unverified=False),
            DeviceModelVariant("HEM-7600T-E", unverified=False),
            DeviceModelVariant("HEM-7600T-Z", unverified=False),
            DeviceModelVariant("HEM-7600T-ZCD6BK", unverified=True),
            DeviceModelVariant("HEM-7600T-SH3BK", unverified=False),
            DeviceModelVariant("HEM-7600T2-JF", unverified=False),
            DeviceModelVariant("HEM-7600T_W", unverified=False),
            DeviceModelVariant("HEM-7600T_W-SH3W", unverified=False),
            DeviceModelVariant("HEM-7600T_W-Z", unverified=False),
            DeviceModelVariant("HEM-9601T-J3", unverified=True),
            DeviceModelVariant("HEM-9601T2-BR3", unverified=True),
            DeviceModelVariant("HEM-9601T_E3", unverified=True),
            DeviceModelVariant("HEM-9700T", unverified=True),
        ),
    ),
    "HEM-6232T": DeviceConfig(
        model="HEM-6232T",
        legacy_pairing_workarounds=True,
        endianness="big",
        user_start_addresses=[0x02E8, 0x0860],
        per_user_records_count=[100, 100],
        record_byte_size=0x0E,
        transmission_block_size=0x38,
        settings_read_address=0x0260,
        settings_write_address=0x02A4,
        settings_unread_records_bytes=[0x00, 0x08],
        settings_time_sync_bytes=[0x14, 0x1E],
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        record_parser="format_a_alt_6232",
        equivalent_model_ids=(
            DeviceModelVariant("HEM-1026T2-AJC", unverified=True),
            DeviceModelVariant("HEM-1026T2-AJE", unverified=True),
            DeviceModelVariant("HEM-1026T2-AKA", unverified=True),
            DeviceModelVariant("HEM-6232T-AP", unverified=False),
            DeviceModelVariant("HEM-6232T-D", unverified=True),
            DeviceModelVariant("HEM-6232T-E", unverified=False),
            DeviceModelVariant("HEM-6232T-Z", unverified=True),
            DeviceModelVariant("HEM-6233T", unverified=False),
            DeviceModelVariant("HEM-6320T-SH", unverified=True),
            DeviceModelVariant("HEM-6322T-SH", unverified=True),
            DeviceModelVariant("HEM-6323T", unverified=True),
            DeviceModelVariant("HEM-6324T", unverified=True),
            DeviceModelVariant("HEM-6325T", unverified=True),
        ),
    ),
    "HEM-7530T": DeviceConfig(
        model="HEM-7530T",
        legacy_pairing_workarounds=True,
        endianness="big",
        user_start_addresses=[0x02E8],
        per_user_records_count=[90],
        record_byte_size=0x0E,
        transmission_block_size=0x10,
        settings_read_address=0x0260,
        settings_write_address=0x02A4,
        # Unread/time sync not supported
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=None,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 89, "slot_index_bias": -1},
            ],
        },
        record_parser="format_a_alt",
        equivalent_model_ids=(
            DeviceModelVariant("HEM-6161T-E", unverified=True),
            DeviceModelVariant("HEM-6161T-RU", unverified=True),
            DeviceModelVariant("HEM-6161T2-BR", unverified=True),
            DeviceModelVariant("HEM-6231T-SH", unverified=True),
            DeviceModelVariant("HEM-6231T_Z", unverified=True),
            DeviceModelVariant("HEM-6231T2-JC", unverified=False),
            DeviceModelVariant("HEM-6231T2-JE", unverified=False),
            DeviceModelVariant("HEM-6231T2-JT3", unverified=False),
            DeviceModelVariant("HEM-7136T-SH3", unverified=True),
            DeviceModelVariant("HEM-7138JT-SH", unverified=True),
            DeviceModelVariant("HEM-7138T-SH", unverified=True),
            DeviceModelVariant("HEM-7139T-SH3", unverified=True),
            DeviceModelVariant("HEM-7143T1-AIN", unverified=True),
            DeviceModelVariant("HEM-7143T1-AP", unverified=True),
            DeviceModelVariant("HEM-7143T1-D", unverified=True),
            DeviceModelVariant("HEM-7143T1-E", unverified=True),
            DeviceModelVariant("HEM-7143T1_D", unverified=True),
            DeviceModelVariant("HEM-7143T1_EBK", unverified=True),
            DeviceModelVariant("HEM-7143T2-E", unverified=True),
            DeviceModelVariant("HEM-7143T2_ESL", unverified=True),
            DeviceModelVariant("HEM-7144T1-AU", unverified=True),
            DeviceModelVariant("HEM-7144T2-BR", unverified=True),
            DeviceModelVariant("HEM-7144T2-LA", unverified=True),
            DeviceModelVariant("HEM-716DT2-LA", unverified=True),
            DeviceModelVariant("HEM-7271L-SH3", unverified=True),
            DeviceModelVariant("HEM-7271P-SH3", unverified=False),
            DeviceModelVariant("HEM-7271T_SH3", unverified=False),
            DeviceModelVariant("HEM-7530T-Z", unverified=True),
            DeviceModelVariant("HEM-7530T1-BR3", unverified=False),
            DeviceModelVariant("HEM-7530T_AP3", unverified=False),
            DeviceModelVariant("HEM-7530T_E3", unverified=False),
            DeviceModelVariant("HEM-7530T_J3", unverified=False),
            DeviceModelVariant("HEM-7530T_JT3", unverified=False),
            DeviceModelVariant("HEM-8630T-SH", unverified=False),
        ),
    ),
    "HEM-7150T": DeviceConfig(
        model="HEM-7150T",
        legacy_pairing_workarounds=True,
        endianness="little",
        user_start_addresses=[0x0098],
        per_user_records_count=[60],
        record_byte_size=0x10,
        transmission_block_size=0x10,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=[0x00, 0x10],
        settings_time_sync_bytes=[0x2C, 0x3C],
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
            ],
        },
        record_parser="format_b",
        equivalent_model_ids=(
            DeviceModelVariant("HEM-7150T-CA", unverified=True),
            DeviceModelVariant("HEM-7150T-Z", unverified=False),
            DeviceModelVariant("HEM-7153JT_ASH", unverified=False),
            DeviceModelVariant("HEM-7153T_ASH", unverified=False),
            DeviceModelVariant("HEM-7156T-BR", unverified=False),
            DeviceModelVariant("HEM-7156T-LA", unverified=False),
            DeviceModelVariant("HEM-7156T_AAP", unverified=False),
            DeviceModelVariant("HEM-7156T_AP", unverified=False),
            DeviceModelVariant("HEM-7157T-AP", unverified=True),
            DeviceModelVariant("HEM-7158T-JC", unverified=True),
            DeviceModelVariant("HEM-7158T_AP3", unverified=True),
        ),
    ),
    # HEM-7151T: same layout as HEM-7150T but has 80 record slots
    "HEM-7151T": DeviceConfig(
        model="HEM-7151T",
        legacy_pairing_workarounds=True,
        endianness="little",
        user_start_addresses=[0x0098],
        per_user_records_count=[80],
        record_byte_size=0x10,
        transmission_block_size=0x10,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=[0x00, 0x10],
        settings_time_sync_bytes=[0x2C, 0x3C],
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 79, "slot_index_bias": -1},
            ],
        },
        record_parser="format_b",
        equivalent_model_ids=(
            DeviceModelVariant("HEM-7151T-Z", unverified=True),
        ),
    ),
    "HEM-7155T": DeviceConfig(
        model="HEM-7155T",
        legacy_pairing_workarounds=True,
        endianness="little",
        user_start_addresses=[0x0098, 0x0458],
        per_user_records_count=[60, 60],
        record_byte_size=0x10,
        transmission_block_size=0x10,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=[0x00, 0x10],
        settings_time_sync_bytes=[0x2C, 0x3C],
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
            ],
        },
        record_parser="format_b",
        equivalent_model_ids=(
            DeviceModelVariant("HEM-7155T-ALRU", unverified=False),
            DeviceModelVariant("HEM-7155T-D", unverified=False),
            DeviceModelVariant("HEM-7155T-EBK", unverified=False),
            DeviceModelVariant("HEM-7155T_AP", unverified=False),
            DeviceModelVariant("HEM-7155T_ASH3BK", unverified=False),
            DeviceModelVariant("HEM-7155T_ASH3SL", unverified=False),
            DeviceModelVariant("HEM-7155T_ESL", unverified=False),
            DeviceModelVariant("HEM-7155T_K4-D", unverified=True),
            DeviceModelVariant("HEM-7155T_K4-EBK", unverified=True),
            DeviceModelVariant("HEM-7155T_K4-ESL", unverified=True),
            DeviceModelVariant("HEM-7340T-CA", unverified=True),
            DeviceModelVariant("HEM-7340T-Z", unverified=False),
            DeviceModelVariant("HEM-7340T_K4-CA", unverified=True),
            DeviceModelVariant("HEM-7340T_K4-Z", unverified=True),
            DeviceModelVariant("HEM-7341T-Z", unverified=False),
            DeviceModelVariant("HEM-7341T_K4-Z", unverified=True),
        ),
    ),
    # HEM-7155T modern stack V2 — OS bonding only, same EEPROM addresses as V1
    "HEM-7155T-MW": DeviceConfig(
        model="HEM-7155T-MW",
        parent_service_uuid=MODERN_STACK_PARENT_SERVICE_UUID,
        rx_channel_uuids=["49123040-aee8-11e1-a74d-0002a5d5c51b"],
        tx_channel_uuids=["db5b55e0-aee7-11e1-965e-0002a5d5c51b"],
        requires_unlock=False,
        supports_pairing=False,
        supports_os_bonding_only=True,
        endianness="little",
        user_start_addresses=[0x0098, 0x0458],
        per_user_records_count=[60, 60],
        record_byte_size=0x10,
        transmission_block_size=0x38,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=None,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
            ],
        },
        record_parser="format_c",
        latest_selection_strategy="slot_desc_datetime",
    ),
    # HEM-7155T modern stack V3 — OS bonding only, different EEPROM addresses
    "HEM-7155T-MW3": DeviceConfig(
        model="HEM-7155T-MW3",
        parent_service_uuid=MODERN_STACK_PARENT_SERVICE_UUID,
        rx_channel_uuids=["49123040-aee8-11e1-a74d-0002a5d5c51b"],
        tx_channel_uuids=["db5b55e0-aee7-11e1-965e-0002a5d5c51b"],
        requires_unlock=False,
        supports_pairing=False,
        supports_os_bonding_only=True,
        endianness="little",
        user_start_addresses=[0x02E8, 0x06A8],
        per_user_records_count=[60, 60],
        record_byte_size=0x10,
        transmission_block_size=0x38,
        settings_read_address=0x0260,
        settings_write_address=0x02A4,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=None,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
            ],
        },
        record_parser="format_c",
        latest_selection_strategy="slot_desc_datetime",
    ),
    # HEM-7146T modern stack — OS bonding only, 1 user, 30 records
    "HEM-7146T": DeviceConfig(
        model="HEM-7146T",
        parent_service_uuid=MODERN_STACK_PARENT_SERVICE_UUID,
        rx_channel_uuids=["49123040-aee8-11e1-a74d-0002a5d5c51b"],
        tx_channel_uuids=["db5b55e0-aee7-11e1-965e-0002a5d5c51b"],
        requires_unlock=False,
        supports_pairing=False,
        supports_os_bonding_only=True,
        endianness="little",
        user_start_addresses=[0x02E8],
        per_user_records_count=[30],
        record_byte_size=0x0E,
        transmission_block_size=0x38,
        settings_read_address=0x0260,
        settings_write_address=0x02A4,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=None,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 29, "slot_index_bias": -1},
            ],
        },
        record_parser="format_c",
        latest_selection_strategy="slot_desc_datetime",
        equivalent_model_ids=(
            DeviceModelVariant("HEM-7146T2-EBK", unverified=True),
            DeviceModelVariant("HEM-7146T2-ESL", unverified=True),
            DeviceModelVariant("HEM-7146T2-JD", unverified=True),
            DeviceModelVariant("HEM-7146T2-JF", unverified=True),
        ),
    ),
    "HEM-7342T": DeviceConfig(
        model="HEM-7342T",
        legacy_pairing_workarounds=True,
        endianness="little",
        user_start_addresses=[0x0098, 0x06D8],
        per_user_records_count=[100, 100],
        record_byte_size=0x10,
        transmission_block_size=0x10,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=[0x00, 0x10],
        settings_time_sync_bytes=[0x2C, 0x3C],
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        record_parser="format_b",
        equivalent_model_ids=(
            DeviceModelVariant("HEM-7159T_AP3", unverified=False),
            DeviceModelVariant("HEM-7342T-CA", unverified=True),
            DeviceModelVariant("HEM-7342T-Z", unverified=False),
            DeviceModelVariant("HEM-7342T1-ACACD6", unverified=True),
            DeviceModelVariant("HEM-7343T-Z", unverified=False),
            DeviceModelVariant("HEM-7344JT_ASH3", unverified=False),
            DeviceModelVariant("HEM-7344T_ASH3BK", unverified=False),
            DeviceModelVariant("HEM-7344T_ASH3SL", unverified=False),
            DeviceModelVariant("HEM-7346T-AJC3", unverified=False),
            DeviceModelVariant("HEM-7346T-AJE3", unverified=False),
            DeviceModelVariant("HEM-7346T2-AJC32", unverified=False),
            DeviceModelVariant("HEM-7346T2-AJE32", unverified=False),
            DeviceModelVariant("HEM-7346T_ABR3", unverified=False),
            DeviceModelVariant("HEM-7346T_AP3", unverified=False),
            DeviceModelVariant("HEM-7347T-AJC3", unverified=False),
            DeviceModelVariant("HEM-7347T-AJE3", unverified=False),
            DeviceModelVariant("HEM-7347T2-AJC32", unverified=False),
            DeviceModelVariant("HEM-7347T2-AJE32", unverified=False),
            DeviceModelVariant("HEM-7349T_ABR", unverified=False),
            DeviceModelVariant("HEM-7361T-ALRU", unverified=False),
            DeviceModelVariant("HEM-7361T-AP", unverified=False),
            DeviceModelVariant("HEM-7361T-D", unverified=False),
            DeviceModelVariant("HEM-7361T-EBK", unverified=False),
            DeviceModelVariant("HEM-7361T1-BS", unverified=True),
            DeviceModelVariant("HEM-7361T_ESL", unverified=False),
        ),
    ),
    "HEM-7361T": DeviceConfig(
        model="HEM-7361T",
        legacy_pairing_workarounds=True,
        endianness="little",
        user_start_addresses=[0x0098, 0x06D8],
        per_user_records_count=[100, 100],
        record_byte_size=0x10,
        transmission_block_size=0x10,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=[0x00, 0x10],
        settings_time_sync_bytes=[0x2C, 0x3C],
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        record_parser="format_b",
    ),
    "HEM-7380T1": DeviceConfig(
        model="HEM-7380T1",
        parent_service_uuid=MODERN_STACK_PARENT_SERVICE_UUID,
        rx_channel_uuids=["49123040-aee8-11e1-a74d-0002a5d5c51b"],
        tx_channel_uuids=["db5b55e0-aee7-11e1-965e-0002a5d5c51b"],
        requires_unlock=False,
        supports_pairing=False,
        supports_os_bonding_only=True,
        endianness="little",
        user_start_addresses=[0x01C4, 0x0804],
        per_user_records_count=[100, 100],
        record_byte_size=0x10,
        transmission_block_size=0x38,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=None,
        index_pointer_layout={
            "index_region_byte_size": 0x18,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        record_parser="format_c",
        latest_selection_strategy="slot_desc_datetime",
        equivalent_model_ids=(
            
            DeviceModelVariant("HEM-7183T1-AP", unverified=True),
            DeviceModelVariant("HEM-7183T1-CAP", unverified=True),
            DeviceModelVariant("HEM-7183T1_FLBIN", unverified=True),
            DeviceModelVariant("HEM-7183T1_FLIN", unverified=True),
            DeviceModelVariant("HEM-7183T1_LAP", unverified=True),
            DeviceModelVariant("HEM-7188T1-LE", unverified=True),
            DeviceModelVariant("HEM-7188T1-LEO", unverified=True),
            DeviceModelVariant("HEM-7194T1-FLAP", unverified=True),
            DeviceModelVariant("HEM-7194T1-FLCAP", unverified=True),
            DeviceModelVariant("HEM-7194T1_FLBIN", unverified=True),
            DeviceModelVariant("HEM-7194T1_FLIN", unverified=True),
            DeviceModelVariant("HEM-7196T1-FLE", unverified=True),
            DeviceModelVariant("HEM-7196T1-FLEO", unverified=True),
            DeviceModelVariant("HEM-7376T1-ACACD6", unverified=True),
            DeviceModelVariant("HEM-7376T1-Z", unverified=True),
            DeviceModelVariant("HEM-7377T1-ZAZ", unverified=True),
            DeviceModelVariant("HEM-7380T", unverified=False),
            DeviceModelVariant("HEM-7380T1-EBK", unverified=False),
            DeviceModelVariant("HEM-7380T1-EOSL", unverified=True),
            DeviceModelVariant("HEM-7381T1-AZ", unverified=True),
            DeviceModelVariant("HEM-7382T1", unverified=False),   # same modern stack layout, confirmed by user report
            DeviceModelVariant("HEM-7382T1-AZAZ", unverified=True),
            DeviceModelVariant("HEM-7383T1-AP", unverified=False),
            DeviceModelVariant("HEM-7384T1-NBBR", unverified=False),
            DeviceModelVariant("HEM-7385T1-AJAZ3", unverified=True),
            DeviceModelVariant("HEM-7386T1-AJF3", unverified=True),
            DeviceModelVariant("HEM-7387T1-AJAZ3", unverified=True),
            DeviceModelVariant("HEM-7388T1-AJF3", unverified=True),
            DeviceModelVariant("HEM-7389T1-JM3", unverified=True),
        ),
    ),
    "HEM-7142T2": DeviceConfig(
        model="HEM-7142T2",
        parent_service_uuid=MODERN_STACK_PARENT_SERVICE_UUID,
        rx_channel_uuids=["49123040-aee8-11e1-a74d-0002a5d5c51b"],
        tx_channel_uuids=["db5b55e0-aee7-11e1-965e-0002a5d5c51b"],
        requires_unlock=False,
        supports_pairing=False,
        supports_os_bonding_only=True,
        endianness="little",
        # Single on-device measurement buffer region for this profile.
        user_start_addresses=[0x02E8],
        per_user_records_count=[14],
        record_byte_size=0x0E,
        transmission_block_size=0x38,
        settings_read_address=0x0260,
        settings_write_address=0x02A4,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=None,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "big",
            "backtrack_slots": 13,
            "collect_all_valid_in_index_window": True,
            "skip_full_scan_fallback_when_index_empty": True,
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 13, "slot_index_bias": -1},
            ],
            "record_addresses": [0x02E8],
            "record_byte_size": 0x0E,
            "record_step": 0x0E,
        },
        record_parser="format_c_7142",
        latest_selection_strategy="record_id_slot_datetime",
        enable_index_debug_logs=True,
        use_layout_fallback_scan=True,
        equivalent_model_ids=(
            DeviceModelVariant("HEM-7138K-SH", unverified=False),
            DeviceModelVariant("HEM-7140T1-AP", unverified=False),
            DeviceModelVariant("HEM-7141T1-AP", unverified=False),
            DeviceModelVariant("HEM-7142T1-AP", unverified=False),
            DeviceModelVariant("HEM-7142T2-AP", unverified=False),
            DeviceModelVariant("HEM-7142T2-Z", unverified=True),
            DeviceModelVariant("HEM-7142T2-ZAZ", unverified=True),
            DeviceModelVariant("HEM-7142T2_JAZ", unverified=False),
            DeviceModelVariant("HEM-716BT2-ZAZ", unverified=True),
            DeviceModelVariant("HEM-716CT2-Z", unverified=True),
        ),
    ),
}

DEFAULT_DEVICE_MODEL = "HEM-7142T2"


def _build_model_variant_map() -> dict[str, tuple[str, DeviceModelVariant]]:
    idx: dict[str, tuple[str, DeviceModelVariant]] = {}
    for canonical_model_id, profile in CANONICAL_DEVICE_PROFILES.items():
        for variant in profile.equivalent_model_ids:
            if variant.model_id in idx:
                raise ValueError(f"Duplicate catalog model variant {variant.model_id!r}")
            idx[variant.model_id] = (canonical_model_id, variant)
    return idx


MODEL_VARIANT_MAP: dict[str, tuple[str, DeviceModelVariant]] = _build_model_variant_map()


def is_model_variant_unverified(model: str) -> bool:
    """True when the selected ID is a catalog variant not yet validated on hardware."""
    entry = MODEL_VARIANT_MAP.get(model)
    return bool(entry and entry[1].unverified)


def get_device_config(model: str) -> DeviceConfig:
    """Get device configuration by model name.

    Alternate catalog model IDs map to a canonical entry in CANONICAL_DEVICE_PROFILES.
    """
    canonical_profile = CANONICAL_DEVICE_PROFILES.get(model)
    if canonical_profile is not None:
        return canonical_profile
    variant_entry = MODEL_VARIANT_MAP.get(model)
    if variant_entry:
        profile_key, _variant = variant_entry
        config = CANONICAL_DEVICE_PROFILES[profile_key]
        return replace(config, model=model)
    _LOGGER.warning(
        "Unknown device model '%s', falling back to %s",
        model, DEFAULT_DEVICE_MODEL,
    )
    config = CANONICAL_DEVICE_PROFILES[DEFAULT_DEVICE_MODEL]
    if config.model != model:
        return replace(config, model=model)
    return config


def get_supported_models() -> list[str]:
    """Return list of supported model strings (registry profiles + catalog variants)."""
    core = list(CANONICAL_DEVICE_PROFILES.keys())
    extra = list(MODEL_VARIANT_MAP.keys())
    return sorted(set(core) | set(extra))


def get_supported_model_stats() -> dict[str, int]:
    """Counts for UI copy: total selectable strings, profiles, and extra variant-only codes."""
    canonical_keys = set(CANONICAL_DEVICE_PROFILES.keys())
    variant_keys = set(MODEL_VARIANT_MAP.keys())
    return {
        "total": len(canonical_keys | variant_keys),
        "profiles": len(canonical_keys),
        "extra_variants": len(variant_keys - canonical_keys),
    }


_HEM_MODEL_CODE_RE = re.compile(r"(HEM-[A-Z0-9_.-]+)", re.IGNORECASE)


def infer_model_id_from_local_name(local_name: str | None) -> str | None:
    """Return a catalog model id if the BLE local name embeds a known HEM-* code.

    Many Omron cuffs advertise a name like ``HEM-7600T`` or ``Omron … HEM-7322T-D``;
    manufacturer data alone usually does not include the full model string. The mobile
    app can read additional identifiers after connecting; this only uses passive scan data.
    """
    if not local_name or not str(local_name).strip():
        return None
    match = _HEM_MODEL_CODE_RE.search(str(local_name).strip())
    if not match:
        return None
    token = match.group(1).strip()
    candidates = {
        token,
        token.upper(),
        token.replace(" ", ""),
        token.upper().replace(" ", ""),
    }
    supported = set(CANONICAL_DEVICE_PROFILES.keys()) | set(MODEL_VARIANT_MAP.keys())
    for cand in candidates:
        if cand in supported:
            return cand
    return None


def resolve_profile_model_id(model: str) -> str:
    """Registry profile key (EEPROM layout) for a model string, including catalog variants."""
    if model in CANONICAL_DEVICE_PROFILES:
        return model
    variant_entry = MODEL_VARIANT_MAP.get(model)
    if variant_entry:
        return variant_entry[0]
    return DEFAULT_DEVICE_MODEL

