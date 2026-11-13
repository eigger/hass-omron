"""Constants for the Omron Bluetooth integration."""

from __future__ import annotations

from typing import Final

DOMAIN = "omron"
# Upper bound for a pairing/time-sync BLE session. Keep direct advertisement
# and button sessions under the same limit as the scheduled poll.
BLE_SESSION_TIMEOUT_SECONDS: Final = 180
CONF_BINDKEY: Final = "bindkey"
CONF_DEVICE_MODEL: Final = "device_model"
CONF_USER_ALIASES: Final = "user_aliases"
# Publish every measurement stored since the last poll (#234); off by default,
# offered only for the profiles that support it.
CONF_SYNC_STORED_RECORDS: Final = "sync_stored_records"
# Application-layer credential for profiles whose transport keeps its own
# key (UnlockMode.SECURE_SESSION). Stored hex-encoded; only the entry that
# established it can use it, so it never leaves this device's entry data.
CONF_TRANSPORT_CREDENTIAL: Final = "transport_credential"

# Users of a single-slot device (#301). The cuff or scale cannot tell people
# apart, so each user is a config subentry and every new measurement is given
# to one of them by the device's assignment strategy.
SUBENTRY_USER: Final = "user"
CONF_ASSIGNMENT_STRATEGY: Final = "assignment_strategy"
CONF_WEIGHT_TOLERANCE_PCT: Final = "weight_tolerance_pct"
STRATEGY_WEIGHT: Final = "weight"
STRATEGY_RULES: Final = "rules"
EVENT_UNASSIGNED_MEASUREMENT: Final = "omron_unassigned_measurement"

# Subentry data keys.
CONF_USER_NAME: Final = "name"
CONF_USER_EXPECTED_WEIGHT: Final = "expected_weight"
CONF_USER_HEIGHT: Final = "height_cm"
CONF_USER_TIME_WINDOWS: Final = "time_windows"
CONF_USER_WEIGHT_MIN: Final = "weight_min"
CONF_USER_WEIGHT_MAX: Final = "weight_max"
CONF_USER_SYS_MIN: Final = "sys_min"
CONF_USER_SYS_MAX: Final = "sys_max"
CONF_USER_DIA_MIN: Final = "dia_min"
CONF_USER_DIA_MAX: Final = "dia_max"

# Weight-based assignment. None of these are derived from measured data: they
# are design assumptions (docs/multi-user-plan.md, 2-1) not yet checked against
# real HN-300T2 readings, so they live here and nowhere else.
# How many of a user's latest assigned weights make up the reference weight.
WEIGHT_HISTORY_SIZE: Final = 5
# Weight given to an assigned weight halves every this many assignments (by
# assignment order, not time: the scale's clock cannot be trusted).
WEIGHT_HALF_LIFE: Final = 2
# Tolerance around the reference weight: a share of it, clamped to a range.
WEIGHT_TOLERANCE_PCT_DEFAULT: Final = 3.0
WEIGHT_TOLERANCE_PCT_MIN: Final = 1.0
WEIGHT_TOLERANCE_PCT_MAX: Final = 5.0
WEIGHT_TOLERANCE_MIN_KG: Final = 1.0
WEIGHT_TOLERANCE_MAX_KG: Final = 4.0
# The closest user must beat the runner-up by at least this much.
WEIGHT_MIN_GAP_KG: Final = 1.0
# Outside the tolerance the closest user still gets the weight when it is within
# this multiple of the tolerance and this many kg (a slow gain or loss).
WEIGHT_FALLBACK_TOLERANCE_FACTOR: Final = 2.0
WEIGHT_FALLBACK_MAX_KG: Final = 5.0
