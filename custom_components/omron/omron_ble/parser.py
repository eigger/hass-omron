"""Parser for Omron BLE blood pressure monitors.

Handles device detection from BLE advertisements and active polling
for measurement data via GATT connection.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection

from bluetooth_sensor_state_data import BluetoothData
from home_assistant_bluetooth import BluetoothServiceInfoBleak
from sensor_state_data import (
    SensorLibrary,
    SensorUpdate,
    SensorDeviceClass,
    Units,
)
from homeassistant.util import dt as dt_util

from .const import TIMEOUT_5MIN
from .devices import (
    DISCOVERABLE_PARENT_SERVICE_UUIDS,
    DeviceConfig,
    DEFAULT_DEVICE_MODEL,
    MODEL_VARIANT_MAP,
    get_device_config,
    resolve_profile_model_id,
)
from .omron_driver import GattTransport, OmronDeviceDriver, _bleak_refresh_services

_LOGGER = logging.getLogger(__name__)
CTS_CHARACTERISTIC_UUID = "00002a2b-0000-1000-8000-00805f9b34fb"
BP_MEASUREMENT_CHAR_UUID = "00002a35-0000-1000-8000-00805f9b34fb"
BP_RACP_CHAR_UUID = "00002a52-0000-1000-8000-00805f9b34fb"
VERBOSE_BLS_LOG = os.getenv("OMRON_VERBOSE_BLS_LOG", "0") == "1"
# Bluetooth SIG company identifier for Omron Healthcare (matches manifest.json bluetooth manufacturer_id)
OMRON_MANUFACTURER_ID = 526





class OmronBluetoothDeviceData(BluetoothData):
    """Data handler for Omron BLE blood pressure monitors."""

    def __init__(self, device_model: str = DEFAULT_DEVICE_MODEL) -> None:
        super().__init__()
        self.last_service_info: BluetoothServiceInfoBleak | None = None
        self.pending = True
        self._device_model = device_model
        self._device_config: DeviceConfig = get_device_config(device_model)
        self._driver = OmronDeviceDriver(self._device_config)
        self._last_record_signature: tuple[Any, ...] | None = None
        self._last_record_signatures_by_user: dict[int, tuple[Any, ...]] = {}
        self._bp_char_unavailable = False
        self._bls_racp_unavailable_logged = False
        self._unvalidated_variant_warning_logged = False

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
        self._unvalidated_variant_warning_logged = False

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
        _LOGGER.debug("service_info: %s", service_info)

        # Check if any known Omron service UUID is present
        for uuid in DISCOVERABLE_PARENT_SERVICE_UUIDS:
            if uuid in service_info.service_uuids:
                self._setup_device_info(service_info)
                self.last_service_info = service_info
                return

        # Omron manufacturer company id (manifest manufacturer_id 526)
        md = getattr(service_info, "manufacturer_data", None) or {}
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

        key_suffix = f"_user{user}" if multi_user and user is not None else ""
        name_suffix = f" (User {user})" if multi_user and user is not None else ""

        sys_val = record.get("sys")
        dia_val = record.get("dia")
        bpm_val = record.get("bpm")

        self.update_sensor(
            f"blood_pressure_systolic{key_suffix}",
            "mmHg",
            record["sys"],
            device_class=ExtendedSensorDeviceClass.BLOOD_PRESSURE_SYSTOLIC,
            name=f"Systolic{name_suffix}",
        )
        self.update_sensor(
            f"blood_pressure_diastolic{key_suffix}",
            "mmHg",
            record["dia"],
            device_class=ExtendedSensorDeviceClass.BLOOD_PRESSURE_DIASTOLIC,
            name=f"Diastolic{name_suffix}",
        )
        self.update_sensor(
            f"heart_rate{key_suffix}",
            "bpm",
            record["bpm"],
            device_class=ExtendedSensorDeviceClass.HEART_RATE,
            name=f"Pulse{name_suffix}",
        )

        if (
            isinstance(sys_val, (int, float))
            and isinstance(dia_val, (int, float))
            and sys_val > dia_val
        ):
            pulse_pressure = float(sys_val - dia_val)
            estimated_map = float(dia_val + (pulse_pressure / 3))
            self.update_sensor(
                f"pulse_pressure{key_suffix}",
                "mmHg",
                round(pulse_pressure, 1),
                device_class=ExtendedSensorDeviceClass.PULSE_PRESSURE,
                name=f"Pulse Pressure{name_suffix}",
            )
            self.update_sensor(
                f"mean_arterial_pressure_estimated{key_suffix}",
                "mmHg",
                round(estimated_map, 1),
                device_class=ExtendedSensorDeviceClass.MEAN_ARTERIAL_PRESSURE_ESTIMATED,
                name=f"Estimated MAP{name_suffix}",
            )
            self.update_sensor(
                f"blood_pressure_category{key_suffix}",
                None,
                self._classify_blood_pressure_category(float(sys_val), float(dia_val)),
                device_class=ExtendedSensorDeviceClass.BLOOD_PRESSURE_CATEGORY,
                name=f"BP Category (ACC/AHA){name_suffix}",
            )

        if (
            isinstance(sys_val, (int, float))
            and sys_val > 0
            and isinstance(bpm_val, (int, float))
        ):
            shock_index = float(bpm_val) / float(sys_val)
            self.update_sensor(
                f"shock_index{key_suffix}",
                "ratio",
                round(shock_index, 2),
                device_class=ExtendedSensorDeviceClass.SHOCK_INDEX,
                name=f"Shock Index{name_suffix}",
            )

        if (
            isinstance(sys_val, (int, float))
            and isinstance(bpm_val, (int, float))
        ):
            rate_pressure_product = float(sys_val) * float(bpm_val)
            self.update_sensor(
                f"rate_pressure_product{key_suffix}",
                "mmHg*bpm",
                round(rate_pressure_product, 1),
                device_class=ExtendedSensorDeviceClass.RATE_PRESSURE_PRODUCT,
                name=f"Rate Pressure Product{name_suffix}",
            )

        measured_at = record.get("datetime")
        if measured_at is not None:
            measured_at = self._ensure_aware_datetime(measured_at)
            self.update_sensor(
                f"measurement_timestamp{key_suffix}",
                None,
                measured_at,
                device_class=SensorDeviceClass.TIMESTAMP,
                name=f"Measured At{name_suffix}",
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
        if has_status and len(payload) >= idx + 2:
            idx += 2

        return {
            "sys": sys_mmhg,
            "dia": dia_mmhg,
            "bpm": pulse,
            "datetime": measured_dt,
        }

    async def _read_latest_via_bls_racp(self, client: BleakClient) -> dict[str, Any] | None:
        """Request latest BP measurement via BLS RACP and parse 0x2A35 notification."""
        meas_char = client.services.get_characteristic(BP_MEASUREMENT_CHAR_UUID)
        racp_char = client.services.get_characteristic(BP_RACP_CHAR_UUID)
        if meas_char is None or racp_char is None:
            if not self._bls_racp_unavailable_logged:
                _LOGGER.info(
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
            # RACP: Report Stored Records (0x01), operator Last Record (0x06)
            await client.write_gatt_char(BP_RACP_CHAR_UUID, b"\x01\x06", response=True)
            raw = await asyncio.wait_for(measurement_future, timeout=3.0)
            if VERBOSE_BLS_LOG:
                _LOGGER.debug("BLS RACP latest raw 0x2A35=%s", raw.hex())
            try:
                await asyncio.wait_for(racp_done.wait(), timeout=1.5)
            except asyncio.TimeoutError:
                pass
            return self._parse_bp_measurement(raw)
        except Exception as exc:
            if not self._bls_racp_unavailable_logged:
                _LOGGER.info("BLS RACP latest read failed: %s", exc)
                self._bls_racp_unavailable_logged = True
            elif VERBOSE_BLS_LOG:
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

    async def async_poll(self, ble_device: BLEDevice) -> SensorUpdate:
        """Poll the device to retrieve measurement records via GATT connection."""
        _LOGGER.debug("Polling device: %s (model: %s)", ble_device.address, self._device_model)
        variant_entry = MODEL_VARIANT_MAP.get(self._device_model)
        if variant_entry:
            profile_key, variant = variant_entry
            _LOGGER.debug(
                "Catalog variant: %s -> profile %s (unverified=%s, reason=%s)",
                self._device_model,
                profile_key,
                variant.unverified,
                variant.reason,
            )
            if variant.unverified and not self._unvalidated_variant_warning_logged:
                _LOGGER.warning(
                    "Unverified catalog variant: %s -> profile %s (reason=%s). "
                    "If sync fails or values look wrong, choose another registry profile.",
                    self._device_model,
                    profile_key,
                    variant.reason,
                )
                self._unvalidated_variant_warning_logged = True

        self._events_updates.clear()

        client: BleakClient | None = None
        poll_status = "poll_failed"
        try:
            client = await establish_connection(
                BleakClient, ble_device, ble_device.address
            )
            # Ensure Bleak service cache is populated before reading client.services.
            await _bleak_refresh_services(client)

            # Some OS-bonding-only models require an encrypted link on each session
            # to return meaningful measurement memory data.
            if self._device_config.supports_os_bonding_only:
                try:
                    await client.pair()
                    await _bleak_refresh_services(client)
                    _LOGGER.debug(
                        "Ensured OS-level bonding before poll for model=%s",
                        self._device_model,
                    )
                except TypeError:
                    await client.pair(protection_level=2)
                    await _bleak_refresh_services(client)
                    _LOGGER.debug(
                        "Ensured OS-level bonding (protection_level=2) before poll for model=%s",
                        self._device_model,
                    )
                except Exception as exc:
                    _LOGGER.debug(
                        "OS-level bonding check before poll failed/ignored for model=%s: %s",
                        self._device_model,
                        exc,
                    )

            # Verify the device has expected services
            parent_uuid = self._device_config.parent_service_uuid
            service_found = False
            for attempt in range(5):
                try:
                    if parent_uuid in [s.uuid for s in client.services]:
                        service_found = True
                        break
                except Exception as exc:
                    _LOGGER.debug(
                        "Services not ready during poll (%d/5) for %s: %s",
                        attempt + 1,
                        ble_device.address,
                        exc,
                    )
                await _bleak_refresh_services(client)
                await asyncio.sleep(0.25)

            if not service_found:
                prof = resolve_profile_model_id(self._device_model)
                variant_entry = MODEL_VARIANT_MAP.get(self._device_model)
                _LOGGER.error(
                    "Required service %s not found on device %s",
                    parent_uuid,
                    ble_device.address,
                )
                _LOGGER.error(
                    "poll_failed: model/service mismatch (model=%s profile=%s "
                    "variant_unverified=%s variant_reason=%s expected_stack=%s)",
                    self._device_model,
                    prof,
                    variant_entry[1].unverified if variant_entry else False,
                    variant_entry[1].reason if variant_entry else None,
                    self._device_config.parent_service_stack(),
                )
                return self._finish_update()

            if self.last_service_info and not self._device_config.is_advertisement_compatible(
                self.last_service_info.service_uuids
            ):
                prof = resolve_profile_model_id(self._device_model)
                _LOGGER.warning(
                    "Configured model %s (profile %s) may not match advertised service family. "
                    "advertised=%s expected_stack=%s",
                    self._device_model,
                    prof,
                    self.last_service_info.service_uuids,
                    self._device_config.parent_service_stack(),
                )

            try:
                services = client.services
                missing_rx = [
                    uuid for uuid in self._device_config.rx_channel_uuids
                    if services.get_characteristic(uuid) is None
                ]
                missing_tx = [
                    uuid for uuid in self._device_config.tx_channel_uuids
                    if services.get_characteristic(uuid) is None
                ]
            except Exception as exc:
                _LOGGER.debug(
                    "Skipping characteristic pre-check; services unavailable for %s: %s",
                    ble_device.address,
                    exc,
                )
                missing_rx = []
                missing_tx = []
            if missing_rx or missing_tx:
                prof = resolve_profile_model_id(self._device_model)
                _LOGGER.warning(
                    "Potential model/stack mismatch: missing expected characteristics "
                    "for model=%s profile=%s missing_rx=%s missing_tx=%s",
                    self._device_model,
                    prof,
                    missing_rx,
                    missing_tx,
                )
                _LOGGER.warning(
                    "Continuing poll despite missing characteristic pre-check; "
                    "driver command path will determine actual compatibility."
                )

            transport = GattTransport(client, self._device_config)
            multi_user_mode = self._device_config.num_users > 1
            record: dict[str, Any] | None = None
            latest_by_user: dict[int, dict[str, Any]] = {}
            if multi_user_mode:
                latest_by_user = await self._driver.get_latest_records_per_user(transport)
            else:
                record = await self._driver.get_latest_record(transport)
                live_record: dict[str, Any] | None = None
                # Preferred live path for BLS devices: request latest via RACP indications.
                live_record = await self._read_latest_via_bls_racp(client)
                if live_record and VERBOSE_BLS_LOG:
                    _LOGGER.debug("BLS RACP parsed latest: %s", live_record)
                if not self._bp_char_unavailable:
                    try:
                        bp_raw = await client.read_gatt_char(BP_MEASUREMENT_CHAR_UUID)
                        if bp_raw:
                            # Keep RACP result if present; otherwise use direct read result.
                            if live_record is None:
                                live_record = self._parse_bp_measurement(bytes(bp_raw))
                            if VERBOSE_BLS_LOG:
                                _LOGGER.debug(
                                    "Read BP measurement char 0x2A35: raw=%s parsed=%s",
                                    bytes(bp_raw).hex(),
                                    live_record,
                                )
                        elif VERBOSE_BLS_LOG:
                            _LOGGER.debug("Read BP measurement char 0x2A35: empty payload")
                    except Exception as exc:
                        if "Read not permitted" in str(exc):
                            self._bp_char_unavailable = True
                            _LOGGER.info(
                                "BP measurement char 0x2A35 read not permitted on %s; "
                                "disabling live BLS read path",
                                ble_device.address,
                            )
                        elif VERBOSE_BLS_LOG:
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
                        # Some devices expose recent measurement values in 0x2A35 without timestamp.
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
                        _LOGGER.info(
                            "Using live BP measurement characteristic over EEPROM "
                            "(sys=%s dia=%s bpm=%s datetime=%s)",
                            record.get("sys"),
                            record.get("dia"),
                            record.get("bpm"),
                            record.get("datetime"),
                        )

            if multi_user_mode:
                if latest_by_user:
                    has_new = False
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
                            has_new = True
                            self._last_record_signatures_by_user[user] = signature
                    poll_status = "new_measurement" if has_new else "no_new_valid_record"
                else:
                    poll_status = "no_new_valid_record"
                    _LOGGER.debug("No records found on device for any configured user")
            elif record:
                _LOGGER.info("Got record: %s", record)
                _LOGGER.debug(
                    "Latest measurement selected: datetime=%s user=%s sys=%s dia=%s bpm=%s",
                    record.get("datetime"),
                    record.get("user"),
                    record.get("sys"),
                    record.get("dia"),
                    record.get("bpm"),
                )
                self._update_measurement_sensors(record)
                signature = self._build_record_signature(record)
                if signature == self._last_record_signature:
                    poll_status = "no_new_valid_record"
                    _LOGGER.debug(
                        "no_new_valid_record: latest valid record unchanged (model=%s, sig=%s)",
                        self._device_model,
                        signature,
                    )
                else:
                    poll_status = "new_measurement"
                    _LOGGER.debug(
                        "new_measurement: latest valid record changed from %s to %s",
                        self._last_record_signature,
                        signature,
                    )
                    self._last_record_signature = signature
                _LOGGER.debug(
                    "Prepared sensor update payload for %s: %s",
                    ble_device.address,
                    {
                        "blood_pressure_systolic": record.get("sys"),
                        "blood_pressure_diastolic": record.get("dia"),
                        "heart_rate": record.get("bpm"),
                        "pulse_pressure": (
                            (record.get("sys") - record.get("dia"))
                            if isinstance(record.get("sys"), (int, float))
                            and isinstance(record.get("dia"), (int, float))
                            and record.get("sys") > record.get("dia")
                            else None
                        ),
                    },
                )
            else:
                poll_status = "no_new_valid_record"
                prof = resolve_profile_model_id(self._device_model)
                _LOGGER.debug("No records found on device")
                _LOGGER.debug(
                    "no_new_valid_record: no valid parsed records (model=%s profile=%s)",
                    self._device_model,
                    prof,
                )

        except Exception as exc:
            prof = resolve_profile_model_id(self._device_model)
            variant_entry = MODEL_VARIANT_MAP.get(self._device_model)
            _LOGGER.error(
                "Error polling device model=%s profile=%s address=%s: %s",
                self._device_model,
                prof,
                ble_device.address,
                exc,
                exc_info=exc,
            )
            _LOGGER.error(
                "poll_failed: exc_type=%s model=%s profile=%s variant_unverified=%s "
                "variant_reason=%s",
                type(exc).__name__,
                self._device_model,
                prof,
                variant_entry[1].unverified if variant_entry else False,
                variant_entry[1].reason if variant_entry else None,
            )
        finally:
            if client and client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            _LOGGER.debug("poll status for %s: %s", ble_device.address, poll_status)

        return self._finish_update()

    async def async_sync_current_time(self, ble_device: BLEDevice) -> bool:
        """Connect to device and sync current local time via CTS."""
        client: BleakClient | None = None
        try:
            client = await establish_connection(BleakClient, ble_device, ble_device.address)
            char = client.services.get_characteristic(CTS_CHARACTERISTIC_UUID)
            if char is None:
                _LOGGER.warning(
                    "CTS characteristic not found while syncing time (model=%s, address=%s)",
                    self._device_model,
                    ble_device.address,
                )
                return False

            now = dt.datetime.now().astimezone()
            payload = bytearray()
            payload += int(now.year).to_bytes(2, "little")
            payload += bytes(
                [
                    now.month,
                    now.day,
                    now.hour,
                    now.minute,
                    now.second,
                    now.isoweekday(),
                    0x00,  # Fractions256
                    0x01,  # Adjust reason: manual time update
                ]
            )
            await client.write_gatt_char(CTS_CHARACTERISTIC_UUID, payload, response=True)
            _LOGGER.info(
                "Synced current time via CTS for %s (%s): %s",
                self._device_model,
                ble_device.address,
                now.isoformat(timespec="seconds"),
            )
            return True
        finally:
            if client and client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass
