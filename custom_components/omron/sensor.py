"""Support for Omron sensors."""

from __future__ import annotations

from datetime import datetime

import datetime as dt
from typing import Any
from sensor_state_data import (
    DeviceKey,
    SensorDeviceClass as OmronSensorDeviceClass,
    SensorUpdate,
    Units,
)

from .omron_ble.const import (
    ExtendedSensorDeviceClass as OmronExtendedSensorDeviceClass,
)

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataUpdate,
    PassiveBluetoothProcessorEntity,
)
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .coordinator import OmronPassiveBluetoothDataProcessor
from .entity import OmronCoordinatorEntity
from .entity_helpers import (
    apply_translated_entity_name,
    device_key_entity_id_suffix,
    device_key_to_bluetooth_entity_key,
    hass_device_info_with_ble_connection,
    preserved_passive_unique_id,
)
from .types import OmronConfigEntry

# Values stored before measurement_type became a translation key.
_LEGACY_MEASUREMENT_TYPES = {
    "Single": "single",
    "TruRead Average": "truread_average",
}

# Poll-backed measurement sensors. RSSI is advertisement-only (see below).
SENSOR_DESCRIPTIONS = {
    # ---- Blood Pressure / Heart Rate (primary sensors) ----

    # Blood Pressure System (mmHg)
    (
        OmronExtendedSensorDeviceClass.BLOOD_PRESSURE_SYSTOLIC,
        "mmHg",
    ): SensorEntityDescription(
        key=f"{OmronExtendedSensorDeviceClass.BLOOD_PRESSURE_SYSTOLIC}_mmHg",
        native_unit_of_measurement="mmHg",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart-plus",
    ),
    (
        OmronExtendedSensorDeviceClass.BLOOD_PRESSURE_DIASTOLIC,
        "mmHg",
    ): SensorEntityDescription(
        key=f"{OmronExtendedSensorDeviceClass.BLOOD_PRESSURE_DIASTOLIC}_mmHg",
        native_unit_of_measurement="mmHg",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart-minus",
    ),

    # Heart Rate (beats per minute)
    (
        OmronExtendedSensorDeviceClass.HEART_RATE,
        "bpm",
    ): SensorEntityDescription(
        key=f"{OmronExtendedSensorDeviceClass.HEART_RATE}_bpm",
        device_class=SensorDeviceClass.HEART_RATE
        if hasattr(SensorDeviceClass, "HEART_RATE")
        else None,
        native_unit_of_measurement="bpm",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:pulse",
    ),
    (
        OmronExtendedSensorDeviceClass.PULSE_PRESSURE,
        "mmHg",
    ): SensorEntityDescription(
        key=f"{OmronExtendedSensorDeviceClass.PULSE_PRESSURE}_mmHg",
        native_unit_of_measurement="mmHg",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:chart-bell-curve-cumulative",
    ),
    (
        OmronExtendedSensorDeviceClass.MEAN_ARTERIAL_PRESSURE_ESTIMATED,
        "mmHg",
    ): SensorEntityDescription(
        key=f"{OmronExtendedSensorDeviceClass.MEAN_ARTERIAL_PRESSURE_ESTIMATED}_mmHg",
        native_unit_of_measurement="mmHg",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:waves-arrow-right",
    ),
    (
        OmronExtendedSensorDeviceClass.SHOCK_INDEX,
        "ratio",
    ): SensorEntityDescription(
        key=f"{OmronExtendedSensorDeviceClass.SHOCK_INDEX}_ratio",
        native_unit_of_measurement="ratio",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart-flash",
    ),
    (
        OmronExtendedSensorDeviceClass.RATE_PRESSURE_PRODUCT,
        "mmHg*bpm",
    ): SensorEntityDescription(
        key=f"{OmronExtendedSensorDeviceClass.RATE_PRESSURE_PRODUCT}_mmHg_bpm",
        native_unit_of_measurement="mmHg*bpm",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:multiplication",
    ),
    (
        OmronExtendedSensorDeviceClass.BLOOD_PRESSURE_CATEGORY,
        None,
    ): SensorEntityDescription(
        key=f"{OmronExtendedSensorDeviceClass.BLOOD_PRESSURE_CATEGORY}",
        icon="mdi:clipboard-pulse-outline",
    ),

    # Timestamp (datetime object)
    (
        OmronSensorDeviceClass.TIMESTAMP,
        None,
    ): SensorEntityDescription(
        key=str(OmronSensorDeviceClass.TIMESTAMP),
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
}

ADVERTISEMENT_SENSOR_DESCRIPTIONS = {
    # Signal Strength (RSSI) — passive advertisement (ble-esl pattern)
    (
        OmronSensorDeviceClass.SIGNAL_STRENGTH,
        Units.SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    ): SensorEntityDescription(
        key=f"{OmronSensorDeviceClass.SIGNAL_STRENGTH}_{Units.SIGNAL_STRENGTH_DECIBELS_MILLIWATT}",
        translation_key="signal_strength",
        has_entity_name=True,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
}

def hass_device_info(sensor_device_info, address: str | None = None):
    """Map SensorDeviceInfo to HA DeviceInfo (BLE connection + firmware fields)."""
    return hass_device_info_with_ble_connection(
        sensor_device_info, address, include_revision_attrs=True
    )


def _sensor_description_for_update(sensor_update: SensorUpdate, device_key: DeviceKey) -> SensorEntityDescription | None:
    """Map poll-backed sensor-state description to HA sensor description."""
    state_desc = sensor_update.entity_descriptions.get(device_key)
    if state_desc is None or state_desc.device_class is None:
        return None
    key = (state_desc.device_class, state_desc.native_unit_of_measurement)
    if key in ADVERTISEMENT_SENSOR_DESCRIPTIONS:
        return None
    return SENSOR_DESCRIPTIONS.get(key)


def advertisement_sensor_update_to_bluetooth_data_update(
    sensor_update: SensorUpdate,
) -> PassiveBluetoothDataUpdate[int | float | None]:
    """Convert advertisement SensorUpdate keys (RSSI) to a PassiveBluetooth update."""
    return PassiveBluetoothDataUpdate(
        devices={
            device_id: hass_device_info(device_info)
            for device_id, device_info in sensor_update.devices.items()
        },
        entity_descriptions={
            device_key_to_bluetooth_entity_key(device_key): (
                ADVERTISEMENT_SENSOR_DESCRIPTIONS[
                    (
                        description.device_class,
                        description.native_unit_of_measurement,
                    )
                ]
            )
            for device_key, description in sensor_update.entity_descriptions.items()
            if (
                description.device_class is not None
                and (
                    description.device_class,
                    description.native_unit_of_measurement,
                )
                in ADVERTISEMENT_SENSOR_DESCRIPTIONS
            )
        },
        # None clears a name saved before translation_key existed. An absent
        # key would leave that English name in place on upgrade.
        entity_names={
            device_key_to_bluetooth_entity_key(device_key): None
            for device_key in sensor_update.entity_descriptions
            if _is_advertisement_sensor_key(sensor_update, device_key)
        },
        entity_data={
            # Keep the parser's int RSSI. float() turns -55 into -55.0, and
            # this entity has no suggested_display_precision.
            device_key_to_bluetooth_entity_key(device_key): sensor_values.native_value
            for device_key, sensor_values in sensor_update.entity_values.items()
            if _is_advertisement_sensor_key(sensor_update, device_key)
            and isinstance(sensor_values.native_value, (int, float))
        },
    )


def _is_advertisement_sensor_key(
    sensor_update: SensorUpdate, device_key: DeviceKey
) -> bool:
    """Return True when this key is an advertisement-only sensor (RSSI)."""
    desc = sensor_update.entity_descriptions.get(device_key)
    if desc is None or desc.device_class is None:
        return False
    return (
        desc.device_class,
        desc.native_unit_of_measurement,
    ) in ADVERTISEMENT_SENSOR_DESCRIPTIONS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OmronConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Omron BLE sensors."""
    runtime = entry.runtime_data
    bt_coordinator = runtime.bt_coordinator
    poll_coordinator = runtime.poll_coordinator
    known_entity_keys: set[str] = set()

    # RSSI from advertisements — same PassiveBluetooth path as ble-esl.
    adv_processor = OmronPassiveBluetoothDataProcessor(
        advertisement_sensor_update_to_bluetooth_data_update
    )
    entry.async_on_unload(
        adv_processor.async_add_entities_listener(
            OmronAdvertisementSensorEntity, async_add_entities
        )
    )
    entry.async_on_unload(
        bt_coordinator.async_register_processor(adv_processor, SensorEntityDescription)
    )

    def _build_new_entities(sensor_update: SensorUpdate | None) -> list[SensorEntity]:
        if sensor_update is None:
            return []
        new_entities: list[SensorEntity] = []
        for device_key in sensor_update.entity_descriptions:
            entity_key = device_key_entity_id_suffix(device_key)
            if entity_key in known_entity_keys:
                continue
            description = _sensor_description_for_update(sensor_update, device_key)
            if description is None:
                continue
            sensor_value = sensor_update.entity_values.get(device_key)
            sensor_name = sensor_value.name if sensor_value is not None else str(device_key.key)
            new_entities.append(
                OmronBluetoothSensorEntity(
                    entry=entry,
                    coordinator=poll_coordinator,
                    device_key=device_key,
                    description=description,
                    sensor_name=sensor_name,
                )
            )
            known_entity_keys.add(entity_key)
        return new_entities

    initial_entities = _build_new_entities(poll_coordinator.data)
    if initial_entities:
        async_add_entities(initial_entities)

    @callback
    def _handle_poll_update() -> None:
        """Create entities for new keys discovered in later polls."""
        new_entities = _build_new_entities(poll_coordinator.data)
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(poll_coordinator.async_add_listener(_handle_poll_update))

    async_add_entities(
        [
            OmronPollDurationSensorEntity(entry, runtime.duration_coordinator),
            OmronLastReadoutSensorEntity(entry, runtime.readout_coordinator),
            OmronLastFailureSensorEntity(entry, runtime.failure_coordinator),
            OmronFailureCountSensorEntity(entry, runtime.failure_count_coordinator),
        ]
    )


class OmronAdvertisementSensorEntity(
    PassiveBluetoothProcessorEntity[
        OmronPassiveBluetoothDataProcessor[int | float | None]
    ],
    SensorEntity,
):
    """Sensor fed by Omron BLE advertisements (RSSI).

    Availability stays with the processor. RSSI is a measurement: holding the
    last dBm after the cuff disappears keeps writing a stale value into history.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        processor: OmronPassiveBluetoothDataProcessor[int | float | None],
        entity_key,
        description: SensorEntityDescription,
        context=None,
    ) -> None:
        super().__init__(processor, entity_key, description, context)
        # TODO: remove after 3.2.x. A name restored from 3.1.0 or earlier
        # would override translation_key until the next restart, so drop it
        # here. Once those installs have restarted, nothing sets it.
        if hasattr(self, "_attr_name"):
            del self._attr_name
        self._attr_unique_id = preserved_passive_unique_id(
            model=processor.coordinator.device_data.device_model,
            address=processor.coordinator.address,
            entity_key=entity_key,
        )

    @property
    def native_value(self) -> int | float | None:
        """Return the native value."""
        return self.processor.entity_data.get(self.entity_key)


class OmronBluetoothSensorEntity(
    OmronCoordinatorEntity[SensorUpdate],
    RestoreEntity,
    SensorEntity,
):
    """Representation of a Omron BLE sensor."""

    entity_description: SensorEntityDescription

    def __init__(
        self,
        entry: OmronConfigEntry,
        coordinator: DataUpdateCoordinator[SensorUpdate],
        device_key: DeviceKey,
        description: SensorEntityDescription,
        sensor_name: str,
    ) -> None:
        """Initialize sensor entity backed by poll coordinator state."""
        super().__init__(entry, coordinator)
        self.entity_description = description
        self._device_key = device_key
        self._omron_device_data = self._runtime.device_data
        key_slug = f"{device_key.device_id}_{device_key.key}".lower().replace(" ", "_")
        self._attr_unique_id = self._runtime.entity_unique_id(key_slug)
        apply_translated_entity_name(
            self,
            str(device_key.key),
            getattr(self._omron_device_data, "_user_aliases", {}),
            sensor_name,
        )
        self._restored_native_value: Any | None = None

    def _coerce_native_value(self, value: Any) -> Any:
        """Normalize values from coordinator or restore (timestamps, etc.)."""
        if (
            self.entity_description.device_class == SensorDeviceClass.TIMESTAMP
            and isinstance(value, str)
        ):
            parsed = dt_util.parse_datetime(value)
            if parsed is None:
                try:
                    parsed = dt.datetime.fromisoformat(value)
                except ValueError:
                    return None
            if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
                parsed = parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            return parsed
        return value

    def _parse_restored_state_string(self, state_str: str) -> Any:
        """Parse recorder state string back to a native value."""
        if state_str in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
            return None
        if self.entity_description.device_class == SensorDeviceClass.TIMESTAMP:
            parsed = dt_util.parse_datetime(state_str)
            if parsed is None:
                try:
                    parsed = dt.datetime.fromisoformat(state_str)
                except ValueError:
                    return None
            if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
                parsed = parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            return parsed
        if self.entity_description.state_class == SensorStateClass.MEASUREMENT:
            try:
                num = float(state_str)
                if num.is_integer():
                    return int(num)
                return num
            except ValueError:
                return state_str
        return state_str

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator and restore last state from recorder."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None:
            return

        # Restore custom attributes (like truread_details) across reboots
        device_id = self._device_key.device_id
        if not device_id:
            device_id = self._resolve_user_id_from_key()

        if not hasattr(self._omron_device_data, 'omron_extra_attributes'):
            self._omron_device_data.omron_extra_attributes = {}
        if device_id not in self._omron_device_data.omron_extra_attributes:
            self._omron_device_data.omron_extra_attributes[device_id] = {}

        for key in ['truread_details', 'measurement_type', 'improper_position']:
            if key in last_state.attributes:
                value = last_state.attributes[key]
                if key == "measurement_type":
                    value = _LEGACY_MEASUREMENT_TYPES.get(value, value)
                self._omron_device_data.omron_extra_attributes[device_id][key] = value

        if last_state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, None):
            return
        self._restored_native_value = self._parse_restored_state_string(
            str(last_state.state)
        )

    def _resolve_user_id_from_key(self) -> str:
        """Resolve user_id from sensor key using aliases or numeric suffix."""
        key = str(self._device_key.key)
        # Reverse search using aliases (dynamic, works when names change)
        aliases = getattr(self._omron_device_data, '_user_aliases', {})
        if aliases:
            from .omron_ble.util import slugify_for_entity_key
            for u_idx, label in aliases.items():
                slug = slugify_for_entity_key(label)
                if slug and key.endswith(f"_{slug}"):
                    return f"user_{u_idx}"
        # Fallback: numeric suffix (_2, _user2)
        import re
        match = re.search(r'_(?:user)?([0-9]+)$', key)
        if match:
            return f"user_{match.group(1)}"
        return 'user_1'

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the state attributes."""
        attrs = {}
        try:
            device_id = self._device_key.device_id
            if not device_id:
                device_id = self._resolve_user_id_from_key()

            if hasattr(self._omron_device_data, 'omron_extra_attributes'):
                if device_id in self._omron_device_data.omron_extra_attributes:
                    attrs.update(self._omron_device_data.omron_extra_attributes[device_id])
        except Exception:
            pass
        return attrs if attrs else None

    @property
    def native_value(self) -> Any:
        """Return the native value."""
        sensor_update = self.coordinator.data
        if sensor_update is not None:
            sensor_value = sensor_update.entity_values.get(self._device_key)
            if sensor_value is not None and sensor_value.native_value is not None:
                return self._coerce_native_value(sensor_value.native_value)
        if self._restored_native_value is not None:
            return self._coerce_native_value(self._restored_native_value)
        return None

    @property
    def available(self) -> bool:
        """Keep showing last restored value when coordinator poll has not succeeded yet."""
        if self.native_value is not None:
            return True
        return super().available

    @property
    def device_info(self) -> DeviceInfo:
        """Attach sensor to the same discovered Omron device."""
        sensor_update = self.coordinator.data
        if sensor_update is not None:
            sensor_device_info = sensor_update.devices.get(self._device_key.device_id)
            if sensor_device_info is not None:
                return hass_device_info(sensor_device_info, self._address)
        return DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._address)},
        )


class OmronPollDurationSensorEntity(
    OmronCoordinatorEntity[float | None],
    SensorEntity,
):
    """Diagnostic sensor for latest poll duration.

    Its attributes are the last BLE session's breakdown -- outcome, the radio
    it went over, and per-stage timings (``connect_s``, ``unlock_s``,
    ``readout_s`` ...) -- so the session can be read from the entity instead
    of debug logs. See ``session_report.build_session_report``. ``failed_stage``
    is the shared name (``auth``, ``transfer``, …); ``failed_detail`` is the
    cuff's own (``unlock``, ``readout``, …).
    """

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:timer-outline"
    _attr_has_entity_name = True
    _attr_translation_key = "poll_duration"

    def __init__(
        self,
        entry: OmronConfigEntry,
        coordinator: DataUpdateCoordinator[float | None],
    ) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = self._runtime.entity_unique_id("duration")

    @property
    def native_value(self) -> float | None:
        """Return wall-clock seconds for the last poll attempt (success or failure)."""
        return self.coordinator.data

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The last session's breakdown; each session's final duration update publishes it."""
        return self._runtime.session_reports.last

    @property
    def device_info(self) -> DeviceInfo:
        """Attach sensor to the same BLE device."""
        return DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._address)},
        )


