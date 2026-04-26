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
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo, CONNECTION_BLUETOOTH
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.sensor import sensor_device_info_to_hass_device_info
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import DOMAIN
from .omron_ble import DeviceKey
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


def _device_key_id(device_key: DeviceKey) -> str:
    """Build a stable identifier from sensor-state device key."""
    return f"{device_key.device_id}_{device_key.key}"


def _binary_description_for_update(
    sensor_update: SensorUpdate,
    device_key: DeviceKey,
) -> BinarySensorEntityDescription | None:
    """Map sensor-state binary description to HA binary description."""
    state_desc = sensor_update.binary_entity_descriptions.get(device_key)
    if state_desc is None or state_desc.device_class is None:
        return None
    return BINARY_SENSOR_DESCRIPTIONS.get(state_desc.device_class)


def _binary_device_info(
    sensor_device_info,
    address: str,
) -> dict:
    """Convert sensor-state device info and ensure BLE connection key exists."""
    device_info = sensor_device_info_to_hass_device_info(sensor_device_info)
    if "connections" not in device_info:
        device_info["connections"] = {(CONNECTION_BLUETOOTH, address)}
    # Keep only BLE connection metadata from binary-sensor-side device_info.
    for key in list(device_info):
        if key != "connections":
            device_info.pop(key, None)
    return device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OmronConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Omron BLE binary sensors."""
    poll_coordinator = entry.runtime_data.poll_coordinator
    known_entity_keys: set[str] = set()

    def _build_new_entities(sensor_update: SensorUpdate | None) -> list[BinarySensorEntity]:
        if sensor_update is None:
            return []
        new_entities: list[BinarySensorEntity] = []
        for device_key in sensor_update.binary_entity_descriptions:
            entity_key = _device_key_id(device_key)
            if entity_key in known_entity_keys:
                continue
            description = _binary_description_for_update(sensor_update, device_key)
            if description is None:
                continue
            sensor_value = sensor_update.binary_entity_values.get(device_key)
            sensor_name = sensor_value.name if sensor_value is not None else str(device_key.key)
            new_entities.append(
                OmronBluetoothBinarySensorEntity(
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

    connection_coordinator = (
        hass.data[DOMAIN][entry.entry_id].get("connection_coordinator")
    )
    if connection_coordinator is not None:
        async_add_entities(
            [OmronConnectionBinarySensorEntity(hass, entry, connection_coordinator)]
        )


class OmronBluetoothBinarySensorEntity(
    CoordinatorEntity[DataUpdateCoordinator[SensorUpdate]],
    BinarySensorEntity,
):
    """Representation of a Omron binary sensor."""

    entity_description: BinarySensorEntityDescription

    def __init__(
        self,
        hass: HomeAssistant,
        entry: OmronConfigEntry,
        coordinator: DataUpdateCoordinator[SensorUpdate],
        device_key: DeviceKey,
        description: BinarySensorEntityDescription,
        sensor_name: str,
    ) -> None:
        """Initialize binary sensor entity backed by poll coordinator state."""
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
    def is_on(self) -> bool | None:
        """Return the native value."""
        sensor_update = self.coordinator.data
        if sensor_update is None:
            return None
        sensor_value = sensor_update.binary_entity_values.get(self._device_key)
        if sensor_value is None:
            return None
        return sensor_value.native_value

    @property
    def device_info(self) -> DeviceInfo:
        """Attach binary sensor to the same discovered Omron device."""
        sensor_update = self.coordinator.data
        if sensor_update is not None:
            sensor_device_info = sensor_update.devices.get(self._device_key.device_id)
            if sensor_device_info is not None:
                return _binary_device_info(
                    sensor_device_info,
                    self._address,
                )
        return DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._address)},
        )


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
