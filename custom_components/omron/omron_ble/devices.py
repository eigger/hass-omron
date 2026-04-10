"""Omron device definitions and record parsers."""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

# --- BLE UUID Constants ---
LEGACY_PARENT_SERVICE_UUID = "ecbe3980-c9a2-11e1-b1bd-0002a5d5c51b"
NEW_PARENT_SERVICE_UUID = "0000fe4a-0000-1000-8000-00805f9b34fb"

LEGACY_RX_CHANNEL_UUIDS = [
    "49123040-aee8-11e1-a74d-0002a5d5c51b",
    "4d0bf320-aee8-11e1-a0d9-0002a5d5c51b",
    "5128ce60-aee8-11e1-b84b-0002a5d5c51b",
    "560f1420-aee8-11e1-8184-0002a5d5c51b",
]
LEGACY_TX_CHANNEL_UUIDS = [
    "db5b55e0-aee7-11e1-965e-0002a5d5c51b",
    "e0b8a060-aee7-11e1-92f4-0002a5d5c51b",
    "0ae12b00-aee8-11e1-a192-0002a5d5c51b",
    "10e1ba60-aee8-11e1-89e5-0002a5d5c51b",
]
LEGACY_UNLOCK_UUID = "b305b680-aee7-11e1-a730-0002a5d5c51b"

ALL_SERVICE_UUIDS = [LEGACY_PARENT_SERVICE_UUID, NEW_PARENT_SERVICE_UUID]


# --- Bit-level parsing utility ---
def bytearray_bits_to_int(
    bytes_array: bytes | bytearray, endianess: str,
    first_bit: int, last_bit: int,
) -> int:
    """Extract an integer from a bit range within a byte array."""
    big_int = int.from_bytes(bytes_array, endianess)
    num_valid_bits = (last_bit - first_bit) + 1
    shifted = big_int >> (len(bytes_array) * 8 - (last_bit + 1))
    bitmask = (2 ** num_valid_bits) - 1
    return shifted & bitmask


