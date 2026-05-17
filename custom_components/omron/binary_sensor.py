"""Support for Omron binary sensors."""

from __future__ import annotations

from .omron_ble import (
    BinarySensorDeviceClass as OmronBinarySensorDeviceClass,
    SensorUpdate,
)
from .omron_ble.const import ExtendedBinarySensorDeviceClass as OmronExtendedBinarySensorDeviceClass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import DOMAIN
from .entity_helpers import (
    device_key_entity_id_suffix,
    hass_device_info_with_ble_connection,
)
from .omron_ble import DeviceKey
from .types import OmronConfigEntry

BINARY_SENSOR_DESCRIPTIONS = {
    OmronBinarySensorDeviceClass.BATTERY: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.BATTERY,
        device_class=BinarySensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OmronBinarySensorDeviceClass.PROBLEM: BinarySensorEntityDescription(
        key=OmronBinarySensorDeviceClass.PROBLEM,
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    OmronExtendedBinarySensorDeviceClass.BODY_MOVEMENT: BinarySensorEntityDescription(
        key=OmronExtendedBinarySensorDeviceClass.BODY_MOVEMENT,
        device_class=BinarySensorDeviceClass.PROBLEM,
        icon="mdi:account-multiple",
    ),
    OmronExtendedBinarySensorDeviceClass.CUFF_FIT: BinarySensorEntityDescription(
        key=OmronExtendedBinarySensorDeviceClass.CUFF_FIT,
        device_class=BinarySensorDeviceClass.PROBLEM,
        icon="mdi:arm-flex",
    ),
    OmronExtendedBinarySensorDeviceClass.IRREGULAR_PULSE: BinarySensorEntityDescription(
        key=OmronExtendedBinarySensorDeviceClass.IRREGULAR_PULSE,
        device_class=BinarySensorDeviceClass.PROBLEM,
        icon="mdi:heart-multiple",
    ),
    OmronExtendedBinarySensorDeviceClass.IMPROPER_POSITION: BinarySensorEntityDescription(
        key=OmronExtendedBinarySensorDeviceClass.IMPROPER_POSITION,
        device_class=BinarySensorDeviceClass.PROBLEM,
        icon="mdi:seat-recline-normal",
    ),
    OmronExtendedBinarySensorDeviceClass.FORCED_TRANSFER: BinarySensorEntityDescription(
        key=OmronExtendedBinarySensorDeviceClass.FORCED_TRANSFER,
        icon="mdi:sync",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OmronExtendedBinarySensorDeviceClass.INVALID_TIME: BinarySensorEntityDescription(
        key=OmronExtendedBinarySensorDeviceClass.INVALID_TIME,
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OmronExtendedBinarySensorDeviceClass.PAIRING_MODE: BinarySensorEntityDescription(
        key=OmronExtendedBinarySensorDeviceClass.PAIRING_MODE,
        icon="mdi:bluetooth-connect",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
}


def _binary_description_for_update(
    sensor_update: SensorUpdate,
    device_key: DeviceKey,
) -> BinarySensorEntityDescription | None:
    """Map sensor-state binary description to HA binary description."""
    state_desc = sensor_update.binary_entity_descriptions.get(device_key)
    if state_desc is None or state_desc.device_class is None:
        return None
    return BINARY_SENSOR_DESCRIPTIONS.get(state_desc.device_class)


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
            entity_key = device_key_entity_id_suffix(device_key)
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
    RestoreEntity,
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
        self._restored_is_on: bool | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator and restore last state from recorder."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        if last_state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, None):
            return
        self._restored_is_on = last_state.state == "on"

    @property
    def is_on(self) -> bool | None:
        """Return the native value."""
        sensor_update = self.coordinator.data
        if sensor_update is not None:
            sensor_value = sensor_update.binary_entity_values.get(self._device_key)
            if sensor_value is not None and sensor_value.native_value is not None:
                return sensor_value.native_value
        if self._restored_is_on is not None:
            return self._restored_is_on
        return None

    @property
    def available(self) -> bool:
        """Keep showing last restored value when coordinator poll has not succeeded yet."""
        if self.is_on is not None:
            return True
        return super().available

    @property
    def device_info(self) -> DeviceInfo:
        """Attach binary sensor to the same discovered Omron device."""
        sensor_update = self.coordinator.data
        if sensor_update is not None:
            sensor_device_info = sensor_update.devices.get(self._device_key.device_id)
            if sensor_device_info is not None:
                return hass_device_info_with_ble_connection(
                    sensor_device_info,
                    self._address,
                    include_revision_attrs=False,
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
