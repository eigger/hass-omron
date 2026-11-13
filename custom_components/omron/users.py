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
import datetime as dt
import logging
import uuid
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_HOME, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .assignment import (
    KIND_MATCHED,
    KIND_NEAREST,
    KIND_MANUAL,
    REASON_MANUAL,
    REASON_NOBODY_HOME,
    REASON_SLOT_UNCLAIMED,
    REASON_USER_REMOVED,
    Assignment,
    RuleUser,
    SlotUser,
    WeightUser,
    assign_by_rules,
    assign_by_slot,
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
    CONF_USER_PERSON,
    CONF_USER_SLOT,
    CONF_USER_SYS_MAX,
    CONF_USER_SYS_MIN,
    CONF_USER_TIME_WINDOWS,
    CONF_USER_WEIGHT_MAX,
    CONF_USER_WEIGHT_MIN,
    CONF_WEIGHT_TOLERANCE_PCT,
    DOMAIN,
    EVENT_UNASSIGNED_MEASUREMENT,
    MEASUREMENT_LOG_SIZE,
    STRATEGY_RULES,
    STRATEGY_SLOT,
    STRATEGY_WEIGHT,
    SUBENTRY_USER,
    WEIGHT_HISTORY_SIZE,
    WEIGHT_TOLERANCE_PCT_DEFAULT,
)
from .data import signal_user_updated
from .history import async_entry_device
from .omron_ble.const import DEFAULT_DEVICE_MODEL
from .omron_ble.devices import MeasurementKind, get_device_config

if TYPE_CHECKING:
    from .types import OmronConfigEntry

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 2

# Values of a measurement that identify it, and that the event carries.
_SCALE_VALUES = ("weight",)
_BODY_VALUES = (
    "weight",
    "body_fat",
    "skeletal_muscle",
    "visceral_fat",
    "basal_metabolic_rate",
    "bmi",
)
_PRESSURE_VALUES = ("sys", "dia", "bpm", "irregular_pulse", "body_movement")


def _value_keys(kind: MeasurementKind) -> tuple[str, ...]:
    """The values a measurement of this kind of device carries."""
    if kind == MeasurementKind.BLOOD_PRESSURE:
        return _PRESSURE_VALUES
    if kind == MeasurementKind.BODY_COMPOSITION:
        return _BODY_VALUES
    return _SCALE_VALUES


def _migrate_v1(old: dict[str, Any]) -> dict[str, Any]:
    """Version 1 kept one fingerprint and one sequence; now each is per slot.

    A single-slot device's only slot is 1. What each user learned and last
    showed is carried over unchanged.
    """
    data = dict(old)
    fingerprint = data.pop("fingerprint", None)
    last_seq = data.pop("last_seq", None)
    data["fingerprints"] = {"1": fingerprint} if isinstance(fingerprint, list) else {}
    data["last_seq"] = (
        {"1": last_seq}
        if isinstance(last_seq, int) and not isinstance(last_seq, bool)
        else {}
    )
    return data


class _UsersStore(Store[dict[str, Any]]):
    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        if old_major_version == 1:
            return _migrate_v1(old_data)
        raise NotImplementedError(
            f"Unknown users store version {old_major_version}.{old_minor_version}"
        )


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return _UsersStore(hass, STORE_VERSION, f"{DOMAIN}.users_{entry_id}")


def _device_config(entry: OmronConfigEntry | Any):
    return get_device_config(entry.data.get(CONF_DEVICE_MODEL, DEFAULT_DEVICE_MODEL))


def strategy_for(entry: OmronConfigEntry | Any, *, is_scale: bool) -> str:
    """The entry's assignment strategy.

    A body-composition scale only has the slot it measured on; a scale of one
    kind of reading chooses between weight and rules; a cuff only has rules.
    """
    if _device_config(entry).measurement_kind == MeasurementKind.BODY_COMPOSITION:
        return STRATEGY_SLOT
    if not is_scale:
        return STRATEGY_RULES
    chosen = entry.options.get(CONF_ASSIGNMENT_STRATEGY)
    return chosen if chosen in (STRATEGY_WEIGHT, STRATEGY_RULES) else STRATEGY_WEIGHT


