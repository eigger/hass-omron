"""Shared helpers for sensor/binary_sensor platforms."""

from __future__ import annotations

from homeassistant.const import ATTR_HW_VERSION, ATTR_SW_VERSION
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.sensor import sensor_device_info_to_hass_device_info

from .omron_ble import DeviceKey


def device_key_entity_id_suffix(device_key: DeviceKey) -> str:
    """Build a stable identifier from sensor-state device key."""
    return f"{device_key.device_id}_{device_key.key}"


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
