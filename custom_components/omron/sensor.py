"""Support for Omron sensors."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from .omron_ble import SensorDeviceClass as OmronSensorDeviceClass, SensorUpdate, Units
from .omron_ble.const import (
    ExtendedSensorDeviceClass as OmronExtendedSensorDeviceClass,
)

from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataUpdate,
    PassiveBluetoothProcessorEntity,
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

from .coordinator import OmronPassiveBluetoothDataProcessor
from .const import DOMAIN
from .device import device_key_to_bluetooth_entity_key
from .types import OmronConfigEntry

_LOGGER = logging.getLogger(__name__)

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

def hass_device_info(sensor_device_info):
    device_info = sensor_device_info_to_hass_device_info(sensor_device_info)
    if sensor_device_info.sw_version is not None:
        device_info[ATTR_SW_VERSION] = sensor_device_info.sw_version
    if sensor_device_info.hw_version is not None:
        device_info[ATTR_HW_VERSION] = sensor_device_info.hw_version
    return device_info
    
def sensor_update_to_bluetooth_data_update(
    sensor_update: SensorUpdate,
) -> PassiveBluetoothDataUpdate[Any]:
    """Convert a sensor update to a bluetooth data update."""
    return PassiveBluetoothDataUpdate(
        devices={
            device_id: hass_device_info(device_info)
            for device_id, device_info in sensor_update.devices.items()
        },
        entity_descriptions={
            device_key_to_bluetooth_entity_key(device_key): descriptor
            for device_key, description in sensor_update.entity_descriptions.items()
            if description.device_class
            and (
                descriptor := SENSOR_DESCRIPTIONS.get(
                    (description.device_class, description.native_unit_of_measurement)
                )
            )
        },
        entity_data={
            device_key_to_bluetooth_entity_key(device_key): sensor_values.native_value
            for device_key, sensor_values in sensor_update.entity_values.items()
        },
        entity_names={
            device_key_to_bluetooth_entity_key(device_key): sensor_values.name
            for device_key, sensor_values in sensor_update.entity_values.items()
        },
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OmronConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Omron BLE sensors."""
    coordinator = entry.runtime_data
    processor = OmronPassiveBluetoothDataProcessor(
        sensor_update_to_bluetooth_data_update
    )
    entry.async_on_unload(
        processor.async_add_entities_listener(
            OmronBluetoothSensorEntity, async_add_entities
        )
    )
    entry.async_on_unload(
        coordinator.async_register_processor(processor, SensorEntityDescription)
    )
    duration_coordinator = (
        hass.data[DOMAIN][entry.entry_id].get("duration_coordinator")
    )
    extra_entities: list[SensorEntity] = []
    if duration_coordinator is not None:
        extra_entities.append(OmronPollDurationSensorEntity(hass, entry, duration_coordinator))
    if extra_entities:
        async_add_entities(extra_entities)


class OmronBluetoothSensorEntity(
    PassiveBluetoothProcessorEntity[OmronPassiveBluetoothDataProcessor[float | None]],
    SensorEntity,
):
    """Representation of a Omron BLE sensor."""

    @property
    def native_value(self) -> Any:
        """Return the native value."""
        value = self.processor.entity_data.get(self.entity_key)
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
    def available(self) -> bool:
        """Return True if entity is available."""
        return super().available

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        poll_coordinator = self.processor.coordinator.poll_coordinator

        @callback
        def _handle_poll_update() -> None:
            """Forward the latest poll coordinator payload to the processor."""
            self.processor.async_handle_update(poll_coordinator.data)
            _LOGGER.debug(
                "Applied poll update to entity %s (%s): value=%s",
                self.entity_id,
                self.entity_key,
                self.processor.entity_data.get(self.entity_key),
            )

        remove = poll_coordinator.async_add_listener(_handle_poll_update)
        self.async_on_remove(remove)


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