def supports_users(entry: OmronConfigEntry | Any) -> bool:
    """Whether users make sense: the device cannot tell people apart, or can name a slot.

    A weight scale or a cuff with one slot cannot tell people apart. A
    body-composition scale has slots, and a user is the one who claimed a slot.
    """
    config = _device_config(entry)
    if config.measurement_kind == MeasurementKind.BODY_COMPOSITION:
        return True
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

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        strategy: str,
        *,
        multi_slot: bool = False,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self.strategy = strategy
        # Whether the device reports the newest record of each slot (a
        # multi-slot scale) rather than one newest record.
        self.multi_slot = multi_slot
        # Per device slot (1 for a single-slot device): fingerprint of the
        # newest record seen. A slot is absent until the first poll after the
        # first user was added (that poll only sets the baseline).
        self.fingerprints: dict[int, list[Any]] = {}
        # Per slot: last handled sequence, only while stored-record sync is on.
        self.last_seq: dict[int, int] = {}
        # Per subentry id: recent assigned weights (oldest first) and the last
        # values, which a restart restores the sensors from.
        self.recent: dict[str, list[float]] = {}
        self.last: dict[str, dict[str, Any]] = {}
        # The newest ``MEASUREMENT_LOG_SIZE`` measurements handled, oldest
        # first, assigned or not (see ``log_measurement``).
        self.log: list[dict[str, Any]] = []

    async def async_load(self, user_ids: set[str]) -> None:
        """Read the Store, drop users that no longer exist, reset on a new strategy."""
        stored = await _store(self._hass, self._entry_id).async_load()
        if not isinstance(stored, dict):
            return
        for slot, fingerprint in _slot_map(stored.get("fingerprints")).items():
            if isinstance(fingerprint, list):
                self.fingerprints[slot] = list(fingerprint)
        for slot, sequence in _slot_map(stored.get("last_seq")).items():
            if isinstance(sequence, int) and not isinstance(sequence, bool):
                self.last_seq[slot] = sequence
        same_strategy = stored.get("strategy") == self.strategy
        self.log = _load_log(stored.get("log"), user_ids, same_strategy)
        users = stored.get("users")
        if not isinstance(users, dict):
            return
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
            "fingerprints": {
                str(slot): list(fp) for slot, fp in sorted(self.fingerprints.items())
            },
            "last_seq": {str(slot): seq for slot, seq in sorted(self.last_seq.items())},
            "users": {
                user_id: {
                    "recent": list(self.recent.get(user_id, [])),
                    "last": dict(self.last.get(user_id, {})),
                }
                for user_id in {*self.recent, *self.last}
            },
            "log": [dict(entry) for entry in self.log],
        }

    def log_measurement(
        self,
        measurement: dict[str, Any],
        slot: int,
        assignment: Assignment,
        value_keys: tuple[str, ...],
        *,
        learned: bool,
    ) -> dict[str, Any]:
        """Remember a handled measurement, so it can be moved to another user.

        ``learned`` says whether its weight went into the user's recent
        weights, which the reassignment has to take back out.
        """
        entry = {
            "id": uuid.uuid4().hex[:12],
            "user": assignment.user_id,
            "reason": assignment.reason,
            "kind": assignment.kind,
            "learned": learned,
            "slot": slot,
            "measured_at": measurement.get("measured_at"),
            "device_time": measurement.get("device_time"),
            "sequence": measurement.get("sequence"),
            "values": {key: measurement.get(key) for key in value_keys},
        }
        self.log.append(entry)
        del self.log[:-MEASUREMENT_LOG_SIZE]
        return entry

    def find_logged(self, measurement_id: str) -> dict[str, Any] | None:
        return next((e for e in self.log if e["id"] == measurement_id), None)

    def _newest_logged(self, user_id: str) -> dict[str, Any] | None:
        return next((e for e in reversed(self.log) if e["user"] == user_id), None)

    def _learned_weights(self, user_id: str) -> list[float]:
        return [
            float(e["values"]["weight"])
            for e in self.log
            if e["user"] == user_id and e["learned"]
        ]

    def reassign(self, entry: dict[str, Any], new_user_id: str | None) -> set[str]:
        """Move a logged measurement to ``new_user_id`` (None: to nobody).

        Returns the users whose sensors may have changed. Under the weight
        strategy the recent weights of both users are rebuilt: the log holds
        the latest measurements only, so the weights it knows of are the tail of
        a user's recent list, and what lies before them is kept as it was.
        """
        old_user_id = entry["user"]
        affected = {user for user in (old_user_id, new_user_id) if user}
        learning = self.strategy == STRATEGY_WEIGHT
        older: dict[str, list[float]] = {}
        if learning:
            for user in affected:
                known = len(self._learned_weights(user))
                recent = self.recent.get(user, [])
                older[user] = recent[: max(len(recent) - known, 0)]
        was_newest = (
            old_user_id is not None and self._newest_logged(old_user_id) is entry
        )

        weight = entry["values"].get("weight")
        entry["user"] = new_user_id
        entry["reason"] = None if new_user_id else REASON_MANUAL
        entry["kind"] = KIND_MANUAL if new_user_id else None
        entry["learned"] = bool(
            learning
            and new_user_id
            and isinstance(weight, (int, float))
            and not isinstance(weight, bool)
        )

        if learning:
            for user in affected:
                self.recent[user] = [*older[user], *self._learned_weights(user)][
                    -WEIGHT_HISTORY_SIZE:
                ]
        if old_user_id is not None and was_newest:
            fallback = self._newest_logged(old_user_id)
            if fallback is None:
                self.last.pop(old_user_id, None)
            else:
                self.last[old_user_id] = _last_from_log(fallback)
        if new_user_id is not None and self._newest_logged(new_user_id) is entry:
            self.last[new_user_id] = _last_from_log(entry)
        return affected

    async def async_save(self) -> None:
        await _store(self._hass, self._entry_id).async_save(self.as_dict())


