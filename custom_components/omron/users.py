"""Users of a single-slot device and the measurements given to them (#301).

The device cannot say whose measurement it is, so each user is a config
subentry and every new measurement is handed to one of them by the device's
assignment strategy (``assignment.py``). What is learned along the way (recent
assigned weights, each user's last values, how far the records have been
handled) lives in a Store of its own, not in the entry: updating the entry
reloads the integration, which would restart it after every measurement.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .assignment import (
    KIND_MATCHED,
    KIND_NEAREST,
    Assignment,
    RuleUser,
    WeightUser,
    assign_by_rules,
    assign_by_weight,
    parse_windows,
    remember_weight,
)
from .const import (
    CONF_ASSIGNMENT_STRATEGY,
    CONF_DEVICE_MODEL,
    CONF_USER_DIA_MAX,
    CONF_USER_DIA_MIN,
    CONF_USER_EXPECTED_WEIGHT,
    CONF_USER_NAME,
    CONF_USER_SYS_MAX,
    CONF_USER_SYS_MIN,
    CONF_USER_TIME_WINDOWS,
    CONF_USER_WEIGHT_MAX,
    CONF_USER_WEIGHT_MIN,
    CONF_WEIGHT_TOLERANCE_PCT,
    DOMAIN,
    EVENT_UNASSIGNED_MEASUREMENT,
    STRATEGY_RULES,
    STRATEGY_WEIGHT,
    SUBENTRY_USER,
    WEIGHT_TOLERANCE_PCT_DEFAULT,
)
from .data import signal_user_updated
from .history import async_entry_device
from .omron_ble.const import DEFAULT_DEVICE_MODEL
from .omron_ble.devices import MeasurementKind, get_device_config

if TYPE_CHECKING:
    from .types import OmronConfigEntry

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1

# Values of a measurement that identify it, and that the event carries.
_SCALE_VALUES = ("weight",)
_PRESSURE_VALUES = ("sys", "dia", "bpm", "irregular_pulse", "body_movement")
# What a cuff user's last values keep: the event's values plus the two flags the
# user's binary sensors show (the event is left as it was).
_PRESSURE_STORED = (*_PRESSURE_VALUES, "cuff_loose", "improper_position")


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORE_VERSION, f"{DOMAIN}.users_{entry_id}")


def strategy_for(entry: OmronConfigEntry | Any, *, is_scale: bool) -> str:
    """The entry's assignment strategy: a scale's choice, a cuff's only one."""
    if not is_scale:
        return STRATEGY_RULES
    chosen = entry.options.get(CONF_ASSIGNMENT_STRATEGY)
    return chosen if chosen in (STRATEGY_WEIGHT, STRATEGY_RULES) else STRATEGY_WEIGHT


def supports_users(entry: OmronConfigEntry | Any) -> bool:
    """Whether users make sense: a weight scale or a cuff with one slot.

    The device cannot tell people apart then. Body-composition scales are not
    covered.
    """
    config = get_device_config(entry.data.get(CONF_DEVICE_MODEL, DEFAULT_DEVICE_MODEL))
    return config.num_users == 1 and config.measurement_kind in (
        MeasurementKind.WEIGHT,
        MeasurementKind.BLOOD_PRESSURE,
    )


def user_subentries(entry: OmronConfigEntry | Any) -> dict[str, Any]:
    """The entry's user subentries by id."""
    return {
        subentry_id: subentry
        for subentry_id, subentry in entry.subentries.items()
        if subentry.subentry_type == SUBENTRY_USER
    }


def record_fingerprint(record: dict[str, Any]) -> list[Any] | None:
    """When it was taken and what it read: how a record is told from the last.

    A record without a time is told by its values alone, so the same values
    twice in a row look like one measurement (a documented limit). None when
    the record has no values to go by.
    """
    if "weight" in record:
        values = [record.get("weight")]
    else:
        values = [record.get("sys"), record.get("dia"), record.get("bpm")]
    if all(value is None for value in values):
        return None
    return [record.get("device_time"), *values]


def _sequence_ahead(sequence: int, reference: int) -> int:
    """Steps from ``reference`` to ``sequence`` on the cuff's 16-bit counter."""
    return (sequence - reference) & 0xFFFF


def order_events(events: list[dict[str, Any]], last_seq: int | None) -> list[dict]:
    """Events newer than ``last_seq``, oldest first, across the counter's wrap.

    The events arrive in measured-time order, which a wrong cuff clock can
    scramble; the sequence is what the cuff counted. Without a last handled
    sequence there is nothing to drop, and the order is taken from the event
    every other one is ahead of.
    """
    numbered = [e for e in events if isinstance(e.get("sequence"), int)]
    if last_seq is not None:
        fresh = [
            e for e in numbered if 0 < _sequence_ahead(e["sequence"], last_seq) < 0x8000
        ]
        return sorted(fresh, key=lambda e: _sequence_ahead(e["sequence"], last_seq))
    if not numbered:
        return []
    anchor = next(
        (
            e
            for e in numbered
            if all(
                _sequence_ahead(o["sequence"], e["sequence"]) < 0x8000 for o in numbered
            )
        ),
        numbered[0],
    )
    return sorted(
        numbered, key=lambda e: _sequence_ahead(e["sequence"], anchor["sequence"])
    )


