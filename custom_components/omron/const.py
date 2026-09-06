"""Constants for the Omron Bluetooth integration."""

from __future__ import annotations

from typing import Final

DOMAIN = "omron"
CONF_BINDKEY: Final = "bindkey"
CONF_DEVICE_MODEL: Final = "device_model"
CONF_USER_ALIASES: Final = "user_aliases"
# Application-layer credential for profiles whose transport keeps its own
# key (UnlockMode.SECURE_SESSION). Stored hex-encoded; only the entry that
# established it can use it, so it never leaves this device's entry data.
CONF_TRANSPORT_CREDENTIAL: Final = "transport_credential"