def _slot_map(raw: Any) -> dict[int, Any]:
    """``{"1": x}`` from the Store back to ``{1: x}``, dropping odd keys."""
    result: dict[int, Any] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            try:
                result[int(key)] = value
            except (TypeError, ValueError):
                continue
    return result


def _last_from_log(entry: dict[str, Any]) -> dict[str, Any]:
    """What a user's sensors show for a logged measurement."""
    return {
        **entry["values"],
        "measured_at": entry["measured_at"],
        "assignment": entry["kind"],
        "measurement_id": entry["id"],
    }


def _load_log(
    raw: Any, user_ids: set[str], same_strategy: bool
) -> list[dict[str, Any]]:
    """The saved log, dropping what does not fit.

    A measurement of a user who no longer exists belongs to nobody now. What
    was learned under another strategy is not learned any more.
    """
    log: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return log
    for item in raw:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not isinstance(item.get("values"), dict)
        ):
            continue
        entry = {
            "id": item["id"],
            "user": item.get("user"),
            "reason": item.get("reason"),
            "kind": item.get("kind"),
            "learned": bool(item.get("learned")) and same_strategy,
            "slot": item.get("slot") if isinstance(item.get("slot"), int) else 1,
            "measured_at": item.get("measured_at"),
            "device_time": item.get("device_time"),
            "sequence": item.get("sequence"),
            "values": dict(item["values"]),
        }
        if entry["user"] is not None and entry["user"] not in user_ids:
            entry.update(
                user=None, reason=REASON_USER_REMOVED, kind=None, learned=False
            )
        log.append(entry)
    return log[-MEASUREMENT_LOG_SIZE:]


