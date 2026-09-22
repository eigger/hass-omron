"""Shared helpers for sensor/binary_sensor platforms."""

from __future__ import annotations

from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothEntityKey,
)
from homeassistant.const import ATTR_HW_VERSION, ATTR_SW_VERSION
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.sensor import sensor_device_info_to_hass_device_info

from sensor_state_data import DeviceKey


def device_key_entity_id_suffix(device_key: DeviceKey) -> str:
    """Build a stable identifier from sensor-state device key."""
    return f"{device_key.device_id}_{device_key.key}"


def device_key_to_bluetooth_entity_key(
    device_key: DeviceKey,
) -> PassiveBluetoothEntityKey:
    """Convert a sensor-state device key to a PassiveBluetooth entity key."""
    return PassiveBluetoothEntityKey(device_key.key, device_key.device_id)


def hass_device_info_with_ble_connection(
    sensor_device_info,
    address: str | None,
    *,
    include_revision_attrs: bool = True,
) -> dict:
    """Map SensorDeviceInfo to HA DeviceInfo and ensure BLE connection is present."""
    device_info = sensor_device_info_to_hass_device_info(sensor_device_info)
    if address is not None and "connections" not in device_info:
        device_info["connections"] = {(CONNECTION_BLUETOOTH, address)}
    if include_revision_attrs:
        if sensor_device_info.sw_version is not None:
            device_info[ATTR_SW_VERSION] = sensor_device_info.sw_version
        if sensor_device_info.hw_version is not None:
            device_info[ATTR_HW_VERSION] = sensor_device_info.hw_version
    return device_info


def preserved_passive_unique_id(
    *,
    model: str,
    address: str,
    entity_key: PassiveBluetoothEntityKey,
) -> str:
    """Keep the CoordinatorEntity unique_id shape so registry entries survive.

    PassiveBluetoothProcessorEntity defaults to ``{address}-{key}``. Omron's
    poll-backed entities used ``{model}_{last4}_{device_id}_{key}``; changing
    that would orphan Data Pending / Pairing Mode / RSSI in the registry.
    """
    identifier = address.replace(":", "")[-4:].lower()
    model_slug = model.lower().replace("-", "_")
    key_slug = f"{entity_key.device_id}_{entity_key.key}".lower().replace(" ", "_")
    return f"{model_slug}_{identifier}_{key_slug}"
