"""Support for Omron binary sensors."""

from __future__ import annotations

from .omron_ble import (
    BinarySensorDeviceClass as OmronBinarySensorDeviceClass,
    SensorUpdate,
)

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataUpdate,
    PassiveBluetoothProcessorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.sensor import sensor_device_info_to_hass_device_info

from .coordinator import OmronPassiveBluetoothDataProcessor
from .device import device_key_to_bluetooth_entity_key
from .types import OmronConfigEntry

BINARY_SENSOR_DESCRIPTIONS = {
    OmronBinarySensorDeviceClass.BATTERY: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.BATTERY,
        device_class=BinarySensorDeviceClass.BATTERY,
    ),
    OmronBinarySensorDeviceClass.BATTERY_CHARGING: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.BATTERY_CHARGING,
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
    ),
    OmronBinarySensorDeviceClass.CO: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.CO,
        device_class=BinarySensorDeviceClass.CO,
    ),
    OmronBinarySensorDeviceClass.COLD: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.COLD,
        device_class=BinarySensorDeviceClass.COLD,
    ),
    OmronBinarySensorDeviceClass.CONNECTIVITY: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.CONNECTIVITY,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
    OmronBinarySensorDeviceClass.DOOR: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.DOOR,
        device_class=BinarySensorDeviceClass.DOOR,
    ),
    OmronBinarySensorDeviceClass.HEAT: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.HEAT,
        device_class=BinarySensorDeviceClass.HEAT,
    ),
    OmronBinarySensorDeviceClass.GARAGE_DOOR: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.GARAGE_DOOR,
        device_class=BinarySensorDeviceClass.GARAGE_DOOR,
    ),
    OmronBinarySensorDeviceClass.GAS: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.GAS,
        device_class=BinarySensorDeviceClass.GAS,
    ),
    OmronBinarySensorDeviceClass.GENERIC: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.GENERIC,
    ),
    OmronBinarySensorDeviceClass.LIGHT: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.LIGHT,
        device_class=BinarySensorDeviceClass.LIGHT,
    ),
    OmronBinarySensorDeviceClass.LOCK: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.LOCK,
        device_class=BinarySensorDeviceClass.LOCK,
    ),
    OmronBinarySensorDeviceClass.MOISTURE: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.MOISTURE,
        device_class=BinarySensorDeviceClass.MOISTURE,
    ),
    OmronBinarySensorDeviceClass.MOTION: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.MOTION,
        device_class=BinarySensorDeviceClass.MOTION,
    ),
    OmronBinarySensorDeviceClass.MOVING: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.MOVING,
        device_class=BinarySensorDeviceClass.MOVING,
    ),
    OmronBinarySensorDeviceClass.OCCUPANCY: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.OCCUPANCY,
        device_class=BinarySensorDeviceClass.OCCUPANCY,
    ),
    OmronBinarySensorDeviceClass.OPENING: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.OPENING,
        device_class=BinarySensorDeviceClass.OPENING,
    ),
    OmronBinarySensorDeviceClass.PLUG: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.PLUG,
        device_class=BinarySensorDeviceClass.PLUG,
    ),
    OmronBinarySensorDeviceClass.POWER: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.POWER,
        device_class=BinarySensorDeviceClass.POWER,
    ),
    OmronBinarySensorDeviceClass.PRESENCE: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.PRESENCE,
        device_class=BinarySensorDeviceClass.PRESENCE,
    ),
    OmronBinarySensorDeviceClass.PROBLEM: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.PROBLEM,
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    OmronBinarySensorDeviceClass.RUNNING: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.RUNNING,
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
    OmronBinarySensorDeviceClass.SAFETY: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.SAFETY,
        device_class=BinarySensorDeviceClass.SAFETY,
    ),
    OmronBinarySensorDeviceClass.SMOKE: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.SMOKE,
        device_class=BinarySensorDeviceClass.SMOKE,
    ),
    OmronBinarySensorDeviceClass.SOUND: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.SOUND,
        device_class=BinarySensorDeviceClass.SOUND,
    ),
    OmronBinarySensorDeviceClass.TAMPER: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.TAMPER,
        device_class=BinarySensorDeviceClass.TAMPER,
    ),
    OmronBinarySensorDeviceClass.VIBRATION: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.VIBRATION,
        device_class=BinarySensorDeviceClass.VIBRATION,
    ),
    OmronBinarySensorDeviceClass.WINDOW: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.WINDOW,
        device_class=BinarySensorDeviceClass.WINDOW,
    ),
}


def sensor_update_to_bluetooth_data_update(
    sensor_update: SensorUpdate,
) -> PassiveBluetoothDataUpdate[bool | None]:
    """Convert a binary sensor update to a bluetooth data update."""
    return PassiveBluetoothDataUpdate(
        devices={
            device_id: sensor_device_info_to_hass_device_info(device_info)
            for device_id, device_info in sensor_update.devices.items()
        },
        entity_descriptions={
            device_key_to_bluetooth_entity_key(device_key): BINARY_SENSOR_DESCRIPTIONS[
                description.device_class
            ]
            for device_key, description in sensor_update.binary_entity_descriptions.items()
            if description.device_class
        },
        entity_data={
            device_key_to_bluetooth_entity_key(device_key): sensor_values.native_value
            for device_key, sensor_values in sensor_update.binary_entity_values.items()
        },
        entity_names={
            device_key_to_bluetooth_entity_key(device_key): sensor_values.name
            for device_key, sensor_values in sensor_update.binary_entity_values.items()
        },
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OmronConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Omron BLE binary sensors."""
    coordinator = entry.runtime_data
    processor = OmronPassiveBluetoothDataProcessor(
        sensor_update_to_bluetooth_data_update
    )
    entry.async_on_unload(
        processor.async_add_entities_listener(
            OmronBluetoothBinarySensorEntity, async_add_entities
        )
    )
    entry.async_on_unload(
        coordinator.async_register_processor(processor, BinarySensorEntityDescription)
    )


class OmronBluetoothBinarySensorEntity(
    PassiveBluetoothProcessorEntity[OmronPassiveBluetoothDataProcessor[bool | None]],
    BinarySensorEntity,
):
    """Representation of a Omron binary sensor."""

    @property
    def is_on(self) -> bool | None:
        """Return the native value."""
        return self.processor.entity_data.get(self.entity_key)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return super().available
