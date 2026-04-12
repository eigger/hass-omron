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
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo, CONNECTION_BLUETOOTH
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.sensor import sensor_device_info_to_hass_device_info
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .coordinator import OmronBluetoothDataProcessor
from .const import DOMAIN
from .device import device_key_to_bluetooth_entity_key
from .types import OmronConfigEntry

BINARY_SENSOR_DESCRIPTIONS = {
    OmronBinarySensorDeviceClass.BATTERY: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.BATTERY,
        device_class=BinarySensorDeviceClass.BATTERY,
    ),
    OmronBinarySensorDeviceClass.PROBLEM: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.PROBLEM,
        device_class=BinarySensorDeviceClass.PROBLEM,
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
    processor = OmronBluetoothDataProcessor(
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
    connection_coordinator = (
        hass.data[DOMAIN][entry.entry_id].get("connection_coordinator")
    )
    if connection_coordinator is not None:
        async_add_entities(
            [OmronConnectionBinarySensorEntity(hass, entry, connection_coordinator)]
        )


class OmronBluetoothBinarySensorEntity(
    PassiveBluetoothProcessorEntity[OmronBluetoothDataProcessor[bool | None]],
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


class OmronConnectionBinarySensorEntity(
    CoordinatorEntity[DataUpdateCoordinator[bool]],
    BinarySensorEntity,
):
    """Diagnostic binary sensor for active BLE poll connection."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        entry: OmronConfigEntry,
        coordinator: DataUpdateCoordinator[bool],
    ) -> None:
        super().__init__(coordinator)
        model = hass.data[DOMAIN][entry.entry_id]["data"].device_model
        self._address = hass.data[DOMAIN][entry.entry_id]["address"]
        identifier = self._address.replace(":", "")[-4:].lower()
        model_slug = model.lower().replace("-", "_")
        self._attr_name = f"{model} {identifier.upper()} Connection"
        self._attr_unique_id = f"{model_slug}_{identifier}_connection"

    @property
    def is_on(self) -> bool:
        """Return true while active BLE polling connection is open."""
        return bool(self.coordinator.data)

    @property
    def icon(self) -> str:
        """Return icon based on connection state."""
        return "mdi:bluetooth-connect" if self.is_on else "mdi:bluetooth-off"

    @property
    def device_info(self) -> DeviceInfo:
        """Attach sensor to the same BLE device."""
        return DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._address)},
        )
