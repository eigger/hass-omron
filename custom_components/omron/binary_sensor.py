"""Support for Omron binary sensors."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sensor_state_data import (
    BinarySensorDeviceClass as OmronBinarySensorDeviceClass,
    DeviceKey,
    SensorUpdate,
)

from .omron_ble.const import (
    ExtendedBinarySensorDeviceClass as OmronExtendedBinarySensorDeviceClass,
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
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .assignment import Window, in_windows, parse_windows
from .const import CONF_USER_TIME_WINDOWS, DOMAIN
from .coordinator import OmronPassiveBluetoothDataProcessor
from .entity import OmronCoordinatorEntity
from .entity_helpers import (
    apply_translated_entity_name,
    device_key_entity_id_suffix,
    device_key_to_bluetooth_entity_key,
    hass_device_info_with_ble_connection,
    preserved_passive_unique_id,
)
from .data import signal_user_updated
from .entity import OmronEntity, user_device_info
from .omron_ble.devices import get_device_config
from .omron_ble.record_parsers import recorded_pressure_flags
from .types import OmronConfigEntry
from .users import user_subentries

# Advertisement MSD flags — live on the PassiveBluetooth processor (ble-esl pattern).
ADVERTISEMENT_BINARY_DEVICE_CLASSES = frozenset(
    {
        OmronExtendedBinarySensorDeviceClass.FORCED_TRANSFER,
        OmronExtendedBinarySensorDeviceClass.INVALID_TIME,
        OmronExtendedBinarySensorDeviceClass.PAIRING_MODE,
    }
)

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

# Per-user status flags a readout can set; the ones restored from the registry.
_RESTORE_STATUS_FLAG_BASES = (
    OmronExtendedBinarySensorDeviceClass.BODY_MOVEMENT.value,
    OmronExtendedBinarySensorDeviceClass.CUFF_FIT.value,
    OmronExtendedBinarySensorDeviceClass.IRREGULAR_PULSE.value,
    OmronExtendedBinarySensorDeviceClass.IMPROPER_POSITION.value,
)

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
        device_class=BinarySensorDeviceClass.PROBLEM,
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
        # None clears a name saved before translation_key existed. An absent
        # key would leave that English name in place on upgrade.
        entity_names={
            device_key_to_bluetooth_entity_key(device_key): None
            for device_key, sensor_values in sensor_update.binary_entity_values.items()
            if _published_advertisement_binary(sensor_update, device_key, sensor_values)
        },
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

    def _build_new_entities(
        sensor_update: SensorUpdate | None,
    ) -> list[BinarySensorEntity]:
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
            sensor_name = (
                sensor_value.name if sensor_value is not None else str(device_key.key)
            )
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

    # Status flags have no seed, so they only appear with a readout. Re-add the
    # ones already registered so they restore their last value while the cuff
    # is asleep. Only keys valid for the current user count and aliases qualify,
    # so an entity left over from an old alias is not brought back.
    uid_prefix = runtime.entity_unique_id("none_")
    valid_suffixes = runtime.device_data.measurement_key_suffixes()
    registry = er.async_get(hass)
    restored_entities: list[BinarySensorEntity] = []
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.domain != "binary_sensor" or reg_entry.platform != DOMAIN:
            continue
        if reg_entry.disabled or not reg_entry.unique_id.startswith(uid_prefix):
            continue
        key = reg_entry.unique_id[len(uid_prefix) :]
        base = next((b for b in _RESTORE_STATUS_FLAG_BASES if key.startswith(b)), None)
        if base is None or key[len(base) :] not in valid_suffixes:
            continue
        device_key = DeviceKey(key=key, device_id=None)
        entity_key = device_key_entity_id_suffix(device_key)
        if entity_key in known_entity_keys:
            continue
        restored_entities.append(
            OmronBluetoothBinarySensorEntity(
                entry=entry,
                coordinator=poll_coordinator,
                device_key=device_key,
                description=POLL_BINARY_SENSOR_DESCRIPTIONS[
                    OmronExtendedBinarySensorDeviceClass(base)
                ],
                sensor_name=key,
            )
        )
        known_entity_keys.add(entity_key)
    if restored_entities:
        async_add_entities(restored_entities)

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

    if runtime.users is not None:
        for subentry_id, subentry in user_subentries(entry).items():
            async_add_entities(
                [OmronUserAssignmentTimeBinarySensor(entry, subentry)],
                config_subentry_id=subentry_id,
            )

    # Registered users of a single-slot cuff: the status flags of their last
    # measurement, only the ones this model's records carry.
    if runtime.users is not None and not runtime.is_scale:
        flags = recorded_pressure_flags(get_device_config(runtime.model).record_parser)
        for subentry_id, subentry in user_subentries(entry).items():
            async_add_entities(
                [OmronUserFlagBinarySensor(entry, subentry, flag) for flag in flags],
                config_subentry_id=subentry_id,
            )


class OmronAdvertisementBinarySensorEntity(
    PassiveBluetoothProcessorEntity[OmronPassiveBluetoothDataProcessor[bool | None]],
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
        # Restore replays the description saved before translation_key existed.
        # Swap in the current one first, then drop the saved English name so
        # it cannot override the translation or collapse to the device name.
        description = ADVERTISEMENT_BINARY_SENSOR_DESCRIPTIONS.get(
            description.key, description
        )
        super().__init__(processor, entity_key, description, context)
        if hasattr(self, "_attr_name"):
            del self._attr_name
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


# --- users of a single-slot cuff (#301) -----------------------------------------


@dataclass(frozen=True)
class _UserFlag:
    """One status flag of a user's last measurement."""

    key: str  # unique-id suffix, the device's own key for the flag
    stored: str  # key in the user's last values
    device_class: OmronExtendedBinarySensorDeviceClass