class OmronUsers:
    """The learned state of an entry's users, kept in one Store."""

    def __init__(self, hass: HomeAssistant, entry_id: str, strategy: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self.strategy = strategy
        # Fingerprint of the newest record seen, None until the first poll
        # after the first user was added (that poll only sets the baseline).
        self.fingerprint: list[Any] | None = None
        # Last handled cuff sequence, only while stored-record sync is on.
        self.last_seq: int | None = None
        # Per subentry id: recent assigned weights (oldest first) and the last
        # values, which a restart restores the sensors from.
        self.recent: dict[str, list[float]] = {}
        self.last: dict[str, dict[str, Any]] = {}

    async def async_load(self, user_ids: set[str]) -> None:
        """Read the Store, drop users that no longer exist, reset on a new strategy."""
        stored = await _store(self._hass, self._entry_id).async_load()
        if not isinstance(stored, dict):
            return
        fingerprint = stored.get("fingerprint")
        self.fingerprint = list(fingerprint) if isinstance(fingerprint, list) else None
        last_seq = stored.get("last_seq")
        self.last_seq = last_seq if isinstance(last_seq, int) else None
        users = stored.get("users")
        if not isinstance(users, dict):
            return
        same_strategy = stored.get("strategy") == self.strategy
        for user_id, state in users.items():
            if user_id not in user_ids or not isinstance(state, dict):
                continue
            # What was learned under another strategy means nothing to this
            # one; the last values are still what the sensors showed.
            recent = state.get("recent")
            if same_strategy and isinstance(recent, list):
                self.recent[user_id] = [
                    float(w) for w in recent if isinstance(w, (int, float))
                ]
            last = state.get("last")
            if isinstance(last, dict):
                self.last[user_id] = dict(last)

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "fingerprint": self.fingerprint,
            "last_seq": self.last_seq,
            "users": {
                user_id: {
                    "recent": list(self.recent.get(user_id, [])),
                    "last": dict(self.last.get(user_id, {})),
                }
                for user_id in {*self.recent, *self.last}
            },
        }

    async def async_save(self) -> None:
        await _store(self._hass, self._entry_id).async_save(self.as_dict())


async def async_setup_users(
    hass: HomeAssistant, entry: OmronConfigEntry, *, is_scale: bool
) -> OmronUsers | None:
    """Load the users of a single-slot device; None if it has none registered.

    Dropping the Store when the last user is gone makes the next registration
    start again from a baseline.
    """
    subentries = user_subentries(entry) if supports_users(entry) else {}
    if not subentries:
        # Nothing registered (or never was): no Store should linger from a user
        # who was deleted. Not touched for devices that never offer users.
        if supports_users(entry):
            await async_remove_users(hass, entry.entry_id)
        return None
    users = OmronUsers(hass, entry.entry_id, strategy_for(entry, is_scale=is_scale))
    await users.async_load(set(subentries))
    if users.strategy == STRATEGY_WEIGHT:
        # A user added while the scale assigned by rules has no usual weight,
        # and the weight-based strategy has nothing to place them by.
        missing = [
            subentry.title
            for subentry in subentries.values()
            if _number(subentry.data, CONF_USER_EXPECTED_WEIGHT) is None
        ]
        if missing:
            _LOGGER.warning(
                "Measurements are assigned by weight, but %s has no usual weight "
                "and will never be assigned one: edit the user to enter it",
                ", ".join(missing),
            )
    return users


async def async_remove_users(hass: HomeAssistant, entry_id: str) -> None:
    """Forget the users' state of a removed entry or of a last removed user."""
    await _store(hass, entry_id).async_remove()


# --- members ------------------------------------------------------------------


