"""Constants for the Omron Bluetooth integration."""

from __future__ import annotations

from typing import Final, TypedDict

DOMAIN = "omron"
CONF_BINDKEY: Final = "bindkey"
CONF_DEVICE_MODEL: Final = "device_model"
CONF_SUBTYPE: Final = "subtype"


# BLE Service UUIDs for device detection
OMRON_LEGACY_SERVICE_UUID = "ecbe3980-c9a2-11e1-b1bd-0002a5d5c51b"
OMRON_NEW_SERVICE_UUID = "0000fe4a-0000-1000-8000-00805f9b34fb"