# --- Record Parsers ---
def parse_record_format_a(data: bytes | bytearray, endianess: str) -> dict[str, Any]:
    """Parse format A: HEM-7322T, HEM-7600T, HEM-6232T style (big-endian).

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
    record["dia"] = bytearray_bits_to_int(data, endianess, 0, 7)
    record["sys"] = bytearray_bits_to_int(data, endianess, 8, 15) + 25
    year = bytearray_bits_to_int(data, endianess, 16, 23) + 2000
    record["bpm"] = bytearray_bits_to_int(data, endianess, 24, 31)
    record["mov"] = bytearray_bits_to_int(data, endianess, 32, 32)
    record["ihb"] = bytearray_bits_to_int(data, endianess, 33, 33)
    month = bytearray_bits_to_int(data, endianess, 34, 37)
    day = bytearray_bits_to_int(data, endianess, 38, 42)
    hour = bytearray_bits_to_int(data, endianess, 43, 47)
    minute = bytearray_bits_to_int(data, endianess, 52, 57)
    second = min(bytearray_bits_to_int(data, endianess, 58, 63), 59)
    record["datetime"] = datetime.datetime(year, month, day, hour, minute, second)
    return record


def parse_record_format_a_alt(data: bytes | bytearray, endianess: str) -> dict[str, Any]:
    """Parse format A-alt: HEM-7530T, HEM-6232T style.

    Same as format A but year bits are [18:23] instead of [16:23],
    and ihb/mov may be swapped for HEM-6232T.
    """
    record = {}
    record["dia"] = bytearray_bits_to_int(data, endianess, 0, 7)
    record["sys"] = bytearray_bits_to_int(data, endianess, 8, 15) + 25
    year = bytearray_bits_to_int(data, endianess, 18, 23) + 2000
    record["bpm"] = bytearray_bits_to_int(data, endianess, 24, 31)
    record["mov"] = bytearray_bits_to_int(data, endianess, 32, 32)
    record["ihb"] = bytearray_bits_to_int(data, endianess, 33, 33)
    month = bytearray_bits_to_int(data, endianess, 34, 37)
    day = bytearray_bits_to_int(data, endianess, 38, 42)
    hour = bytearray_bits_to_int(data, endianess, 43, 47)
    minute = bytearray_bits_to_int(data, endianess, 52, 57)
    second = min(bytearray_bits_to_int(data, endianess, 58, 63), 59)
    record["datetime"] = datetime.datetime(year, month, day, hour, minute, second)
    return record


def parse_record_format_a_alt_6232(data: bytes | bytearray, endianess: str) -> dict[str, Any]:
    """Parse format for HEM-6232T (ihb/mov order swapped vs 7530T)."""
    record = {}
    record["dia"] = bytearray_bits_to_int(data, endianess, 0, 7)
    record["sys"] = bytearray_bits_to_int(data, endianess, 8, 15) + 25
    year = bytearray_bits_to_int(data, endianess, 18, 23) + 2000
    record["bpm"] = bytearray_bits_to_int(data, endianess, 24, 31)
    record["ihb"] = bytearray_bits_to_int(data, endianess, 32, 32)
    record["mov"] = bytearray_bits_to_int(data, endianess, 33, 33)
    month = bytearray_bits_to_int(data, endianess, 34, 37)
    day = bytearray_bits_to_int(data, endianess, 38, 42)
    hour = bytearray_bits_to_int(data, endianess, 43, 47)
    minute = bytearray_bits_to_int(data, endianess, 52, 57)
    second = min(bytearray_bits_to_int(data, endianess, 58, 63), 59)
    record["datetime"] = datetime.datetime(year, month, day, hour, minute, second)
    return record


def parse_record_format_b(data: bytes | bytearray, endianess: str) -> dict[str, Any]:
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
    minute = bytearray_bits_to_int(data, endianess, 68, 73)
    second = min(bytearray_bits_to_int(data, endianess, 74, 79), 59)
    record["mov"] = bytearray_bits_to_int(data, endianess, 80, 80)
    record["ihb"] = bytearray_bits_to_int(data, endianess, 81, 81)
    month = bytearray_bits_to_int(data, endianess, 82, 85)
    day = bytearray_bits_to_int(data, endianess, 86, 90)
    hour = bytearray_bits_to_int(data, endianess, 91, 95)
    year = bytearray_bits_to_int(data, endianess, 98, 103) + 2000
    record["bpm"] = bytearray_bits_to_int(data, endianess, 104, 111)
    record["dia"] = bytearray_bits_to_int(data, endianess, 112, 119)
    record["sys"] = bytearray_bits_to_int(data, endianess, 120, 127) + 25
    record["datetime"] = datetime.datetime(year, month, day, hour, minute, second)
    return record


def parse_record_format_c(data: bytes | bytearray, endianess: str) -> dict[str, Any]:
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


def parse_record_format_c_7142(data: bytes | bytearray, endianess: str) -> dict[str, Any]:
    """Parse format C variant for HEM-7142T2.

    APK-side OGSC flows indicate the first 3 bytes are still SYS/DIA/BPM, but
    date/time bits can be partially inconsistent in some dumps. Keep vitals and
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
@dataclass
class DeviceConfig:
    """Configuration for a specific Omron device model."""

    # Device identity
    model: str

    # BLE channel configuration
    parent_service_uuid: str = LEGACY_PARENT_SERVICE_UUID
    rx_channel_uuids: list[str] = field(default_factory=lambda: list(LEGACY_RX_CHANNEL_UUIDS))
    tx_channel_uuids: list[str] = field(default_factory=lambda: list(LEGACY_TX_CHANNEL_UUIDS))
    unlock_uuid: str = LEGACY_UNLOCK_UUID
    requires_unlock: bool = True
    supports_pairing: bool = True
    supports_os_bonding_only: bool = False

    # EEPROM layout
    endianess: str = "big"
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
        return parser(data, self.endianess)

    def expected_service_family(self) -> str:
        """Return expected service family for model-level compatibility checks."""
        if self.parent_service_uuid == NEW_PARENT_SERVICE_UUID:
            return "new"
        return "legacy"

    def is_service_compatible(self, service_uuids: list[str]) -> bool:
        """Check whether advertised services match this model family."""
        if self.expected_service_family() == "new":
            return NEW_PARENT_SERVICE_UUID in service_uuids
        return LEGACY_PARENT_SERVICE_UUID in service_uuids


# --- Device Registry ---
DEVICE_REGISTRY: dict[str, DeviceConfig] = {
    "HEM-7322T": DeviceConfig(
        model="HEM-7322T",
        endianess="big",
        user_start_addresses=[0x02AC, 0x0824],
        per_user_records_count=[100, 100],
        record_byte_size=0x0E,
        transmission_block_size=0x38,
        settings_read_address=0x0260,
        settings_write_address=0x0286,
        settings_unread_records_bytes=[0x00, 0x08],
        settings_time_sync_bytes=[0x14, 0x1E],
        index_pointer_layout={
            "pointer_unsend_size": 0x08,
            "endianess": "big",
            "users": [
                {"pointer_offset": 0x00, "unsend_offset": 0x04, "pointer_mask": 0xFF, "pointer_min": 0, "pointer_max": 99, "latest_pos_correction": -1},
                {"pointer_offset": 0x02, "unsend_offset": 0x06, "pointer_mask": 0xFF, "pointer_min": 0, "pointer_max": 99, "latest_pos_correction": -1},
            ],
        },
        record_parser="format_a",
    ),
    "HEM-7600T": DeviceConfig(
        model="HEM-7600T",
        endianess="big",
        user_start_addresses=[0x02AC],
        per_user_records_count=[100],
        record_byte_size=0x0E,
        transmission_block_size=0x38,
        settings_read_address=0x0260,
        settings_write_address=0x0286,
        settings_unread_records_bytes=[0x00, 0x08],
        settings_time_sync_bytes=[0x14, 0x1E],
        index_pointer_layout={
            "pointer_unsend_size": 0x08,
            "endianess": "big",
            "users": [
                {"pointer_offset": 0x00, "unsend_offset": 0x04, "pointer_mask": 0xFF, "pointer_min": 0, "pointer_max": 99, "latest_pos_correction": -1},
            ],
        },
        record_parser="format_a",
    ),
    "HEM-6232T": DeviceConfig(
        model="HEM-6232T",
        endianess="big",
        user_start_addresses=[0x02E8, 0x0860],
        per_user_records_count=[100, 100],
        record_byte_size=0x0E,
        transmission_block_size=0x38,
        settings_read_address=0x0260,
        settings_write_address=0x02A4,
        settings_unread_records_bytes=[0x00, 0x08],
        settings_time_sync_bytes=[0x14, 0x1E],
        index_pointer_layout={
            "pointer_unsend_size": 0x10,
            "endianess": "big",
            "users": [
                {"pointer_offset": 0x00, "unsend_offset": 0x04, "pointer_mask": 0xFF, "pointer_min": 0, "pointer_max": 99, "latest_pos_correction": -1},
                {"pointer_offset": 0x02, "unsend_offset": 0x06, "pointer_mask": 0xFF, "pointer_min": 0, "pointer_max": 99, "latest_pos_correction": -1},
            ],
        },
        record_parser="format_a_alt_6232",
    ),
    "HEM-7530T": DeviceConfig(
        model="HEM-7530T",
        endianess="big",
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
            "pointer_unsend_size": 0x10,
            "endianess": "big",
            "users": [
                {"pointer_offset": 0x00, "unsend_offset": 0x04, "pointer_mask": 0xFF, "pointer_min": 0, "pointer_max": 89, "latest_pos_correction": -1},
            ],
        },
        record_parser="format_a_alt",
    ),
    "HEM-7150T": DeviceConfig(
        model="HEM-7150T",
        endianess="little",
        user_start_addresses=[0x0098],
        per_user_records_count=[60],
        record_byte_size=0x10,
        transmission_block_size=0x10,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=[0x00, 0x10],
        settings_time_sync_bytes=[0x2C, 0x3C],
        index_pointer_layout={
            "pointer_unsend_size": 0x10,
            "endianess": "little",
            "users": [
                {"pointer_offset": 0x00, "unsend_offset": 0x04, "pointer_mask": 0xFF, "pointer_min": 0, "pointer_max": 59, "latest_pos_correction": -1},
            ],
        },
        record_parser="format_b",
    ),
    "HEM-7155T": DeviceConfig(
        model="HEM-7155T",
        endianess="little",
        user_start_addresses=[0x0098, 0x0458],
        per_user_records_count=[60, 60],
        record_byte_size=0x10,
        transmission_block_size=0x10,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=[0x00, 0x10],
        settings_time_sync_bytes=[0x2C, 0x3C],
        index_pointer_layout={
            "pointer_unsend_size": 0x10,
            "endianess": "little",
            "users": [
                {"pointer_offset": 0x00, "unsend_offset": 0x04, "pointer_mask": 0xFF, "pointer_min": 0, "pointer_max": 59, "latest_pos_correction": -1},
                {"pointer_offset": 0x02, "unsend_offset": 0x06, "pointer_mask": 0xFF, "pointer_min": 0, "pointer_max": 59, "latest_pos_correction": -1},
            ],
        },
        record_parser="format_b",
    ),
    "HEM-7342T": DeviceConfig(
        model="HEM-7342T",
        endianess="little",
        user_start_addresses=[0x0098, 0x06D8],
        per_user_records_count=[100, 100],
        record_byte_size=0x10,
        transmission_block_size=0x10,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=[0x00, 0x10],
        settings_time_sync_bytes=[0x2C, 0x3C],
        index_pointer_layout={
            "pointer_unsend_size": 0x10,
            "endianess": "little",
            "users": [
                {"pointer_offset": 0x00, "unsend_offset": 0x04, "pointer_mask": 0xFF, "pointer_min": 0, "pointer_max": 99, "latest_pos_correction": -1},
                {"pointer_offset": 0x02, "unsend_offset": 0x06, "pointer_mask": 0xFF, "pointer_min": 0, "pointer_max": 99, "latest_pos_correction": -1},
            ],
        },
        record_parser="format_b",
    ),
    "HEM-7361T": DeviceConfig(
        model="HEM-7361T",
        endianess="little",
        user_start_addresses=[0x0098, 0x06D8],
        per_user_records_count=[100, 100],
        record_byte_size=0x10,
        transmission_block_size=0x10,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=[0x00, 0x10],
        settings_time_sync_bytes=[0x2C, 0x3C],
        index_pointer_layout={
            "pointer_unsend_size": 0x10,
            "endianess": "little",
            "users": [
                {"pointer_offset": 0x00, "unsend_offset": 0x04, "pointer_mask": 0xFF, "pointer_min": 0, "pointer_max": 99, "latest_pos_correction": -1},
                {"pointer_offset": 0x02, "unsend_offset": 0x06, "pointer_mask": 0xFF, "pointer_min": 0, "pointer_max": 99, "latest_pos_correction": -1},
            ],
        },
        record_parser="format_b",
    ),
    "HEM-7380T1": DeviceConfig(
        model="HEM-7380T1",
        parent_service_uuid=NEW_PARENT_SERVICE_UUID,
        rx_channel_uuids=["49123040-aee8-11e1-a74d-0002a5d5c51b"],
        tx_channel_uuids=["db5b55e0-aee7-11e1-965e-0002a5d5c51b"],
        requires_unlock=False,
        supports_pairing=False,
        supports_os_bonding_only=True,
        endianess="little",
        user_start_addresses=[0x01C4, 0x0804],
        per_user_records_count=[100, 100],
        record_byte_size=0x10,
        transmission_block_size=0x38,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=None,
        index_pointer_layout={
            "pointer_unsend_size": 0x18,
            "endianess": "little",
            "users": [
                {"pointer_offset": 0x00, "unsend_offset": 0x04, "pointer_mask": 0xFF, "pointer_min": 0, "pointer_max": 99, "latest_pos_correction": -1},
                {"pointer_offset": 0x02, "unsend_offset": 0x06, "pointer_mask": 0xFF, "pointer_min": 0, "pointer_max": 99, "latest_pos_correction": -1},
            ],
        },
        record_parser="format_c",
        latest_selection_strategy="slot_desc_datetime",
    ),
    "HEM-7142T2": DeviceConfig(
        model="HEM-7142T2",
        parent_service_uuid=NEW_PARENT_SERVICE_UUID,
        rx_channel_uuids=["49123040-aee8-11e1-a74d-0002a5d5c51b"],
        tx_channel_uuids=["db5b55e0-aee7-11e1-965e-0002a5d5c51b"],
        requires_unlock=False,
        supports_pairing=False,
        supports_os_bonding_only=True,
        endianess="little",
        # APK memory_map defines one BP data area for this model.
        user_start_addresses=[0x02E8],
        per_user_records_count=[14],
        record_byte_size=0x0E,
        transmission_block_size=0x38,
        settings_read_address=0x0260,
        settings_write_address=0x02A4,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=None,
        index_pointer_layout={
            "pointer_unsend_size": 0x10,
            "endianess": "big",
            "backtrack_slots": 13,
            "collect_all_valid_in_index_window": True,
            "skip_full_scan_fallback_when_index_empty": True,
            "users": [
                {"pointer_offset": 0x00, "unsend_offset": 0x04, "pointer_mask": 0xFF, "pointer_min": 0, "pointer_max": 13, "latest_pos_correction": -1},
            ],
            "record_addresses": [0x02E8],
            "record_byte_size": 0x0E,
            "record_step": 0x0E,
        },
        record_parser="format_c_7142",
        latest_selection_strategy="record_id_slot_datetime",
        enable_index_debug_logs=True,
        use_layout_fallback_scan=True,
    ),
}

DEFAULT_DEVICE_MODEL = "HEM-7322T"


def get_device_config(model: str) -> DeviceConfig:
    """Get device configuration by model name."""
    config = DEVICE_REGISTRY.get(model)
    if config is None:
        _LOGGER.warning(
            "Unknown device model '%s', falling back to %s",
            model, DEFAULT_DEVICE_MODEL,
        )
        config = DEVICE_REGISTRY[DEFAULT_DEVICE_MODEL]
    return config


def get_supported_models() -> list[str]:
    """Return list of supported device model names."""
    return list(DEVICE_REGISTRY.keys())
