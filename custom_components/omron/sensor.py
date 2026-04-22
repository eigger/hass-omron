"""Support for Omron sensors."""

from __future__ import annotations

import datetime as dt
from typing import Any
from .omron_ble import SensorDeviceClass as OmronSensorDeviceClass, SensorUpdate, Units
from .omron_ble.const import (
    ExtendedSensorDeviceClass as OmronExtendedSensorDeviceClass,
)

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    ATTR_SW_VERSION,
    ATTR_HW_VERSION,
    EntityCategory,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.sensor import sensor_device_info_to_hass_device_info
from homeassistant.helpers.device_registry import DeviceInfo, CONNECTION_BLUETOOTH
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import DOMAIN
from .omron_ble import DeviceKey
from .types import OmronConfigEntry

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
    # Signal Strength (RSSI) (dB)
    (
        OmronSensorDeviceClass.SIGNAL_STRENGTH,
        Units.SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    ): SensorEntityDescription(
        key=f"{OmronSensorDeviceClass.SIGNAL_STRENGTH}_{Units.SIGNAL_STRENGTH_DECIBELS_MILLIWATT}",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
}

def hass_device_info(sensor_device_info, address: str | None = None):
    device_info = sensor_device_info_to_hass_device_info(sensor_device_info)
    if address is not None and "connections" not in device_info:
        device_info["connections"] = {(CONNECTION_BLUETOOTH, address)}
    if sensor_device_info.sw_version is not None:
        device_info[ATTR_SW_VERSION] = sensor_device_info.sw_version
    if sensor_device_info.hw_version is not None:
        device_info[ATTR_HW_VERSION] = sensor_device_info.hw_version
    return device_info


def _device_key_id(device_key: DeviceKey) -> str:
    """Build a stable identifier from sensor-state device key."""
    return f"{device_key.device_id}_{device_key.key}"


def _sensor_description_for_update(sensor_update: SensorUpdate, device_key: DeviceKey) -> SensorEntityDescription | None:
    """Map sensor-state description to HA sensor description."""
    state_desc = sensor_update.entity_descriptions.get(device_key)
    if state_desc is None or state_desc.device_class is None:
        return None
    return SENSOR_DESCRIPTIONS.get(
        (state_desc.device_class, state_desc.native_unit_of_measurement)
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OmronConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Omron BLE sensors."""
    poll_coordinator = entry.runtime_data.poll_coordinator
    known_entity_keys: set[str] = set()

    def _build_new_entities(sensor_update: SensorUpdate | None) -> list[SensorEntity]:
        if sensor_update is None:
            return []
        new_entities: list[SensorEntity] = []
        for device_key in sensor_update.entity_descriptions:
            entity_key = _device_key_id(device_key)
            if entity_key in known_entity_keys:
                continue
            description = _sensor_description_for_update(sensor_update, device_key)
            if description is None:
                continue
            sensor_value = sensor_update.entity_values.get(device_key)
            sensor_name = sensor_value.name if sensor_value is not None else str(device_key.key)
            new_entities.append(
                OmronBluetoothSensorEntity(
                    hass=hass,
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

    duration_coordinator = (
        hass.data[DOMAIN][entry.entry_id].get("duration_coordinator")
    )
    extra_entities: list[SensorEntity] = []
    if duration_coordinator is not None:
        extra_entities.append(OmronPollDurationSensorEntity(hass, entry, duration_coordinator))
    if extra_entities:
        async_add_entities(extra_entities)


class OmronBluetoothSensorEntity(
    CoordinatorEntity[DataUpdateCoordinator[SensorUpdate]],
    SensorEntity,
):
    """Representation of a Omron BLE sensor."""

    entity_description: SensorEntityDescription

    def __init__(
        self,
        hass: HomeAssistant,
        entry: OmronConfigEntry,
        coordinator: DataUpdateCoordinator[SensorUpdate],
        device_key: DeviceKey,
        description: SensorEntityDescription,
        sensor_name: str,
    ) -> None:
        """Initialize sensor entity backed by poll coordinator state."""
        super().__init__(coordinator)
        self.entity_description = description
        self._device_key = device_key
        self._address = hass.data[DOMAIN][entry.entry_id]["address"]
        model = hass.data[DOMAIN][entry.entry_id]["data"].device_model
        identifier = self._address.replace(":", "")[-4:].lower()
        model_slug = model.lower().replace("-", "_")
        key_slug = f"{device_key.device_id}_{device_key.key}".lower().replace(" ", "_")
        self._attr_unique_id = f"{model_slug}_{identifier}_{key_slug}"
        self._attr_name = sensor_name

    @property
    def native_value(self) -> Any:
        """Return the native value."""
        sensor_update = self.coordinator.data
        if sensor_update is None:
            return None
        sensor_value = sensor_update.entity_values.get(self._device_key)
        if sensor_value is None:
            return None
        value = sensor_value.native_value
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
    CoordinatorEntity[DataUpdateCoordinator[float | None]],
    SensorEntity,
):
    """Diagnostic sensor for latest poll duration."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:timer-outline"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: OmronConfigEntry,
        coordinator: DataUpdateCoordinator[float | None],
    ) -> None:
        super().__init__(coordinator)
        model = hass.data[DOMAIN][entry.entry_id]["data"].device_model
        self._address = hass.data[DOMAIN][entry.entry_id]["address"]
        identifier = self._address.replace(":", "")[-4:].lower()
        model_slug = model.lower().replace("-", "_")
        self._attr_name = f"{model} {identifier.upper()} Duration"
        self._attr_unique_id = f"{model_slug}_{identifier}_duration"

    @property
    def native_value(self) -> float | None:
        """Return last successful poll duration in seconds."""
        return self.coordinator.data

    @property
    def device_info(self) -> DeviceInfo:
        """Attach sensor to the same BLE device."""
        return DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._address)},
        )