async def async_setup_users(
    hass: HomeAssistant, entry: OmronConfigEntry, *, is_scale: bool
) -> OmronUsers | None:
    """Load the users of a device; None if it has none registered.

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
    users = OmronUsers(
        hass,
        entry.entry_id,
        strategy_for(entry, is_scale=is_scale),
        multi_slot=_device_config(entry).num_users > 1,
    )
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


def slot_users(entry: OmronConfigEntry | Any) -> list[SlotUser]:
    members = []
    for user_id, subentry in user_subentries(entry).items():
        slot = subentry.data.get(CONF_USER_SLOT)
        if isinstance(slot, bool) or not isinstance(slot, int):
            continue
        members.append(
            SlotUser(
                user_id=user_id,
                name=subentry.data.get(CONF_USER_NAME) or subentry.title,
                slot=slot,
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


def _last_values(
    record: dict[str, Any], assignment: Assignment, keys: tuple[str, ...]
) -> dict[str, Any]:
    values: dict[str, Any] = {key: record.get(key) for key in keys}
    values["measured_at"] = record.get("measured_at")
    values["assignment"] = assignment.kind
    return values


def _newest_by_slot(users: OmronUsers, device_data: Any) -> dict[int, dict[str, Any]]:
    """The newest EEPROM record of each slot the last readout decoded."""
    if users.multi_slot:
        return dict(device_data.last_records)
    record = device_data.last_record
    return {1: record} if record is not None else {}


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
    Everything is kept per device slot: a single-slot device has slot 1, a
    multi-slot scale one baseline and one sequence for each of its slots.

    Order: assign, fire the events, save. A restart in between repeats an
    event rather than losing one.
    """
    runtime = entry.runtime_data
    users = runtime.users
    if users is None:
        return
    device_data = runtime.device_data
    pending = device_data.pending_history
    sync_on = device_data.history_last_seq is not None
    before = users.as_dict()
    is_scale = runtime.is_scale

    to_assign: list[tuple[int, dict[str, Any]]] = []
    for slot, record in sorted(_newest_by_slot(users, device_data).items()):
        fingerprint = record_fingerprint(record)
        if fingerprint is None:
            # Nothing was read (or nothing usable): leave this slot's baseline
            # as it is and hand out nothing.
            continue
        if slot not in users.fingerprints:
            # First poll since the first user was added: whatever the device
            # still holds is older than the registration, so only note where
            # it stands.
            if sync_on and pending is not None:
                _set_last_seq(users, slot, pending["next_seq"].get(slot))
        elif sync_on:
            if pending is not None:
                if pending.get("rebased", {}).get(slot):
                    # The device's counter went back: the old sequence means
                    # nothing.
                    _set_last_seq(users, slot, pending["next_seq"].get(slot))
                events = [e for e in pending["events"] if e.get("user", slot) == slot]
                to_assign.extend(
                    (slot, event)
                    for event in order_events(events, users.last_seq.get(slot))
                )
        else:
            # Without the sync the device has no usable sequence, and one left
            # from an earlier sync would stop a later one from starting clean.
            users.last_seq.pop(slot, None)
            if fingerprint != users.fingerprints[slot]:
                to_assign.append((slot, record))
        users.fingerprints[slot] = fingerprint

    strategy = users.strategy
    value_keys = _value_keys(runtime.measurement_kind)
    tolerance = float(
        entry.options.get(CONF_WEIGHT_TOLERANCE_PCT, WEIGHT_TOLERANCE_PCT_DEFAULT)
    )
    for slot, measurement in to_assign:
        if sync_on and isinstance(measurement.get("sequence"), int):
            users.last_seq[slot] = measurement["sequence"]
        if not _usable(measurement, is_scale=is_scale):
            _LOGGER.debug("Skipping a record without usable values: %s", measurement)
            continue
        if strategy == STRATEGY_SLOT:
            assignment = assign_by_slot(slot, slot_users(entry))
            if assignment.reason == REASON_SLOT_UNCLAIMED:
                # Nobody wants this slot: left alone, not announced.
                continue
        else:
            # Linked people who were not home when it was taken are not
            # candidates, whatever the strategy.
            away = await _async_away_users(hass, entry, measurement)
            away_ids = {member["user_id"] for member in away}
            if strategy == STRATEGY_WEIGHT:
                everyone = weight_users(users, entry)
            else:
                everyone = rule_users(entry)
            present = [member for member in everyone if member.user_id not in away_ids]
            if everyone and not present:
                assignment = Assignment(
                    None, reason=REASON_NOBODY_HOME, candidates=away
                )
            elif strategy == STRATEGY_WEIGHT:
                assignment = assign_by_weight(
                    float(measurement["weight"]), present, tolerance
                )
            else:
                assignment = assign_by_rules(measurement, present)
        learned = assignment.kind in (KIND_MATCHED, KIND_NEAREST)
        logged = users.log_measurement(
            measurement, slot, assignment, value_keys, learned=learned
        )
        if assignment.user_id is None:
            hass.bus.async_fire(
                EVENT_UNASSIGNED_MEASUREMENT,
                _unassigned_event(
                    hass, entry, measurement, assignment, value_keys, logged["id"]
                ),
            )
            continue
        user_id = assignment.user_id
        if learned:
            users.recent[user_id] = remember_weight(
                users.recent.get(user_id, []), float(measurement["weight"])
            )
        users.last[user_id] = {
            **_last_values(measurement, assignment, value_keys),
            "measurement_id": logged["id"],
        }
        async_dispatcher_send(hass, signal_user_updated(entry.entry_id, user_id))
        # Each record gets its own state change, as the stored-record sync does.
        await asyncio.sleep(0)

    if users.as_dict() != before:
        await users.async_save()