def _number(data: Any, key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def weight_users(users: OmronUsers, entry: OmronConfigEntry | Any) -> list[WeightUser]:
    members = []
    for user_id, subentry in user_subentries(entry).items():
        expected = _number(subentry.data, CONF_USER_EXPECTED_WEIGHT)
        if expected is None:
            continue
        members.append(
            WeightUser(
                user_id=user_id,
                name=subentry.data.get(CONF_USER_NAME) or subentry.title,
                expected_kg=expected,
                recent_kg=tuple(users.recent.get(user_id, [])),
            )
        )
    return members


def rule_users(entry: OmronConfigEntry | Any) -> list[RuleUser]:
    members = []
    for user_id, subentry in user_subentries(entry).items():
        data = subentry.data
        ranges = {
            "weight": (
                _number(data, CONF_USER_WEIGHT_MIN),
                _number(data, CONF_USER_WEIGHT_MAX),
            ),
            "sys": (_number(data, CONF_USER_SYS_MIN), _number(data, CONF_USER_SYS_MAX)),
            "dia": (_number(data, CONF_USER_DIA_MIN), _number(data, CONF_USER_DIA_MAX)),
        }
        members.append(
            RuleUser(
                user_id=user_id,
                name=data.get(CONF_USER_NAME) or subentry.title,
                windows=tuple(parse_windows(data.get(CONF_USER_TIME_WINDOWS)) or ()),
                ranges={k: v for k, v in ranges.items() if v != (None, None)},
            )
        )
    return members


# --- the poll hook --------------------------------------------------------------


def _usable(record: dict[str, Any], *, is_scale: bool) -> bool:
    """Whether a record carries the values a user is picked by."""
    if is_scale:
        weight = record.get("weight")
        return isinstance(weight, (int, float)) and weight > 0
    return isinstance(record.get("sys"), int) and isinstance(record.get("dia"), int)


def _last_values(record: dict[str, Any], assignment: Assignment, *, is_scale: bool):
    values: dict[str, Any] = {
        key: record.get(key)
        for key in (_SCALE_VALUES if is_scale else _PRESSURE_STORED)
    }
    values["measured_at"] = record.get("measured_at")
    values["assignment"] = assignment.kind
    return values


async def async_assign_measurements(
    hass: HomeAssistant, entry: OmronConfigEntry
) -> None:
    """Give this poll's new measurements to the entry's users.

    Called with the history lock held, before the stored-record sync clears its
    findings. The input is whatever the device offers: with stored-record sync
    on, the synced events (oldest first by their sequence); otherwise the
    newest record, if it is not the one handled last. Never both, or a record
    would be handed out twice. The newest record's fingerprint is kept in
    either mode, so switching the sync on or off hands nothing out again.

    Order: assign, fire the events, save. A restart in between repeats an
    event rather than losing one.
    """
    runtime = entry.runtime_data
    users = runtime.users
    if users is None:
        return
    device_data = runtime.device_data
    record = device_data.last_record
    fingerprint = record_fingerprint(record) if record is not None else None
    if fingerprint is None:
        # Nothing was read (or nothing usable): leave the baseline as it is and
        # hand out nothing.
        return
    pending = device_data.pending_history
    sync_on = device_data.history_last_seq is not None
    before = users.as_dict()
    is_scale = runtime.is_scale

    to_assign: list[dict[str, Any]] = []
    if users.fingerprint is None:
        # First poll since the first user was added: whatever the device still
        # holds is older than the registration, so only note where it stands.
        if sync_on and pending is not None:
            users.last_seq = pending["next_seq"].get(1)
    elif sync_on:
        if pending is not None:
            if pending.get("rebased", {}).get(1):
                # The cuff's counter went back: the old sequence means nothing.
                users.last_seq = pending["next_seq"].get(1)
            to_assign = order_events(pending["events"], users.last_seq)
    else:
        # Without the sync the cuff has no usable sequence, and one left from
        # an earlier sync would stop a later one from starting clean.
        users.last_seq = None
        if fingerprint != users.fingerprint:
            to_assign = [record]
    users.fingerprint = fingerprint

    strategy = users.strategy
    tolerance = float(
        entry.options.get(CONF_WEIGHT_TOLERANCE_PCT, WEIGHT_TOLERANCE_PCT_DEFAULT)
    )
    for measurement in to_assign:
        if sync_on and isinstance(measurement.get("sequence"), int):
            users.last_seq = measurement["sequence"]
        if not _usable(measurement, is_scale=is_scale):
            _LOGGER.debug("Skipping a record without usable values: %s", measurement)
            continue
        if strategy == STRATEGY_WEIGHT:
            assignment = assign_by_weight(
                float(measurement["weight"]),
                weight_users(users, entry),
                tolerance,
            )
        else:
            assignment = assign_by_rules(measurement, rule_users(entry))
        if assignment.user_id is None:
            hass.bus.async_fire(
                EVENT_UNASSIGNED_MEASUREMENT,
                _unassigned_event(hass, entry, measurement, assignment, is_scale),
            )
            continue
        user_id = assignment.user_id
        if assignment.kind in (KIND_MATCHED, KIND_NEAREST):
            users.recent[user_id] = remember_weight(
                users.recent.get(user_id, []), float(measurement["weight"])
            )
        users.last[user_id] = _last_values(measurement, assignment, is_scale=is_scale)
        async_dispatcher_send(hass, signal_user_updated(entry.entry_id, user_id))
        # Each record gets its own state change, as the stored-record sync does.
        await asyncio.sleep(0)

    if users.as_dict() != before:
        await users.async_save()


def _unassigned_event(
    hass: HomeAssistant,
    entry: OmronConfigEntry,
    measurement: dict[str, Any],
    assignment: Assignment,
    is_scale: bool,
) -> dict[str, Any]:
    device = async_entry_device(hass, entry)
    return {
        "device_id": device.id if device else None,
        "address": entry.runtime_data.address,
        "measured_at": measurement.get("measured_at"),
        "device_time": measurement.get("device_time"),
        "sequence": measurement.get("sequence"),
        **{
            key: measurement.get(key)
            for key in (_SCALE_VALUES if is_scale else _PRESSURE_VALUES)
        },
        "reason": assignment.reason,
        "candidates": assignment.candidates,
    }
