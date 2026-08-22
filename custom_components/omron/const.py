"""Constants for the Omron Bluetooth integration."""

from __future__ import annotations

from typing import Final

DOMAIN = "omron"
CONF_BINDKEY: Final = "bindkey"
CONF_DEVICE_MODEL: Final = "device_model"
CONF_USER_ALIASES: Final = "user_aliases"
# Application-layer bond key (hex). Written by the config flow after a
# successful secure pairing and reused by every later session — see
# omron_ble/secure_store.py.
CONF_SECURE_BOND_KEY: Final = "secure_bond_key"
