"""Omron device definitions and profile registry."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from enum import Enum, StrEnum
from typing import Any

from .const import (
    CLASSIC_STACK_PARENT_SERVICE_UUID,
    MODERN_STACK_PARENT_SERVICE_UUID,
    STANDARD_BLOOD_PRESSURE_SERVICE_UUID,
    CLASSIC_STACK_RX_CHARACTERISTIC_UUIDS,
    CLASSIC_STACK_TX_CHARACTERISTIC_UUIDS,
    DEFAULT_DEVICE_MODEL,
)
from .model_aliases import MODEL_NUMBER_ALIASES
from .record_parsers import (
    parse_classic_vital_14,
    parse_classic_vital_14_6232_family,
    parse_classic_vital_14_bitpacked,
    parse_classic_vital_16_6401_family,
    parse_classic_vital_24_heartguide,
)

_LOGGER = logging.getLogger(__name__)


class UnlockMode(str, Enum):
    """Transport unlock strategy for a device profile."""

    NONE = "none"
    CLASSIC_KEY = "classic_key"
    # Stateless 0x11/0x91 handshake: host sends 0x11 + 4 arbitrary (nonce)
    # bytes, device echoes them in a 0x91 0x00 ack. Not a stored secret —
    # confirmed via HCI btsnoop where the same device echoed two different
    # host-chosen values across two connections. Required for memory access
    # outside the device's -P- pairing grace window on some modern-stack units.
    TOKEN_KEY = "token_key"
    SECURE_SESSION = "secure_session"


class HostPairingMode(str, Enum):
    """Host-side pairing strategy for BLE security establishment."""

    CUSTOM_KEY = "custom_key"
    OS_BONDING = "os_bonding"
    # Reserved for future profiles that intentionally skip host-side pairing.
    NONE = "none"


class BondPolicy(StrEnum):
    """Lifetime of the OS-level BLE bond, for ``host_pairing_mode=OS_BONDING``.

    One switch for the whole bonding strategy, so a profile can be moved
    between the two without leaving other flags to keep in sync. See
    ``_WLD3_BOND_POLICY`` in the catalog for the family-wide setting.
    """

    # Bond once and keep reusing it across connections.
    REUSE = "reuse"
    # Treat a bond as good for a single session: drop it when the session
    # closes so the next connection bonds from scratch. For devices that
    # invalidate their side of the bond after each session, a kept bond is
    # worse than none — the host offers an LTK the device no longer holds,
    # security fails, and the device hangs up.
    PER_SESSION = "per_session"


class ConnectType(StrEnum):
    """OMRON communication type; groups devices by BLE bonding/transfer behaviour."""

    UNKNOWN = ""
    WLB1_0 = "WLB1.0"
    WLD1_0 = "WLD1.0"
    WLD2_0 = "WLD2.0"
    WLD3_0 = "WLD3.0"
    WLD4_0 = "WLD4.0"
    WLS3_0 = "WLS3.0"


class Endianness(StrEnum):
    """Byte order for EEPROM/record decoding."""

    BIG = "big"
    LITTLE = "little"


class RecordParser(StrEnum):
    """Record-decoder selector (see record_parsers)."""

    CLASSIC_VITAL_14 = "classic_vital_14"
    CLASSIC_VITAL_16_6401_FAMILY = "classic_vital_16_6401_family"
    CLASSIC_VITAL_14_BITPACKED = "classic_vital_14_bitpacked"
    CLASSIC_VITAL_14_6232_FAMILY = "classic_vital_14_6232_family"
    CLASSIC_VITAL_24_HEARTGUIDE = "classic_vital_24_heartguide"


class TimeSyncLayout(StrEnum):
    """EEPROM time-sync field layout selector."""

    LINEAR_10 = "eeprom_time_linear_10"
    CLASSIC_MIXED = "eeprom_time_classic_mixed"
    CLASSIC_OFFSET8 = "eeprom_time_classic_offset8"
    MODERN_OFFSET8 = "eeprom_time_modern_offset8"
    HEM6401_PREFIX = "eeprom_time_hem6401_prefix"


@dataclass
class DeviceConfig:
    """Configuration for a specific Omron device model."""

    # Device identity
    model: str
    connect_type: ConnectType = ConnectType.UNKNOWN

    # BLE channel configuration
    parent_service_uuid: str = CLASSIC_STACK_PARENT_SERVICE_UUID
    rx_channel_uuids: list[str] = field(
        default_factory=lambda: list(CLASSIC_STACK_RX_CHARACTERISTIC_UUIDS)
    )
    tx_channel_uuids: list[str] = field(
        default_factory=lambda: list(CLASSIC_STACK_TX_CHARACTERISTIC_UUIDS)
    )
    unlock_mode: UnlockMode = UnlockMode.CLASSIC_KEY
    host_pairing_mode: HostPairingMode = HostPairingMode.CUSTOM_KEY
    # Enable more aggressive GATT timing for classic custom-key profiles
    # (extra refresh/retry and pre-unlock 0x02 probe).
    aggressive_gatt_timing: bool = False
    # Bond once at setup and never re-pair on pairing-mode adverts (avoids bond
    # churn). Only meaningful for host_pairing_mode=OS_BONDING, and only when
    # the profile does not already bond during connect (``pair_on_connect``),
    # which makes the advert-triggered re-pair a no-op either way.
    os_bond_once: bool = False
    # Bond lifetime; see BondPolicy. Only meaningful for OS_BONDING profiles.
    bond_policy: BondPolicy = BondPolicy.REUSE
    # Send the connect-time pair request only where a bond has to be created,
    # and let the peripheral drive security on every other connection. See
    # ``pair_on_connect_for``; ignored under BondPolicy.PER_SESSION.
    pair_only_when_pairing: bool = False
    # Connection attempts per session before giving up (see
    # ``establish_connection_with_bond_settle``). Lowered on profiles where a
    # failed attempt costs the stored bond, so one poll is one observation.
    connect_settle_attempts: int = 3
    # Seconds to stay idle at the end of a session, letting the device drop the
    # link itself rather than closing it here. Zero keeps the immediate close.
    # See ``OmronDeviceSession._await_peer_close``.
    peer_closes_session_sec: float = 0.0
    # Subscribe to Service Changed while pairing, the way the official app does.
    # See ``OmronDeviceSession.subscribe_service_changed``.
    subscribe_service_changed: bool = False

    # EEPROM layout
    endianness: Endianness = Endianness.BIG
    user_start_addresses: list[int] = field(default_factory=list)
    per_user_records_count: list[int] = field(default_factory=list)
    record_byte_size: int = 0x0E
    transmission_block_size: int = 0x2C

    # Settings addresses
    settings_read_address: int | None = None
    settings_write_address: int | None = None
    settings_time_sync_bytes: list[int] | None = None
    # Optional override for EEPROM time layout (see omron_driver _decode/_encode_eeprom_time_payload).
    # - eeprom_time_classic_mixed: [2:8] = [month, year-2000, hour, day, second, minute]
    # - eeprom_time_linear_10:      [2:8] = [year-2000, month, day, hour, minute, second]
    # - eeprom_time_modern_offset8: [8:14] = [year-2000, month, day, hour, minute, second]
    # - eeprom_time_classic_offset8: [8:14] = [month, year-2000, hour, day, second, minute]
    # - eeprom_time_hem6401_prefix: [0:6] = [year-2000, month, day, hour, minute, second] in 16-byte block
    time_sync_layout: TimeSyncLayout | None = None
    index_pointer_layout: dict[str, Any] | None = None

    # Enable each notify CCCD once and leave it enabled for the life of the
    # link, the way the app does. Disabling them at session close was why the
    # cuff dropped its side of the bond (#91): the spec has a peripheral keep
    # CCCD configuration per bonded client (Vol 3 Part G, 3.3.3.3) and small
    # stacks store it inside the bond record. ``_secure_unlock`` already
    # avoided the churn, its comment recording that this family rejects a
    # pairing request (0xff 0x26) when the unlock CCCD is dropped and re-added.
    keep_notify_subscriptions: bool = False

    # Record layout key -> parser in parse_record()
    record_parser: RecordParser = RecordParser.CLASSIC_VITAL_14
    equivalent_model_ids: tuple[str, ...] = ()

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
    def display_model(self) -> str:
        """Model name for the UI, kept in the HEM- form.

        A profile can be reached by a retail carton name -- BP5465 is the only
        one in the catalog -- and that is also what the cuff answers with over
        GATT, so it can end up in the device registry as the model. Show the
        profile key instead: it is a real HEM designation and the one thing we
        know maps to this device. There is no retail-to-variant table to do
        better with, and inventing one would be a guess.

        An unrecognised name is left alone rather than replaced with the
        fallback profile, which would name a device we did not identify. Logs
        keep ``model`` either way, so a report still shows what was resolved.
        """
        if self.model.upper().startswith("HEM-"):
            return self.model
        alias = MODEL_NUMBER_ALIASES.get(self.model)
        if alias:
            return alias
        if self.model in CANONICAL_DEVICE_PROFILES or self.model in MODEL_VARIANT_MAP:
            return resolve_profile_model_id(self.model)
        return self.model

    @property
    def num_users(self) -> int:
        """Return the number of users this device supports."""
        return len(self.user_start_addresses)

    @property
    def is_single_channel(self) -> bool:
        """Return True if the device uses a single BLE channel."""
        return len(self.tx_channel_uuids) == 1

    @property
    def supports_eeprom_time_sync(self) -> bool:
        """Return True if the device supports EEPROM-based time synchronization."""
        return (
            self.settings_time_sync_bytes is not None
            and self.settings_read_address is not None
            and self.settings_write_address is not None
        )

    def resolved_time_sync_layout(self) -> TimeSyncLayout:
        """Return effective EEPROM time-layout key for this profile."""
        if self.time_sync_layout is not None:
            return self.time_sync_layout
        if self.settings_time_sync_bytes == [0x2C, 0x3C]:
            return TimeSyncLayout.MODERN_OFFSET8
        return TimeSyncLayout.CLASSIC_MIXED

    def parse_record(self, data: bytes | bytearray) -> dict[str, Any]:
        """Parse a single record using the device-specific parser."""
        parser_map = {
            RecordParser.CLASSIC_VITAL_14: parse_classic_vital_14,
            RecordParser.CLASSIC_VITAL_16_6401_FAMILY: parse_classic_vital_16_6401_family,
            RecordParser.CLASSIC_VITAL_14_BITPACKED: parse_classic_vital_14_bitpacked,
            RecordParser.CLASSIC_VITAL_14_6232_FAMILY: parse_classic_vital_14_6232_family,
            RecordParser.CLASSIC_VITAL_24_HEARTGUIDE: parse_classic_vital_24_heartguide,
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

    @property
    def unpair_after_session(self) -> bool:
        """Drop the OS bond when the session closes (``BondPolicy.PER_SESSION``).

        Derived rather than configurable so "drop the bond and never make
        another" cannot be set: an earlier build did exactly that and every
        connection after the first failed with nothing to reconnect with.
        """
        return (
            self.bond_policy == BondPolicy.PER_SESSION
            and self.host_pairing_mode == HostPairingMode.OS_BONDING
        )

    @property
    def pair_on_connect(self) -> bool:
        """Bond during connect, before GATT discovery.

        WLD3.0 cuffs drop the link shortly after connect when security is not
        already up. Bonding as part of connect establishes it before any
        characteristic is touched, whereas pairing after discovery leaves a
        window where the device sees an unencrypted host poking at GATT —
        which is when the observed disconnects happen. On an already-bonded
        device the backend re-encrypts with the stored LTK rather than
        re-bonding, so this does not churn a kept bond.

        Always true under ``BondPolicy.PER_SESSION``, which is what makes
        dropping the bond safe: the pairing that replaces it is part of the
        next connect. Deriving it here rather than asking profiles to set
        both means "drop the bond but never make a new one" — an earlier
        regression that left the next connection with nothing to reconnect
        with — cannot be configured at all.
        """
        if self.host_pairing_mode != HostPairingMode.OS_BONDING:
            return False
        if self.bond_policy == BondPolicy.PER_SESSION:
            return True
        return self.connect_type in (ConnectType.WLD3_0, ConnectType.WLD4_0)

    def pair_on_connect_for(self, *, pairing_session: bool) -> bool:
        """``pair_on_connect`` for one session, honouring ``pair_only_when_pairing``.

        A connect-time pair request is not free on ESP32 proxies: when it does
        not complete, ESP-IDF treats the failure as an authentication failure
        and deletes the stored bond from flash (``btc_dm_ble_auth_cmpl_evt``
        routes SMP_CONN_TOUT through its default branch). One such failure
        costs the credential permanently, so a profile can ask to send the
        request only where a bond has to be created.
        """
        if not self.pair_on_connect:
            return False
        # PER_SESSION drops the bond on close, so every connect has to remake
        # it; opting out here would leave the next poll with nothing.
        if self.bond_policy == BondPolicy.PER_SESSION:
            return True
        return pairing_session or not self.pair_only_when_pairing

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



# Deliberately not at module top: device_catalog.py imports types (DeviceConfig,
# UnlockMode, etc.) from this module, so importing it back at the top would be
# circular. Safe here since everything device_catalog.py needs from this module
# is already defined above.
from .device_catalog import CANONICAL_DEVICE_PROFILES  # noqa: E402

def _build_model_variant_map() -> dict[str, str]:
    idx: dict[str, str] = {}
    for canonical_model_id, profile in CANONICAL_DEVICE_PROFILES.items():
        for variant_id in profile.equivalent_model_ids:
            if variant_id in idx:
                raise ValueError(f"Duplicate catalog model variant {variant_id!r}")
            idx[variant_id] = canonical_model_id
    return idx


MODEL_VARIANT_MAP: dict[str, str] = _build_model_variant_map()


def get_device_config(model: str) -> DeviceConfig:
    """Get device configuration by model name.

    Alternate catalog model IDs map to a canonical entry in CANONICAL_DEVICE_PROFILES.
    """
    canonical_profile = CANONICAL_DEVICE_PROFILES.get(model)
    if canonical_profile is not None:
        return canonical_profile
    variant_profile = MODEL_VARIANT_MAP.get(model)
    if variant_profile:
        config = CANONICAL_DEVICE_PROFILES[variant_profile]
        return replace(config, model=model)
    alias = MODEL_NUMBER_ALIASES.get(model)
    if alias:
        # The name the device answers with is kept, so logs show what it said;
        # display_model turns it into the HEM designation for the UI.
        return replace(get_device_config(alias), model=model)
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


def _exact_catalog_model_id(token: str) -> str | None:
    """Catalog model id for an exact name, ignoring case and inner spaces.

    Aliases count: a Model Number String is exactly where a carton name shows
    up, and MODEL_NUMBER_ALIASES exists to turn those into catalog ids.
    """
    supported = set(CANONICAL_DEVICE_PROFILES.keys()) | set(MODEL_VARIANT_MAP.keys())
    for cand in (token, token.upper(), token.replace(" ", "").upper()):
        if cand in supported:
            return cand
        alias = MODEL_NUMBER_ALIASES.get(cand)
        if alias:
            return alias
    return None


def infer_model_id_from_local_name(local_name: str | None) -> str | None:
    """Return a catalog model id if the BLE local name embeds a known HEM-* code.

    Many Omron cuffs advertise a name like ``HEM-7600T`` or ``Omron … HEM-7322T-D``;
    manufacturer data alone usually does not include the full model string. The mobile
    app can read additional identifiers after connecting; this only uses passive scan data.
    """
    if not local_name or not str(local_name).strip():
        return None
    name = str(local_name).strip()
    match = _HEM_MODEL_CODE_RE.search(name)
    if not match:
        # The config flow also feeds this the GATT Model Number String, which is
        # a different namespace: a US retail cuff answers with its carton name,
        # like the BP5465 in issue #91, and no HEM-* code appears anywhere in
        # it. Fall back to an exact catalog id so a name that is already one
        # resolves; anything else still infers nothing.
        return _exact_catalog_model_id(name)
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
    for cand in candidates:
        alias = MODEL_NUMBER_ALIASES.get(cand)
        if alias:
            return alias
    return None


def resolve_profile_model_id(model: str) -> str:
    """Registry profile key (EEPROM layout) for a model string, including catalog variants."""
    if model in CANONICAL_DEVICE_PROFILES:
        return model
    variant_profile = MODEL_VARIANT_MAP.get(model)
    if variant_profile:
        return variant_profile
    alias = MODEL_NUMBER_ALIASES.get(model)
    if alias:
        return resolve_profile_model_id(alias)
    return DEFAULT_DEVICE_MODEL