class OmronLastFailureSensorEntity(
    OmronCoordinatorEntity[datetime | None],
    SensorEntity,
):
    """Diagnostic sensor for when a BLE session last failed.

    Its attributes are that session's breakdown (the same keys as Duration's),
    kept until the next failure so a poll that succeeded since does not erase
    it. A connect failure right after a reading is the cuff going back to
    sleep -- the ``likely_cause`` attribute says so -- but a stage further in,
    or one that repeats, is where to start when a cuff will not sync.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:clock-alert-outline"
    _attr_has_entity_name = True
    _attr_translation_key = "last_failure"

    def __init__(
        self,
        entry: OmronConfigEntry,
        coordinator: DataUpdateCoordinator[datetime | None],
    ) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = self._runtime.entity_unique_id("last_failure")

    @property
    def native_value(self) -> "datetime | None":
        """Return when a session last failed, or None if none has since setup."""
        return self.coordinator.data

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The breakdown of the session that failed at this time."""
        return self._runtime.session_reports.last_failure

    @property
    def device_info(self) -> DeviceInfo:
        """Attach sensor to the same BLE device."""
        return DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._address)},
        )


class OmronFailureCountSensorEntity(
    OmronCoordinatorEntity[int],
    SensorEntity,
):
    """Diagnostic sensor counting failed BLE sessions since the entry was (re)loaded.

    Rising while the measurements look fine means sessions are failing where
    nobody is watching -- the cuff going back to sleep before a scheduled poll
    is the usual reason, and Last Failure says which.
    """

    # No state class: the count starts over on every reload, and a
    # long-term statistic built from that would only mislead.
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:alert-circle-outline"
    _attr_has_entity_name = True
    _attr_translation_key = "failure_count"

    def __init__(
        self,
        entry: OmronConfigEntry,
        coordinator: DataUpdateCoordinator[int],
    ) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = self._runtime.entity_unique_id("failure_count")

    @property
    def native_value(self) -> int | None:
        """Return the number of failed sessions since setup."""
        return self.coordinator.data

    @property
    def device_info(self) -> DeviceInfo:
        """Attach sensor to the same BLE device."""
        return DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._address)},
        )


class OmronLastReadoutSensorEntity(
    OmronCoordinatorEntity[datetime | None],
    SensorEntity,
):
    """Diagnostic sensor for when a poll last decoded a record.

    The poll serves cached data instead of failing, so that a cuff which is
    asleep or out of range -- its normal state between readings -- does not
    take every entity unavailable. The cost is that a run where nothing got
    through looks the same from the outside as a quiet one. This is the
    difference: it only moves when a record was actually decoded.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cloud-check-outline"
    _attr_has_entity_name = True
    _attr_translation_key = "last_readout"

    def __init__(
        self,
        entry: OmronConfigEntry,
        coordinator: DataUpdateCoordinator[datetime | None],
    ) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = self._runtime.entity_unique_id("last_readout")

    @property
    def native_value(self) -> "datetime | None":
        """Return when a record was last decoded, or None if none ever was."""
        return self.coordinator.data

    @property
    def device_info(self) -> DeviceInfo:
        """Attach sensor to the same BLE device."""
        return DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._address)},
        )
