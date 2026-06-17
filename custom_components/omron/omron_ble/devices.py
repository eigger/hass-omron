"""Omron device definitions and profile registry."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, NamedTuple

_LOGGER = logging.getLogger(__name__)

from .const import (
    CLASSIC_STACK_PARENT_SERVICE_UUID,
    MODERN_STACK_PARENT_SERVICE_UUID,
    STANDARD_BLOOD_PRESSURE_SERVICE_UUID,
    CLASSIC_STACK_RX_CHARACTERISTIC_UUIDS,
    CLASSIC_STACK_TX_CHARACTERISTIC_UUIDS,
    CLASSIC_STACK_UNLOCK_CHARACTERISTIC_UUID,
    DISCOVERABLE_PARENT_SERVICE_UUIDS,
    DEFAULT_DEVICE_MODEL,
)

from .record_parsers import (
    parse_classic_vital_14,
    parse_classic_vital_16_6401_family,
    parse_classic_vital_14_6232_family,
    parse_classic_vital_14_bitpacked,
)


class DeviceModelVariant(NamedTuple):
    """Alternate catalog model ID that shares EEPROM layout with a canonical profile."""

    model_id: str
    unverified: bool = False
    reason: str | None = None


class UnlockMode(str, Enum):
    """Transport unlock strategy for a device profile."""

    NONE = "none"
    CLASSIC_KEY = "classic_key"
    SECURE_SESSION = "secure_session"


class HostPairingMode(str, Enum):
    """Host-side pairing strategy for BLE security establishment."""

    CUSTOM_KEY = "custom_key"
    OS_BONDING = "os_bonding"
    # Reserved for future profiles that intentionally skip host-side pairing.
    NONE = "none"


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
    unlock_mode: UnlockMode = UnlockMode.CLASSIC_KEY
    host_pairing_mode: HostPairingMode = HostPairingMode.CUSTOM_KEY
    # Enable more aggressive GATT timing for classic custom-key profiles
    # (extra refresh/retry and pre-unlock 0x02 probe).
    aggressive_gatt_timing: bool = False

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
    # Optional override for EEPROM time layout (see omron_driver _decode/_encode_eeprom_time_payload).
    # - eeprom_time_classic_mixed: [2:8] = [month, year-2000, hour, day, second, minute]
    # - eeprom_time_linear_10:      [2:8] = [year-2000, month, day, hour, minute, second]
    # - eeprom_time_modern_offset8: [8:14] = [year-2000, month, day, hour, minute, second]
    # - eeprom_time_classic_offset8: [8:14] = [month, year-2000, hour, day, second, minute]
    # - eeprom_time_hem6401_prefix: [0:6] = [year-2000, month, day, hour, minute, second] in 16-byte block
    time_sync_layout: str | None = None
    index_pointer_layout: dict[str, Any] | None = None

    # Record layout key -> parser in parse_record()
    record_parser: str = "classic_vital_14"
    # When True, pick latest record by highest EEPROM slot index, then datetime (index-pointer devices).
    prefer_latest_by_slot_index: bool = False
    equivalent_model_ids: tuple[DeviceModelVariant, ...] = ()

    def __post_init__(self) -> None:
        """Validate unlock/pairing strategy combinations."""
        if (
            self.unlock_mode == UnlockMode.SECURE_SESSION
            and self.host_pairing_mode != HostPairingMode.OS_BONDING
        ):
            raise ValueError(
                "Invalid profile config for %s: secure-session unlock requires "
                "host_pairing_mode=OS_BONDING (unlock_mode=%s host_pairing_mode=%s)"
                % (
                    self.model,
                    self.unlock_mode,
                    self.host_pairing_mode,
                )
            )
        if (
            self.host_pairing_mode == HostPairingMode.OS_BONDING
            and self.unlock_mode == UnlockMode.CLASSIC_KEY
        ):
            raise ValueError(
                "Invalid profile config for %s: OS_BONDING cannot use classic-key unlock "
                "(unlock_mode=%s host_pairing_mode=%s)"
                % (
                    self.model,
                    self.unlock_mode,
                    self.host_pairing_mode,
                )
            )
        if (
            self.host_pairing_mode == HostPairingMode.NONE
            and self.unlock_mode != UnlockMode.NONE
        ):
            raise ValueError(
                "Invalid profile config for %s: host_pairing_mode=NONE requires unlock_mode=NONE "
                "(unlock_mode=%s host_pairing_mode=%s)"
                % (
                    self.model,
                    self.unlock_mode,
                    self.host_pairing_mode,
                )
            )
        if (
            self.unlock_mode == UnlockMode.NONE
            and self.host_pairing_mode == HostPairingMode.CUSTOM_KEY
        ):
            _LOGGER.warning(
                "Profile %s uses custom-key pairing with unlock_mode=NONE; verify catalog settings",
                self.model,
            )

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

    @property
    def supports_eeprom_time_sync(self) -> bool:
        """Return True if the device supports EEPROM-based time synchronization."""
        return (
            self.settings_time_sync_bytes is not None
            and self.settings_read_address is not None
            and self.settings_write_address is not None
        )

    def resolved_time_sync_layout(self) -> str:
        """Return effective EEPROM time-layout key for this profile."""
        if self.time_sync_layout is not None:
            return self.time_sync_layout
        if self.settings_time_sync_bytes == [0x2C, 0x3C]:
            return "eeprom_time_modern_offset8"
        return "eeprom_time_classic_mixed"

    def parse_record(self, data: bytes | bytearray) -> dict[str, Any]:
        """Parse a single record using the device-specific parser."""
        parser_map = {
            "classic_vital_14": parse_classic_vital_14,
            "classic_vital_16_6401_family": parse_classic_vital_16_6401_family,
            "classic_vital_14_bitpacked": parse_classic_vital_14_bitpacked,
            "classic_vital_14_6232_family": parse_classic_vital_14_6232_family,
        }
        parser = parser_map.get(self.record_parser)
        if parser is None:
            raise ValueError(f"Unknown record parser: {self.record_parser}")
        return parser(data, self.endianness)

    @property
    def is_modern_stack(self) -> bool:
        """Whether this profile uses the modern FE4A parent-service layout."""
        return self.parent_service_uuid == MODERN_STACK_PARENT_SERVICE_UUID

    @property
    def is_classic_stack(self) -> bool:
        """Whether this profile uses the classic 1812 parent-service layout."""
        return not self.is_modern_stack

    def is_service_compatible(self, service_uuids: list[str]) -> bool:
        """Check whether advertised GATT services match this profile's parent service."""
        if self.is_modern_stack:
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



from .device_catalog import CANONICAL_DEVICE_PROFILES

def _build_model_variant_map() -> dict[str, tuple[str, DeviceModelVariant]]:
    idx: dict[str, tuple[str, DeviceModelVariant]] = {}
    for canonical_model_id, profile in CANONICAL_DEVICE_PROFILES.items():
        for variant in profile.equivalent_model_ids:
            if variant.model_id in idx:
                raise ValueError(f"Duplicate catalog model variant {variant.model_id!r}")
            idx[variant.model_id] = (canonical_model_id, variant)
    return idx


MODEL_VARIANT_MAP: dict[str, tuple[str, DeviceModelVariant]] = _build_model_variant_map()


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


def resolve_model_catalog(model: str) -> tuple[str, DeviceModelVariant | None]:
    """Return EEPROM profile key and catalog variant metadata when ``model`` is an alias."""
    variant_entry = MODEL_VARIANT_MAP.get(model)
    if variant_entry:
        return variant_entry[0], variant_entry[1]
    return resolve_profile_model_id(model), None

