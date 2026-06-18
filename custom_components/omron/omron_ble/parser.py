"""Parser for Omron BLE blood pressure monitors.

Handles device detection from BLE advertisements and active polling
for measurement data via GATT connection.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice

from bluetooth_sensor_state_data import BluetoothData
from home_assistant_bluetooth import BluetoothServiceInfoBleak
from sensor_state_data import (
    BinarySensorDeviceClass,
    SensorLibrary,
    SensorUpdate,
    SensorDeviceClass,
    Units,
)
from homeassistant.util import dt as dt_util

from .const import (
    BATTERY_LEVEL_UUID,
    FIRMWARE_REVISION_UUID,
    HARDWARE_REVISION_UUID,
    MANUFACTURER_NAME_UUID,
    MODEL_NUMBER_UUID,
    BP_MEASUREMENT_CHAR_UUID,
    BP_RACP_CHAR_UUID,
    OMRON_MANUFACTURER_ID,
    DISCOVERABLE_PARENT_SERVICE_UUIDS,
    DEFAULT_DEVICE_MODEL,
    ExtendedBinarySensorDeviceClass,
)
from .setup import async_sync_device_time, async_sync_eeprom_time
from .devices import HostPairingMode, DeviceConfig, get_device_config, resolve_profile_model_id
from .omron_driver import (
    OmronDeviceSession,
    OmronDeviceDriver,
)
from ..util import slugify_for_entity_key

_LOGGER = logging.getLogger(__name__)


def _normalize_user_aliases(user_aliases: dict[int, str] | None) -> dict[int, str]:
    """Build 1-based user index -> display label; empty strings become user{n}."""
    if not user_aliases:
        return {}
    out: dict[int, str] = {}
    for k, v in user_aliases.items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        label = str(v).strip() if v is not None else ""
        out[idx] = label if label else f"user{idx}"
    return out

class OmronBluetoothDeviceData(BluetoothData):
    """Data handler for Omron BLE blood pressure monitors."""

    def __init__(
        self,
        device_model: str = DEFAULT_DEVICE_MODEL,
        user_aliases: dict[int, str] | None = None,
    ) -> None:
        super().__init__()
        self.last_service_info: BluetoothServiceInfoBleak | None = None
        self.pending = True
        self._device_model = device_model
        self._device_config: DeviceConfig = get_device_config(device_model)
        self._driver = OmronDeviceDriver(self._device_config)
        self._user_aliases: dict[int, str] = _normalize_user_aliases(user_aliases)
        self._last_record_signature: tuple[Any, ...] | None = None
        self._last_record_signatures_by_user: dict[int, tuple[Any, ...]] = {}
        self._bp_char_unavailable = False
        self._bls_racp_unavailable_logged = False
        self._poll_guard = asyncio.Lock()
        self.omron_extra_attributes = {}

        # Advertisement Status Flags
        # Names referenced from __init__.py (poll triggers): forced_transfer,
        # invalid_time, pairing_mode. Do NOT rename without auditing callers.
        self.forced_transfer: bool = False
        self.invalid_time: bool = False
        self.pairing_mode: bool = False
        # Additional fields parsed from MSD. Kept as
        # informational attributes; not exposed as sensors today.
        self.streaming_mode: bool = False
        self.service_uuid_mode: bool = False
        self.user_register_count: int = 0
        self.guidance_mode: int = 0
        self.result_identifier_num: int = 0

        self._seed_measurement_entities()

    @property
    def device_model(self) -> str:
        """Return the configured device model."""
        return self._device_model

    @device_model.setter
    def device_model(self, model: str) -> None:
        """Set the device model and update internal config."""
        self._device_model = model
        self._device_config = get_device_config(model)
        self._driver = OmronDeviceDriver(self._device_config)
        self._last_record_signature = None
        self._last_record_signatures_by_user = {}

    def _seed_measurement_entities(self) -> None:
        """Pre-register measurement sensor descriptions for offline startup.

        This ensures entities exist on setup so RestoreEntity can show last values
        before the first successful device poll.
        """
        from .const import ExtendedSensorDeviceClass

        multi_user_mode = self._device_config.num_users > 1
        user_ids = (
            list(range(1, self._device_config.num_users + 1))
            if multi_user_mode
            else [None]
        )
        for user in user_ids:
            key_suffix, name_suffix = self._measurement_user_suffixes(
                user if isinstance(user, int) else None, multi_user_mode
            )
            self._publish_seed_measurements_for_suffix(
                key_suffix,
                name_suffix,
                ExtendedSensorDeviceClass,
            )

    def _seed_measurement_specs(self, sensor_classes: Any) -> tuple[tuple[str, str | None, Any, str], ...]:
        """Declarative spec for all measurement entities that must exist at startup."""
        return (
            ("blood_pressure_systolic", "mmHg", sensor_classes.BLOOD_PRESSURE_SYSTOLIC, "Systolic"),
            ("blood_pressure_diastolic", "mmHg", sensor_classes.BLOOD_PRESSURE_DIASTOLIC, "Diastolic"),
            ("heart_rate", "bpm", sensor_classes.HEART_RATE, "Pulse"),
            ("pulse_pressure", "mmHg", sensor_classes.PULSE_PRESSURE, "Pulse Pressure"),
            (
                "mean_arterial_pressure_estimated",
                "mmHg",
                sensor_classes.MEAN_ARTERIAL_PRESSURE_ESTIMATED,
                "Estimated MAP",
            ),
            ("blood_pressure_category", None, sensor_classes.BLOOD_PRESSURE_CATEGORY, "BP Category (ACC/AHA)"),
            ("shock_index", "ratio", sensor_classes.SHOCK_INDEX, "Shock Index"),
            ("rate_pressure_product", "mmHg*bpm", sensor_classes.RATE_PRESSURE_PRODUCT, "Rate Pressure Product"),
            ("measurement_timestamp", None, SensorDeviceClass.TIMESTAMP, "Measured At"),
        )

    def _publish_seed_measurements_for_suffix(
        self,
        key_suffix: str,
        name_suffix: str,
        sensor_classes: Any,
    ) -> None:
        """Publish all startup measurement entities for one user suffix."""
        for base_key, unit, device_class, base_name in self._seed_measurement_specs(sensor_classes):
            self._publish_measurement_sensor(
                base_key,
                unit,
                None,
                device_class,
                base_name,
                key_suffix,
                name_suffix,
            )

    def supported(self, data: BluetoothServiceInfoBleak) -> bool:
        if super().supported(data):
            return True
        for uuid in DISCOVERABLE_PARENT_SERVICE_UUIDS:
            if uuid in data.service_uuids:
                return True
        md = getattr(data, "manufacturer_data", None) or {}
        if OMRON_MANUFACTURER_ID in md:
            return True
        name = (data.name or "").strip()
        if name:
            if "omron" in name.lower():
                return True
            if name.upper().startswith("HEM-"):
                return True
        return False

    def _start_update(self, service_info: BluetoothServiceInfoBleak) -> None:
        """Update from BLE advertisement data."""
        md = getattr(service_info, "manufacturer_data", None) or {}
        if OMRON_MANUFACTURER_ID in md:
            self._parse_omron_msd(md[OMRON_MANUFACTURER_ID])

        # Check if any known Omron service UUID is present
        for uuid in DISCOVERABLE_PARENT_SERVICE_UUIDS:
            if uuid in service_info.service_uuids:
                self._setup_device_info(service_info)
                self.last_service_info = service_info
                return

        # Omron manufacturer company id (manifest manufacturer_id 526)
        if OMRON_MANUFACTURER_ID in md:
            self._setup_device_info(service_info)
            self.last_service_info = service_info
            return

        # Fallback: device name (align with manifest bluetooth local_name matchers)
        name = (service_info.name or "").strip()
        if name:
            if "omron" in name.lower():
                self._setup_device_info(service_info)
                self.last_service_info = service_info
                return
            if name.upper().startswith("HEM-"):
                self._setup_device_info(service_info)
                self.last_service_info = service_info

    def _parse_omron_msd(self, payload: bytes) -> None:
        """Parse Omron Manufacturer Specific Data (MSD) for status flags.

        Bleak's ``manufacturer_data`` value already excludes the 2-byte LE
        manufacturer ID, so ``payload[0]`` is the first format byte of the
        Omron MSD body.

        Three advertisement formats are recognized via ``payload[0]``:

        - ``0x03`` — older single/dual-user BPM. Carries ``invalid_time``,
          ``pairing_mode``, ``guidance_mode``, ``result_identifier_num``.
        - ``0x08`` — BLS-style fixed length (10 or 13 byte payload). Adds
          ``streaming_mode``, ``service_uuid_mode``, ``forced_transfer``.
        - ``0x09`` — newest, variable per registered user count (9/11/13/15).
          Same status bits as ``0x08`` but distinct length contract.

        Advertisements that fail the length contract are logged at DEBUG and
        ignored (sensor state is not flipped) to avoid acting on transient
        or truncated packets.
        """
        if not payload or len(payload) < 2:
            return

        fields = self._decode_omron_msd_fields(payload)
        if fields is None:
            _LOGGER.debug(
                "Ignoring Omron MSD: format=0x%02X len=%d (length contract mismatch)",
                payload[0],
                len(payload),
            )
            return

        # Update instance attributes referenced externally (poll triggers in
        # custom_components/omron/__init__.py).
        self.invalid_time = fields["invalid_time"]
        self.pairing_mode = fields["pairing_mode"]
        self.forced_transfer = fields["forced_transfer"]
        # Additional fields (informational; not yet exposed as sensors).
        self.streaming_mode = fields["streaming_mode"]
        self.service_uuid_mode = fields["service_uuid_mode"]
        self.user_register_count = fields["user_register_count"]
        self.guidance_mode = fields["guidance_mode"]
        self.result_identifier_num = fields["result_identifier_num"]

        self.update_binary_sensor(
            "forced_transfer",
            self.forced_transfer,
            ExtendedBinarySensorDeviceClass.FORCED_TRANSFER,
            "Data Pending",
        )
        self.update_binary_sensor(
            "invalid_time",
            self.invalid_time,
            ExtendedBinarySensorDeviceClass.INVALID_TIME,
            "Time Sync Required",
        )
        self.update_binary_sensor(
            "pairing_mode",
            self.pairing_mode,
            ExtendedBinarySensorDeviceClass.PAIRING_MODE,
            "Pairing Mode",
        )

    @staticmethod
    def _decode_omron_msd_fields(payload: bytes) -> dict[str, Any] | None:
        """Decode OMRON MSD into a normalized dict, or ``None`` on mismatch.

        Bleak strips the 2-byte LE manufacturer ID before delivering the
        payload, so ``payload[0]`` is the first format byte of the MSD body.

        Format 0x03 (older BPM, MSD >= 5 bytes):
            ``len(payload) >= 3``; reads ``payload[1]`` (status bits) and
            ``payload[2]`` (result identifier num).

        Formats 0x01 / 0x02 / 0x06 (legacy WLP single/multi-user; 3-byte
        per-user stride):
            Status byte at ``payload[1]`` uses the same bit layout as
            0x08 / 0x09.  Per-user sequence numbers occupy
            ``payload[i*3 + 2 .. i*3 + 4]``, so minimum required length is
            ``4 + (user_count * 3)``.  HEM-7142T2 and other older
            single-user cuffs emit format 0x01 with ``len(payload) == 5``.

        Format 0x08 (BLS-style, fixed per user count):
            user_count == 0 → ``len(payload) == 10`` (MSD 12B)
            user_count == 1 → ``len(payload) == 13`` (MSD 15B)
            user_count 2 / 3 → not defined for this format → rejected.

        Format 0x09 (newest, 2 bytes per registered user):
            user_count == 0 → ``len(payload) == 9``  (MSD 11B)
            user_count == 1 → ``len(payload) == 11`` (MSD 13B)
            user_count == 2 → ``len(payload) == 13`` (MSD 15B)
            user_count == 3 → ``len(payload) == 15`` (MSD 17B)

        Any other ``payload[0]`` value is unsupported → ``None``.
        Payloads shorter than 2 bytes also return ``None`` (defensive: the
        caller already filters, but the static method is reusable).
        """
        if len(payload) < 2:
            return None
        b11 = payload[0]

        if b11 == 0x03:
            if len(payload) < 3:
                return None
            b12 = payload[1]
            return {
                "user_register_count": b12 & 0x03,
                "invalid_time": bool(b12 & 0x04),
                "pairing_mode": bool(b12 & 0x08),
                "guidance_mode": (b12 & 0x30) >> 4,
                "result_identifier_num": payload[2],
                # Not present in this format.
                "streaming_mode": False,
                "service_uuid_mode": False,
                "forced_transfer": False,
            }

        if b11 in (0x01, 0x02, 0x06):
            b13 = payload[1]
            user_count = b13 & 0x03
            min_len = 4 + (user_count * 3)
            if len(payload) < min_len:
                return None
            return {
                "user_register_count": user_count,
                "invalid_time": bool(b13 & 0x04),
                "pairing_mode": bool(b13 & 0x08),
                "streaming_mode": bool(b13 & 0x10),
                "service_uuid_mode": bool(b13 & 0x20),
                "forced_transfer": bool(b13 & 0x40),
                # Not present in this format.
                "guidance_mode": 0,
                "result_identifier_num": 0,
            }

        if b11 == 0x08:
            b13 = payload[1]
            user_count = b13 & 0x03
            length_ok = (
                (user_count == 0 and len(payload) == 10)
                or (user_count == 1 and len(payload) == 13)
            )
            if not length_ok:
                return None
            return {
                "user_register_count": user_count,
                "invalid_time": bool(b13 & 0x04),
                "pairing_mode": bool(b13 & 0x08),
                "streaming_mode": bool(b13 & 0x10),
                "service_uuid_mode": bool(b13 & 0x20),
                "forced_transfer": bool(b13 & 0x40),
                # Not present in this format.
                "guidance_mode": 0,
                "result_identifier_num": 0,
            }

        if b11 == 0x09:
            b10 = payload[1]
            user_count = b10 & 0x03
            expected_len = {0: 9, 1: 11, 2: 13, 3: 15}.get(user_count)
            if expected_len is None or len(payload) != expected_len:
                return None
            return {
                "user_register_count": user_count,
                "invalid_time": bool(b10 & 0x04),
                "pairing_mode": bool(b10 & 0x08),
                "streaming_mode": bool(b10 & 0x10),
                "service_uuid_mode": bool(b10 & 0x20),
                "forced_transfer": bool(b10 & 0x40),
                # Not present in this format.
                "guidance_mode": 0,
                "result_identifier_num": 0,
            }

        return None

    def _measurement_user_suffixes(
        self, user: int | None, multi_user: bool
    ) -> tuple[str, str]:
        """Return (sensor_key_suffix, friendly_name_suffix) for multi-user cuffs.

        Keys use a slug of the configured alias (default user1, user2, …).
        """
        if not multi_user or user is None:
            return "", ""
        label = self._user_aliases.get(user)
        if not label:
            label = f"user{user}"
        slug = slugify_for_entity_key(label)
        if not slug:
            slug = f"user{user}"
        return f"_{slug}", f" ({label})"

    def _publish_measurement_sensor(
        self,
        base_key: str,
        unit: str | None,
        value: Any,
        device_class: Any,
        base_name: str,
        key_suffix: str,
        name_suffix: str,
    ) -> None:
        """Publish one measurement sensor with precomputed user suffixes."""
        self.update_sensor(
            f"{base_key}{key_suffix}",
            unit,
            value,
            device_class=device_class,
            name=f"{base_name}{name_suffix}",
        )

    def _publish_primary_measurements(
        self,
        record: dict[str, Any],
        key_suffix: str,
        name_suffix: str,
        sensor_classes: Any,
    ) -> tuple[Any, Any, Any]:
        """Publish direct values read from the cuff."""
        sys_val = record.get("sys")
        dia_val = record.get("dia")
        bpm_val = record.get("bpm")
        self._publish_measurement_sensor(
            "blood_pressure_systolic",
            "mmHg",
            sys_val,
            sensor_classes.BLOOD_PRESSURE_SYSTOLIC,
            "Systolic",
            key_suffix,
            name_suffix,
        )
        self._publish_measurement_sensor(
            "blood_pressure_diastolic",
            "mmHg",
            dia_val,
            sensor_classes.BLOOD_PRESSURE_DIASTOLIC,
            "Diastolic",
            key_suffix,
            name_suffix,
        )
        self._publish_measurement_sensor(
            "heart_rate",
            "bpm",
            bpm_val,
            sensor_classes.HEART_RATE,
            "Pulse",
            key_suffix,
            name_suffix,
        )
        return sys_val, dia_val, bpm_val

    def _publish_pressure_derived_metrics(
        self,
        sys_val: Any,
        dia_val: Any,
        key_suffix: str,
        name_suffix: str,
        sensor_classes: Any,
    ) -> None:
        """Publish metrics derivable from systolic/diastolic pair."""
        if not (
            isinstance(sys_val, (int, float))
            and isinstance(dia_val, (int, float))
            and sys_val > dia_val
        ):
            return
        pulse_pressure = float(sys_val - dia_val)
        estimated_map = float(dia_val + (pulse_pressure / 3))
        self._publish_measurement_sensor(
            "pulse_pressure",
            "mmHg",
            round(pulse_pressure, 1),
            sensor_classes.PULSE_PRESSURE,
            "Pulse Pressure",
            key_suffix,
            name_suffix,
        )
        self._publish_measurement_sensor(
            "mean_arterial_pressure_estimated",
            "mmHg",
            round(estimated_map, 1),
            sensor_classes.MEAN_ARTERIAL_PRESSURE_ESTIMATED,
            "Estimated MAP",
            key_suffix,
            name_suffix,
        )
        self._publish_measurement_sensor(
            "blood_pressure_category",
            None,
            self._classify_blood_pressure_category(float(sys_val), float(dia_val)),
            sensor_classes.BLOOD_PRESSURE_CATEGORY,
            "BP Category (ACC/AHA)",
            key_suffix,
            name_suffix,
        )

    def _publish_shock_index(
        self,
        sys_val: Any,
        bpm_val: Any,
        key_suffix: str,
        name_suffix: str,
        sensor_classes: Any,
    ) -> None:
        """Publish shock index only when denominator is valid."""
        if not (
            isinstance(sys_val, (int, float))
            and sys_val > 0
            and isinstance(bpm_val, (int, float))
        ):
            return
        shock_index = float(bpm_val) / float(sys_val)
        self._publish_measurement_sensor(
            "shock_index",
            "ratio",
            round(shock_index, 2),
            sensor_classes.SHOCK_INDEX,
            "Shock Index",
            key_suffix,
            name_suffix,
        )

    def _publish_rate_pressure_product(
        self,
        sys_val: Any,
        bpm_val: Any,
        key_suffix: str,
        name_suffix: str,
        sensor_classes: Any,
    ) -> None:
        """Publish RPP from systolic and pulse."""
        if not (
            isinstance(sys_val, (int, float))
            and isinstance(bpm_val, (int, float))
        ):
            return
        rpp = float(sys_val) * float(bpm_val)
        self._publish_measurement_sensor(
            "rate_pressure_product",
            "mmHg*bpm",
            round(rpp, 1),
            sensor_classes.RATE_PRESSURE_PRODUCT,
            "Rate Pressure Product",
            key_suffix,
            name_suffix,
        )

    def _publish_measurement_timestamp(
        self, record: dict[str, Any], key_suffix: str, name_suffix: str
    ) -> None:
        """Publish measurement timestamp when present."""
        measured_at = record.get("datetime")
        if measured_at is None:
            return
        measured_at = self._ensure_aware_datetime(measured_at)
        self._publish_measurement_sensor(
            "measurement_timestamp",
            None,
            measured_at,
            SensorDeviceClass.TIMESTAMP,
            "Measured At",
            key_suffix,
            name_suffix,
        )

    def _build_record_signature(self, record: dict[str, Any]) -> tuple[Any, ...]:
        """Build a compact record signature for new-vs-stale detection."""
        return (
            record.get("datetime"),
            record.get("user"),
            record.get("sys"),
            record.get("dia"),
            record.get("bpm"),
        )

    def _update_measurement_sensors(
        self, record: dict[str, Any], *, user: int | None = None, multi_user: bool = False
    ) -> None:
        """Publish measurement-derived sensors for one record."""
        from .const import ExtendedSensorDeviceClass

        key_suffix, name_suffix = self._measurement_user_suffixes(user, multi_user)
        sys_val, dia_val, bpm_val = self._publish_primary_measurements(
            record,
            key_suffix,
            name_suffix,
            ExtendedSensorDeviceClass,
        )
        self._publish_pressure_derived_metrics(
            sys_val,
            dia_val,
            key_suffix,
            name_suffix,
            ExtendedSensorDeviceClass,
        )
        self._publish_shock_index(
            sys_val,
            bpm_val,
            key_suffix,
            name_suffix,
            ExtendedSensorDeviceClass,
        )
        self._publish_rate_pressure_product(
            sys_val,
            bpm_val,
            key_suffix,
            name_suffix,
            ExtendedSensorDeviceClass,
        )
        self._publish_measurement_timestamp(record, key_suffix, name_suffix)

        user_id = f"user_{user}" if multi_user else "user_1"
        if user_id not in self.omron_extra_attributes:
            self.omron_extra_attributes[user_id] = {}
            
        if 'measurement_type' in record:
            self.omron_extra_attributes[user_id]['measurement_type'] = record['measurement_type']
        if 'truread_details' in record:
            self.omron_extra_attributes[user_id]['truread_details'] = record['truread_details']
        else:
            self.omron_extra_attributes[user_id].pop('truread_details', None)

        # Merge EEPROM-parsed ihb/mov into status_flags so both BLS and EEPROM
        # paths produce binary sensors for irregular pulse and body movement.
        status_flags = dict(record.get("status_flags") or {})
        if "ihb" in record and "irregular_pulse" not in status_flags:
            status_flags["irregular_pulse"] = bool(record["ihb"])
        if "mov" in record and "body_movement" not in status_flags:
            status_flags["body_movement"] = bool(record["mov"])
        if "cuff" in record and "cuff_fit" not in status_flags:
            status_flags["cuff_fit"] = not bool(record["cuff"])
        if "pos" in record and "improper_position" not in status_flags:
            status_flags["improper_position"] = bool(record["pos"])

        if status_flags:
            from .const import ExtendedBinarySensorDeviceClass
            for flag_key, class_name in [
                ("body_movement", ExtendedBinarySensorDeviceClass.BODY_MOVEMENT),
                ("cuff_fit", ExtendedBinarySensorDeviceClass.CUFF_FIT),
                ("irregular_pulse", ExtendedBinarySensorDeviceClass.IRREGULAR_PULSE),
                ("improper_position", ExtendedBinarySensorDeviceClass.IMPROPER_POSITION),
            ]:
                if flag_key in status_flags:
                    self.update_binary_sensor(
                        f"{flag_key}{key_suffix}",
                        status_flags[flag_key],
                        class_name,
                        f"{flag_key.replace('_', ' ').title()}{name_suffix}",
                    )


    def _ensure_aware_datetime(self, value: Any) -> Any:
        """Convert naive datetime to timezone-aware datetime for HA timestamp sensors."""
        if not isinstance(value, dt.datetime):
            return value
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            return value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return value

    @staticmethod
    def _classify_blood_pressure_category(sys_val: float, dia_val: float) -> str:
        """Classify blood pressure category using ACC/AHA 2017 thresholds."""
        if sys_val > 180 or dia_val > 120:
            return "Hypertensive Crisis"
        if sys_val >= 140 or dia_val >= 90:
            return "Hypertension Stage 2"
        if sys_val >= 130 or dia_val >= 80:
            return "Hypertension Stage 1"
        if sys_val >= 120 and dia_val < 80:
            return "Elevated"
        return "Normal"

    @staticmethod
    def _decode_sfloat_le(raw: bytes) -> float:
        """Decode IEEE-11073 16-bit SFLOAT (little-endian)."""
        if len(raw) != 2:
            raise ValueError("SFLOAT requires 2 bytes")
        val = int.from_bytes(raw, "little", signed=False)
        mantissa = val & 0x0FFF
        exponent = (val >> 12) & 0x0F
        if mantissa >= 0x0800:
            mantissa -= 0x1000
        if exponent >= 0x0008:
            exponent -= 0x0010
        return float(mantissa) * (10.0 ** exponent)

    def _parse_bp_measurement(self, payload: bytes) -> dict[str, Any] | None:
        """Parse BLE Blood Pressure Measurement characteristic (0x2A35)."""
        if not payload or len(payload) < 7:
            return None
        flags = payload[0]
        idx = 1
        unit_kpa = bool(flags & 0x01)
        has_timestamp = bool(flags & 0x02)
        has_pulse = bool(flags & 0x04)
        has_user_id = bool(flags & 0x08)
        has_status = bool(flags & 0x10)

        sys_val = self._decode_sfloat_le(payload[idx:idx + 2]); idx += 2
        dia_val = self._decode_sfloat_le(payload[idx:idx + 2]); idx += 2
        _ = self._decode_sfloat_le(payload[idx:idx + 2]); idx += 2  # MAP

        if unit_kpa:
            # Convert kPa to mmHg for HA entities.
            sys_mmhg = int(round(sys_val * 7.50062))
            dia_mmhg = int(round(dia_val * 7.50062))
        else:
            sys_mmhg = int(round(sys_val))
            dia_mmhg = int(round(dia_val))

        measured_dt: dt.datetime | None = None
        if has_timestamp and len(payload) >= idx + 7:
            year = int.from_bytes(payload[idx:idx + 2], "little")
            month = payload[idx + 2]
            day = payload[idx + 3]
            hour = payload[idx + 4]
            minute = payload[idx + 5]
            second = payload[idx + 6]
            idx += 7
            try:
                measured_dt = dt.datetime(year, month, day, hour, minute, second)
            except ValueError:
                measured_dt = None

        pulse: int | None = None
        if has_pulse and len(payload) >= idx + 2:
            pulse = int(round(self._decode_sfloat_le(payload[idx:idx + 2])))
            idx += 2

        if has_user_id and len(payload) > idx:
            idx += 1
        
        status_flags = {}
        if has_status and len(payload) >= idx + 2:
            status_val = int.from_bytes(payload[idx:idx + 2], "little")
            status_flags["body_movement"] = bool(status_val & 0x01)
            status_flags["cuff_fit"] = bool(status_val & 0x02)
            status_flags["irregular_pulse"] = bool(status_val & 0x04)
            status_flags["improper_position"] = bool(status_val & 0x20)
            idx += 2

        return {
            "sys": sys_mmhg,
            "dia": dia_mmhg,
            "bpm": pulse,
            "datetime": measured_dt,
            "status_flags": status_flags,
        }

    async def _read_latest_via_bls_racp(self, client: BleakClient) -> dict[str, Any] | None:
        """Request latest BP measurement via BLS RACP and parse 0x2A35 notification."""
        meas_char = client.services.get_characteristic(BP_MEASUREMENT_CHAR_UUID)
        racp_char = client.services.get_characteristic(BP_RACP_CHAR_UUID)
        if meas_char is None or racp_char is None:
            if not self._bls_racp_unavailable_logged:
                _LOGGER.debug(
                    "BLS RACP path unavailable: missing characteristics "
                    "(2A35=%s 2A52=%s)",
                    meas_char is not None,
                    racp_char is not None,
                )
                self._bls_racp_unavailable_logged = True
            return None

        measurement_future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        racp_done = asyncio.Event()

        def _meas_cb(_: Any, data: bytearray) -> None:
            if not measurement_future.done() and data:
                measurement_future.set_result(bytes(data))

        def _racp_cb(_: Any, data: bytearray) -> None:
            # Response code or procedure-complete indication.
            if data:
                racp_done.set()

        try:
            await client.start_notify(BP_MEASUREMENT_CHAR_UUID, _meas_cb)
            await client.start_notify(BP_RACP_CHAR_UUID, _racp_cb)
            await asyncio.sleep(0.5)
            # RACP: Report Stored Records (0x01), operator Last Record (0x06)
            await client.write_gatt_char(BP_RACP_CHAR_UUID, b"\x01\x06", response=True)
            raw = await asyncio.wait_for(measurement_future, timeout=3.0)
            try:
                await asyncio.wait_for(racp_done.wait(), timeout=1.5)
            except asyncio.TimeoutError:
                pass
            return self._parse_bp_measurement(raw)
        except Exception as exc:
            if not self._bls_racp_unavailable_logged:
                self._bls_racp_unavailable_logged = True
            _LOGGER.debug("BLS RACP latest read failed: %s", exc)
            return None
        finally:
            try:
                await client.stop_notify(BP_MEASUREMENT_CHAR_UUID)
            except Exception:
                pass
            try:
                await client.stop_notify(BP_RACP_CHAR_UUID)
            except Exception:
                pass

    def _setup_device_info(self, service_info: BluetoothServiceInfoBleak) -> None:
        """Set up device metadata from advertisement."""
        model = self._device_config.model
        manufacturer = "Omron"
        normalized_address = service_info.address.replace(":", "")
        identifier = normalized_address[-4:] if len(normalized_address) >= 4 else normalized_address

        self.set_title(f"{manufacturer} BPM {identifier}")
        self.set_device_name(f"{model} {identifier}")
        self.set_device_type("Blood Pressure Monitor")
        self.set_device_manufacturer(manufacturer)
        self.pending = False

    async def _poll_device_readout(
        self,
        session: OmronDeviceSession,
        client: BleakClient,
        ble_device: BLEDevice,
        *,
        memory_session_active: bool,
    ) -> None:
        """Time sync, record fetch, and device info reads for one poll cycle."""
        try:
            if memory_session_active:
                if self._device_config.supports_eeprom_time_sync:
                    await async_sync_eeprom_time(
                        client,
                        self._device_model,
                        self._device_config,
                        session,
                    )
            elif not self._device_config.supports_eeprom_time_sync:
                await self._async_sync_current_time_with_client(
                    client, ble_device.address, session
                )
            else:
                _LOGGER.debug(
                    "Skipping EEPROM time sync for %s: memory session not opened",
                    ble_device.address,
                )
        except Exception as exc:
            _LOGGER.warning(
                "Time sync failed during poll for %s: %s", ble_device.address, exc
            )

        multi_user_mode = self._device_config.num_users > 1
        record: dict[str, Any] | None = None
        latest_by_user: dict[int, dict[str, Any]] = {}
        if multi_user_mode:
            latest_by_user = await self._driver.get_latest_records_per_user(session)
        else:
            record = await self._driver.get_latest_record(session)
            live_record: dict[str, Any] | None = None
            live_record = await self._read_latest_via_bls_racp(client)
            if not self._bp_char_unavailable:
                try:
                    bp_raw = await client.read_gatt_char(BP_MEASUREMENT_CHAR_UUID)
                    if bp_raw:
                        if live_record is None:
                            live_record = self._parse_bp_measurement(bytes(bp_raw))
                except Exception as exc:
                    if "Read not permitted" in str(exc):
                        self._bp_char_unavailable = True
                        _LOGGER.debug(
                            "BP measurement char 0x2A35 read not permitted on %s; "
                            "disabling live BLS read path",
                            ble_device.address,
                        )
                    else:
                        _LOGGER.debug(
                            "Read BP measurement char 0x2A35 failed for %s: %s",
                            ble_device.address,
                            exc,
                        )
                    live_record = None

            if live_record and isinstance(live_record.get("sys"), int) and isinstance(live_record.get("dia"), int):
                eeprom_dt = record.get("datetime") if record else None
                live_dt = live_record.get("datetime")
                use_live = False
                if record is None:
                    use_live = True
                elif isinstance(live_dt, dt.datetime) and (
                    not isinstance(eeprom_dt, dt.datetime) or live_dt > (eeprom_dt + dt.timedelta(minutes=1))
                ):
                    use_live = True
                elif (
                    not isinstance(live_dt, dt.datetime)
                    and isinstance(record.get("sys"), int)
                    and isinstance(record.get("dia"), int)
                    and (
                        int(live_record["sys"]) != int(record.get("sys"))
                        or int(live_record["dia"]) != int(record.get("dia"))
                    )
                ):
                    use_live = True
                if use_live:
                    merged = dict(record or {})
                    merged["sys"] = live_record["sys"]
                    merged["dia"] = live_record["dia"]
                    if isinstance(live_record.get("bpm"), int):
                        merged["bpm"] = live_record["bpm"]
                    if isinstance(live_dt, dt.datetime):
                        merged["datetime"] = live_dt
                    if "user" not in merged:
                        merged["user"] = 1
                    record = merged

        if multi_user_mode:
            if latest_by_user:
                for user in sorted(latest_by_user):
                    user_record = latest_by_user[user]
                    _LOGGER.debug(
                        "User-specific latest selected: user=%d datetime=%s sys=%s dia=%s bpm=%s",
                        user,
                        user_record.get("datetime"),
                        user_record.get("sys"),
                        user_record.get("dia"),
                        user_record.get("bpm"),
                    )
                    self._update_measurement_sensors(
                        user_record,
                        user=user,
                        multi_user=True,
                    )
                    signature = self._build_record_signature(user_record)
                    previous = self._last_record_signatures_by_user.get(user)
                    if signature != previous:
                        self._last_record_signatures_by_user[user] = signature
        elif record:
            _LOGGER.debug(
                "Latest selected: datetime=%s sys=%s dia=%s bpm=%s",
                record.get("datetime"),
                record.get("sys"),
                record.get("dia"),
                record.get("bpm"),
            )
            self._update_measurement_sensors(record)
            signature = self._build_record_signature(record)
            if signature != self._last_record_signature:
                self._last_record_signature = signature

        absolute_latest_record = None
        if multi_user_mode and latest_by_user:
            absolute_latest_record = max(
                latest_by_user.values(),
                key=lambda r: self._ensure_aware_datetime(r.get("datetime"))
                if isinstance(r.get("datetime"), dt.datetime)
                else dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            )
        elif record:
            absolute_latest_record = record

        if absolute_latest_record and "battery" in absolute_latest_record:
            is_low_battery = bool(absolute_latest_record["battery"])
            _LOGGER.debug(
                "Extracted device-level low battery flag from measurements: %s",
                is_low_battery,
            )
            self.update_binary_sensor(
                "battery",
                is_low_battery,
                BinarySensorDeviceClass.BATTERY,
                "Battery",
            )

        try:
            char_bat = client.services.get_characteristic(BATTERY_LEVEL_UUID)
            bat_bytes = None
            if char_bat:
                _LOGGER.debug(
                    "Found battery characteristic in cached services for %s",
                    ble_device.address,
                )
                bat_bytes = await client.read_gatt_char(char_bat)
            else:
                _LOGGER.debug(
                    "Battery char not in cached services, falling back to UUID for %s",
                    ble_device.address,
                )
                bat_bytes = await client.read_gatt_char(BATTERY_LEVEL_UUID)

            if bat_bytes:
                bat_level = int(bat_bytes[0])
                _LOGGER.debug(
                    "Battery level byte received for %s: %s (Parsed: %d%%)",
                    ble_device.address,
                    bat_bytes.hex(),
                    bat_level,
                )
        except Exception as exc:
            _LOGGER.debug("Failed to read Battery Level: %s", exc)

        try:
            char_fw = client.services.get_characteristic(FIRMWARE_REVISION_UUID)
            if char_fw:
                fw_bytes = await client.read_gatt_char(char_fw)
                if fw_bytes:
                    fw_rev = fw_bytes.decode("utf-8").strip(" \x00")
                    self.set_device_sw_version(fw_rev)
        except Exception as exc:
            _LOGGER.debug("Failed to read Firmware Revision: %s", exc)

        try:
            char_hw = client.services.get_characteristic(HARDWARE_REVISION_UUID)
            if char_hw:
                hw_bytes = await client.read_gatt_char(char_hw)
                if hw_bytes:
                    hw_rev = hw_bytes.decode("utf-8").strip(" \x00")
                    self.set_device_hw_version(hw_rev)
        except Exception as exc:
            _LOGGER.debug("Failed to read Hardware Revision: %s", exc)

        try:
            char_mfg = client.services.get_characteristic(MANUFACTURER_NAME_UUID)
            if char_mfg:
                mfg_bytes = await client.read_gatt_char(char_mfg)
                if mfg_bytes:
                    mfg_name = mfg_bytes.decode("utf-8").strip(" \x00")
                    self.set_device_manufacturer(mfg_name)
        except Exception as exc:
            _LOGGER.debug("Failed to read Manufacturer Name: %s", exc)

        try:
            char_model = client.services.get_characteristic(MODEL_NUMBER_UUID)
            if char_model:
                await client.read_gatt_char(char_model)
        except Exception as exc:
            _LOGGER.debug("Failed to read Model Number: %s", exc)

    async def async_poll(
        self, ble_device: BLEDevice, preconnected_session: OmronDeviceSession | None = None
    ) -> SensorUpdate:
        """Poll the device to retrieve measurement records via GATT connection.

        If ``preconnected_session`` is supplied and still connected, the poll
        adopts that setup session (unlock + memory session state preserved) so
        pairing, time sync, and the initial read share one connection.
        """
        async with self._poll_guard:
            self._events_updates.clear()

            try:
                if (
                    preconnected_session is not None
                    and preconnected_session.is_connected
                ):
                    session = preconnected_session
                    session.reclaim_ownership()
                else:
                    session = OmronDeviceSession(ble_device, self._device_config)
                async with session:
                    client = session.client

                    if not await session.verify_parent_service():
                        prof = resolve_profile_model_id(self._device_model)
                        stack_label = (
                            "modern" if self._device_config.is_modern_stack else "classic"
                        )
                        _LOGGER.error(
                            "Required service %s not on %s; model=%s profile=%s expected_stack=%s",
                            self._device_config.parent_service_uuid,
                            ble_device.address,
                            self._device_model,
                            prof,
                            stack_label,
                        )
                        return self._finish_update()

                    if self.last_service_info and not self._device_config.is_advertisement_compatible(
                        self.last_service_info.service_uuids
                    ):
                        prof = resolve_profile_model_id(self._device_model)
                        stack_label = (
                            "modern" if self._device_config.is_modern_stack else "classic"
                        )
                        _LOGGER.debug(
                            "Configured model %s (profile %s) may not match advertised service family; "
                            "advertised=%s expected_stack=%s",
                            self._device_model,
                            prof,
                            self.last_service_info.service_uuids,
                            stack_label,
                        )

                    _LOGGER.debug(
                        "Poll start: model=%s addr=%s endian=%s parser=%s "
                        "users=%d rec_size=%d time_sync=%s",
                        self._device_config.model,
                        ble_device.address,
                        self._device_config.endianness,
                        self._device_config.record_parser,
                        self._device_config.num_users,
                        self._device_config.record_byte_size,
                        self._device_config.resolved_time_sync_layout()
                        if self._device_config.supports_eeprom_time_sync
                        else "none",
                    )

                    needs_memory = (
                        self._device_config.is_classic_stack
                        or self._device_config.supports_eeprom_time_sync
                    )
                    if needs_memory:
                        if session.memory_session_active:
                            _LOGGER.debug(
                                "Reusing setup memory session for %s",
                                ble_device.address,
                            )
                            await self._poll_device_readout(
                                session,
                                client,
                                ble_device,
                                memory_session_active=True,
                            )
                        else:
                            memory_session_active = False
                            last_session_exc: BaseException | None = None
                            for session_attempt in range(3):
                                try:
                                    pair_first = (
                                        session_attempt == 0
                                        and self._device_config.host_pairing_mode
                                        == HostPairingMode.CUSTOM_KEY
                                        and self.pairing_mode
                                    )
                                    async with session.memory_session_after_unlock(
                                        pair_first=pair_first
                                    ):
                                        memory_session_active = True
                                        await self._poll_device_readout(
                                            session,
                                            client,
                                            ble_device,
                                            memory_session_active=True,
                                        )
                                    break
                                except BaseException as exc:
                                    last_session_exc = exc
                                    _LOGGER.debug(
                                        "Memory session open attempt %d/3 failed: %s",
                                        session_attempt + 1,
                                        exc,
                                    )
                                    if session_attempt < 2:
                                        try:
                                            await session.reset_session_state()
                                        except Exception as reset_exc:
                                            _LOGGER.debug(
                                                "Session state reset failed (ignored): %s",
                                                reset_exc,
                                            )
                                        await session.refresh_services()
                                        await asyncio.sleep(0.5)
                            if not memory_session_active and last_session_exc is not None:
                                if (
                                    self._device_config.host_pairing_mode
                                    == HostPairingMode.OS_BONDING
                                ):
                                    _LOGGER.warning(
                                        "Memory session failed for OS-bonding device %s "
                                        "(model=%s): %s. Ensure OS-level BLE bonding is "
                                        "complete — remove and re-add the device if this "
                                        "error persists.",
                                        ble_device.address,
                                        self._device_config.model,
                                        last_session_exc,
                                    )
                                else:
                                    _LOGGER.debug(
                                        "Could not open memory session globally in poll "
                                        "after retries: %s",
                                        last_session_exc,
                                    )
                                try:
                                    async with session.memory_session_after_unlock():
                                        await self._poll_device_readout(
                                            session,
                                            client,
                                            ble_device,
                                            memory_session_active=True,
                                        )
                                except BaseException as fallback_exc:
                                    _LOGGER.debug(
                                        "Fallback memory session readout failed: %s",
                                        fallback_exc,
                                    )
                    else:
                        await self._poll_device_readout(
                            session,
                            client,
                            ble_device,
                            memory_session_active=False,
                        )

            except ConnectionError as exc:
                # Expected when the cuff is off, out of range, or the link drops mid-poll.
                prof = resolve_profile_model_id(self._device_model)
                _LOGGER.warning(
                    "Poll interrupted (disconnected) model=%s profile=%s address=%s: %s",
                    self._device_model,
                    prof,
                    ble_device.address,
                    exc,
                )
            except Exception as exc:
                prof = resolve_profile_model_id(self._device_model)
                _LOGGER.error(
                    "Poll failed model=%s profile=%s address=%s: %s",
                    self._device_model,
                    prof,
                    ble_device.address,
                    exc,
                    exc_info=exc,
                )

            return self._finish_update()

    async def async_retry_pairing(self, ble_device: BLEDevice) -> None:
        """Connect to the device and retry pairing/bonding (setup-like flow)."""
        async with OmronDeviceSession(ble_device, self._device_config) as session:
            if not await session.verify_parent_service():
                raise ConnectionError(
                    f"Required service {self._device_config.parent_service_uuid} "
                    f"not found on device {ble_device.address}"
                )
            await session.pair()
            await session.refresh_services()
            await async_sync_device_time(
                session.client,
                self._device_model,
                self._device_config,
                session,
            )

    async def async_sync_time(self, ble_device: BLEDevice) -> None:
        """Connect to the device and synchronize time only."""
        async with OmronDeviceSession(ble_device, self._device_config) as session:
            await self._async_sync_current_time_with_client(
                session.client, ble_device.address, session
            )

    async def _async_sync_current_time_with_client(
        self, client: BleakClient, address: str,
        transport: OmronDeviceSession | None = None,
    ) -> bool:
        """Sync current local time via CTS or EEPROM fallback."""
        return await async_sync_device_time(
            client, self._device_model, self._device_config, transport
        )