# The stored values are the record's; ``cuff_loose`` feeds the ``cuff_fit``
# sensor unchanged: under its PROBLEM class "on" means the cuff was too loose,
# as on the device's own sensor.
_USER_FLAGS = {
    "irregular_pulse": _UserFlag(
        "irregular_pulse",
        "irregular_pulse",
        OmronExtendedBinarySensorDeviceClass.IRREGULAR_PULSE,
    ),
    "body_movement": _UserFlag(
        "body_movement",
        "body_movement",
        OmronExtendedBinarySensorDeviceClass.BODY_MOVEMENT,
    ),
    "cuff_loose": _UserFlag(
        "cuff_fit",
        "cuff_loose",
        OmronExtendedBinarySensorDeviceClass.CUFF_FIT,
    ),
    "improper_position": _UserFlag(
        "improper_position",
        "improper_position",
        OmronExtendedBinarySensorDeviceClass.IMPROPER_POSITION,
    ),
}


class OmronUserFlagBinarySensor(OmronEntity, BinarySensorEntity):
    """A status flag of the user's last assigned measurement.

    Same device class, icon and polarity as the cuff's own flag sensor. The
    value is the Store's, so it is unknown until the user has a measurement
    and comes back after a restart without any state restore.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self, entry: OmronConfigEntry, subentry: ConfigSubentry, record_flag: str
    ) -> None:
        self._bind(entry)
        self._subentry = subentry
        self._flag = _USER_FLAGS[record_flag]
        self.entity_description = POLL_BINARY_SENSOR_DESCRIPTIONS[
            self._flag.device_class
        ]
        self._attr_translation_key = f"user_{self._flag.key}"
        self._attr_unique_id = (
            f"{entry.entry_id}_{subentry.subentry_id}_{self._flag.key}"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return user_device_info(self.hass, self._entry, self._subentry)

    @property
    def is_on(self) -> bool | None:
        users = self._runtime.users
        last = users.last.get(self._subentry.subentry_id, {}) if users else {}
        value = last.get(self._flag.stored)
        if not isinstance(value, bool):
            return None
        return value

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_user_updated(self._entry.entry_id, self._subentry.subentry_id),
                self._handle_user_update,
            )
        )

    @callback
    def _handle_user_update(self) -> None:
        self.async_write_ha_state()


class OmronUserAssignmentTimeBinarySensor(OmronEntity, BinarySensorEntity):
    """Whether the current local time satisfies a user's time windows."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "user_assignment_time"
    _attr_icon = "mdi:clock-check-outline"

    def __init__(self, entry: OmronConfigEntry, subentry: ConfigSubentry) -> None:
        self._bind(entry)
        self._subentry = subentry
        self._attr_unique_id = (
            f"{entry.entry_id}_{subentry.subentry_id}_assignment_time"
        )
        self._boundary_listeners: list[Callable[[], None]] = []
        self._scheduled_windows: list[Window] | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return user_device_info(self.hass, self._entry, self._subentry)

    @property
    def _windows(self) -> list[Window]:
        current = self._entry.subentries.get(self._subentry.subentry_id)
        return (
            parse_windows((current or self._subentry).data.get(CONF_USER_TIME_WINDOWS))
            or []
        )

    @property
    def is_on(self) -> bool:
        windows = self._windows
        now = dt_util.now()
        return not windows or in_windows(now.hour * 60 + now.minute, windows)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        def clock(minute: int) -> str:
            return f"{minute // 60:02d}:{minute % 60:02d}"

        return {
            "time_windows": [
                {"start": clock(start), "end": clock(end)}
                for start, end in self._windows
            ]
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._clear_boundaries)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_user_updated(self._entry.entry_id, self._subentry.subentry_id),
                self._handle_user_update,
            )
        )
        self._schedule_boundaries()

    @callback
    def _clear_boundaries(self) -> None:
        for unsubscribe in self._boundary_listeners:
            unsubscribe()
        self._boundary_listeners.clear()

    @callback
    def _schedule_boundaries(self) -> None:
        windows = self._windows
        if windows == self._scheduled_windows:
            return
        self._clear_boundaries()
        self._scheduled_windows = windows
        for minute in {
            boundary % 1440 for window in self._windows for boundary in window
        }:
            self._boundary_listeners.append(
                async_track_time_change(
                    self.hass,
                    self._handle_boundary,
                    hour=minute // 60,
                    minute=minute % 60,
                    second=0,
                )
            )

    @callback
    def _handle_boundary(self, _now: datetime) -> None:
        self.async_write_ha_state()

    @callback
    def _handle_user_update(self) -> None:
        self._schedule_boundaries()
        self.async_write_ha_state()
