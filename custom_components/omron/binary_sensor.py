"""Support for Omron binary sensors."""

from __future__ import annotations

from sensor_state_data import (
    BinarySensorDeviceClass as OmronBinarySensorDeviceClass,
    DeviceKey,
    SensorUpdate,
)

from .omron_ble.const import ExtendedBinarySensorDeviceClass as OmronExtendedBinarySensorDeviceClass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataUpdate,
    PassiveBluetoothProcessorEntity,
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

# Advertisement MSD flags — live on the PassiveBluetooth processor (ble-esl pattern).
ADVERTISEMENT_BINARY_DEVICE_CLASSES = frozenset({
    OmronExtendedBinarySensorDeviceClass.FORCED_TRANSFER,
    OmronExtendedBinarySensorDeviceClass.INVALID_TIME,
    OmronExtendedBinarySensorDeviceClass.PAIRING_MODE,
})

# Measurement status flags from a successful GATT readout — poll coordinator.
POLL_BINARY_SENSOR_DESCRIPTIONS = {
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
}

ADVERTISEMENT_BINARY_SENSOR_DESCRIPTIONS = {
    OmronExtendedBinarySensorDeviceClass.FORCED_TRANSFER: BinarySensorEntityDescription(
        key=OmronExtendedBinarySensorDeviceClass.FORCED_TRANSFER,
        translation_key="forced_transfer",
        has_entity_name=True,
        icon="mdi:sync",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OmronExtendedBinarySensorDeviceClass.INVALID_TIME: BinarySensorEntityDescription(
        key=OmronExtendedBinarySensorDeviceClass.INVALID_TIME,
        translation_key="invalid_time",
        has_entity_name=True,
        icon="mdi:clock-alert-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OmronExtendedBinarySensorDeviceClass.PAIRING_MODE: BinarySensorEntityDescription(
        key=OmronExtendedBinarySensorDeviceClass.PAIRING_MODE,
        translation_key="pairing_mode",
        has_entity_name=True,
        icon="mdi:bluetooth-connect",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
}


def _poll_binary_description_for_update(
    sensor_update: SensorUpdate,
    device_key: DeviceKey,
) -> BinarySensorEntityDescription | None:
    """Map poll-backed sensor-state binary description to HA description."""
    state_desc = sensor_update.binary_entity_descriptions.get(device_key)
    if state_desc is None or state_desc.device_class is None:
        return None
    if state_desc.device_class in ADVERTISEMENT_BINARY_DEVICE_CLASSES:
        return None
    return POLL_BINARY_SENSOR_DESCRIPTIONS.get(state_desc.device_class)


def advertisement_binary_update_to_bluetooth_data_update(
    sensor_update: SensorUpdate,
) -> PassiveBluetoothDataUpdate[bool | None]:
    """Convert advertisement flag SensorUpdate keys to a PassiveBluetooth update."""
    return PassiveBluetoothDataUpdate(
        devices={
            device_id: hass_device_info_with_ble_connection(
                device_info, None, include_revision_attrs=False
            )
            for device_id, device_info in sensor_update.devices.items()
        },
        entity_descriptions={
            device_key_to_bluetooth_entity_key(device_key): (
                ADVERTISEMENT_BINARY_SENSOR_DESCRIPTIONS[description.device_class]
            )
            for device_key, description in sensor_update.binary_entity_descriptions.items()
            if _published_advertisement_binary(
                sensor_update,
                device_key,
                sensor_update.binary_entity_values.get(device_key),
            )
        },
        entity_names={},
        entity_data={
            device_key_to_bluetooth_entity_key(device_key): sensor_values.native_value
            for device_key, sensor_values in sensor_update.binary_entity_values.items()
            if _published_advertisement_binary(sensor_update, device_key, sensor_values)
        },
    )


def _is_advertisement_binary_key(
    sensor_update: SensorUpdate, device_key: DeviceKey
) -> bool:
    """Return True when this key is an MSD advertisement flag sensor."""
    desc = sensor_update.binary_entity_descriptions.get(device_key)
    return (
        desc is not None
        and desc.device_class in ADVERTISEMENT_BINARY_SENSOR_DESCRIPTIONS
    )


def _published_advertisement_binary(
    sensor_update: SensorUpdate, device_key: DeviceKey, sensor_values
) -> bool:
    """Publish a flag only once an advertisement has produced a real bool.

    ``None`` is not a reading. Writing it into the processor would mark a
    cuff that has never advertised as off, and would replace a restored
    ``on`` after restart.
    """
    if sensor_values is None:
        return False
    return _is_advertisement_binary_key(sensor_update, device_key) and isinstance(
        sensor_values.native_value, bool
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OmronConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Omron BLE binary sensors."""
    runtime = entry.runtime_data
    bt_coordinator = runtime.bt_coordinator
    poll_coordinator = runtime.poll_coordinator
    known_entity_keys: set[str] = set()

    # Advertisement MSD flags: update on every advert (hass-ble-esl pattern).
    adv_processor = OmronPassiveBluetoothDataProcessor(
        advertisement_binary_update_to_bluetooth_data_update
    )
    entry.async_on_unload(
        adv_processor.async_add_entities_listener(
            OmronAdvertisementBinarySensorEntity, async_add_entities
        )
    )
    entry.async_on_unload(
        bt_coordinator.async_register_processor(
            adv_processor, BinarySensorEntityDescription
        )
    )

    def _build_new_entities(sensor_update: SensorUpdate | None) -> list[BinarySensorEntity]:
        if sensor_update is None:
            return []
        new_entities: list[BinarySensorEntity] = []
        for device_key in sensor_update.binary_entity_descriptions:
            entity_key = device_key_entity_id_suffix(device_key)
            if entity_key in known_entity_keys:
                continue
            description = _poll_binary_description_for_update(sensor_update, device_key)
            if description is None:
                continue
            sensor_value = sensor_update.binary_entity_values.get(device_key)
            sensor_name = sensor_value.name if sensor_value is not None else str(device_key.key)
            new_entities.append(
                OmronBluetoothBinarySensorEntity(
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
        [OmronConnectionBinarySensorEntity(entry, runtime.connection_coordinator)]
    )


class OmronAdvertisementBinarySensorEntity(
    PassiveBluetoothProcessorEntity[
        OmronPassiveBluetoothDataProcessor[bool | None]
    ],
    BinarySensorEntity,
):
    """Binary sensor fed by Omron manufacturer advertisement flags."""

    _attr_has_entity_name = True

    def __init__(
        self,
        processor: OmronPassiveBluetoothDataProcessor[bool | None],
        entity_key,
        description: BinarySensorEntityDescription,
        context=None,
    ) -> None:
        super().__init__(processor, entity_key, description, context)
        self._attr_unique_id = preserved_passive_unique_id(
            model=processor.coordinator.device_data.device_model,
            address=processor.coordinator.address,
            entity_key=entity_key,
        )

    @property
    def is_on(self) -> bool | None:
        """Return the native value."""
        return self.processor.entity_data.get(self.entity_key)

    @property
    def available(self) -> bool:
        """Keep a real on/off while the cuff is asleep.

        A missing key or ``None`` is unknown, so the entity stays
        unavailable instead of claiming the cuff is off.
        """
        if isinstance(self.processor.entity_data.get(self.entity_key), bool):
            return True
        return super().available


class OmronBluetoothBinarySensorEntity(
    OmronCoordinatorEntity[SensorUpdate],
    RestoreEntity,
    BinarySensorEntity,
):
    """Representation of a poll-backed Omron binary sensor."""

    entity_description: BinarySensorEntityDescription

    def __init__(
        self,
        entry: OmronConfigEntry,
        coordinator: DataUpdateCoordinator[SensorUpdate],
        device_key: DeviceKey,
        description: BinarySensorEntityDescription,
        sensor_name: str,
    ) -> None:
        """Initialize binary sensor entity backed by poll coordinator state."""
        super().__init__(entry, coordinator)
        self.entity_description = description
        self._device_key = device_key
        key_slug = f"{device_key.device_id}_{device_key.key}".lower().replace(" ", "_")
        self._attr_unique_id = self._runtime.entity_unique_id(key_slug)
        apply_translated_entity_name(
            self,
            str(device_key.key),
            getattr(self._runtime.device_data, "_user_aliases", {}),
            sensor_name,
        )
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
    OmronCoordinatorEntity[bool],
    BinarySensorEntity,
):
    """Diagnostic binary sensor for active BLE poll connection."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_translation_key = "connection"

    def __init__(
        self,
        entry: OmronConfigEntry,
        coordinator: DataUpdateCoordinator[bool],
    ) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = self._runtime.entity_unique_id("connection")

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