def _measured_moment(measurement: dict[str, Any]) -> dt.datetime | None:
    """When it was taken, with a time zone; None if the record has no usable time."""
    value = measurement.get("measured_at")
    moment = dt_util.parse_datetime(value) if isinstance(value, str) else None
    if moment is None or moment.tzinfo is None:
        return None
    return dt_util.as_utc(moment)


def _state_at(hass: HomeAssistant, entity_id: str, moment: dt.datetime) -> str | None:
    """The recorded state of an entity at a moment (runs in the recorder's executor).

    The recorder reports the state in force at a start time first, then the
    changes after it; it counts a change at the start time itself as before it
    only if the start is a hair later, hence the millisecond.
    """
    from homeassistant.components.recorder import history

    start = moment + dt.timedelta(milliseconds=1)
    recorded = history.get_significant_states(
        hass,
        start,
        start + dt.timedelta(seconds=1),
        [entity_id],
        include_start_time_state=True,
        significant_changes_only=False,
        no_attributes=True,
    )
    states = recorded.get(entity_id)
    if not states:
        return None
    first = states[0]
    # A first state that only began after the start means nothing was recorded
    # for that moment.
    if first.last_updated > start:
        return None
    return first.state


async def _async_person_state_at(
    hass: HomeAssistant, entity_id: str, moment: dt.datetime
) -> str | None:
    """What the recorder knows of an entity's state at a moment, else None.

    None when the recorder is not loaded, has no state for that moment (never
    recorded, or purged), or cannot be asked: the person then does not count as
    away.
    """
    if "recorder" not in hass.config.components:
        return None
    try:
        from homeassistant.components.recorder import get_instance

        return await get_instance(hass).async_add_executor_job(
            _state_at, hass, entity_id, moment
        )
    except Exception:
        _LOGGER.debug(
            "Could not look up the state of %s at %s", entity_id, moment, exc_info=True
        )
        return None


async def _async_away_users(
    hass: HomeAssistant, entry: OmronConfigEntry, measurement: dict[str, Any]
) -> list[dict[str, Any]]:
    """Users whose linked person was recorded as not home when it was taken.

    Only people linked to a user are asked, and only for a measurement that has
    a time. Unknown or unavailable counts as no information, not as away.
    """
    moment = _measured_moment(measurement)
    if moment is None:
        return []
    away = []
    for user_id, subentry in user_subentries(entry).items():
        person = subentry.data.get(CONF_USER_PERSON)
        if not person:
            continue
        state = await _async_person_state_at(hass, person, moment)
        if state is None or state in (STATE_HOME, STATE_UNKNOWN, STATE_UNAVAILABLE):
            continue
        away.append(
            {
                "user_id": user_id,
                "name": subentry.data.get(CONF_USER_NAME) or subentry.title,
                "person": person,
                "state": state,
            }
        )
    return away


def _set_last_seq(users: OmronUsers, slot: int, sequence: int | None) -> None:
    if sequence is None:
        users.last_seq.pop(slot, None)
    else:
        users.last_seq[slot] = sequence


def _unassigned_event(
    hass: HomeAssistant,
    entry: OmronConfigEntry,
    measurement: dict[str, Any],
    assignment: Assignment,
    value_keys: tuple[str, ...],
    measurement_id: str,
) -> dict[str, Any]:
    device = async_entry_device(hass, entry)
    return {
        "device_id": device.id if device else None,
        "address": entry.runtime_data.address,
        "measurement_id": measurement_id,
        "measured_at": measurement.get("measured_at"),
        "device_time": measurement.get("device_time"),
        "sequence": measurement.get("sequence"),
        **{key: measurement.get(key) for key in value_keys},
        "reason": assignment.reason,
        "candidates": assignment.candidates,
    }
