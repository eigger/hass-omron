"""Each user's "last assignment decision" sensor (#301), through the poll path.

For every handled measurement every user's sensor says whether the user got it
and, if not, why. Only the BLE boundary is replaced.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.omron.assignment import (
    DECISIONS,
    Assignment,
    assign_by_weight,
    user_decision,
    WeightUser,
)

from .test_user_measurements import (
    ALICE_BOB,
    CUFF,
    MORNING_EVENING,
    REGISTERED,
    SCALE,
    SYNC_CUFF,
    _bp,
    _entry,
    _poll,
    _scale,
    _setup,
    _stored,
    _sync_options,
    _user,
)

BASE = "2026-09-20T07:00:00"


def _decision(hass: HomeAssistant, entry, user: str):
    return _sensor_state(hass, entry, user)


def _sensor_state(hass: HomeAssistant, entry, user: str):
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", "omron", f"{entry.entry_id}_{user}_last_decision"
    )
    assert entity_id is not None
    return hass.states.get(entity_id)


def _states(hass: HomeAssistant, entry, *users: str) -> dict[str, str]:
    return {user: _decision(hass, entry, user).state for user in users}


# --- rules ---------------------------------------------------------------------------


async def test_rules_assigned_and_other_user(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    assert _states(hass, entry, "bob", "eve") == {"bob": "unknown", "eve": "unknown"}
    await _poll(hass, entry, _bp(BASE))  # baseline

    await _poll(hass, entry, _bp("2026-09-21T07:30:00", 131, 84))

    bob = _decision(hass, entry, "bob")
    eve = _decision(hass, entry, "eve")
    assert (bob.state, eve.state) == ("assigned", "other_user")
    assert bob.attributes["measured_at"] == "2026-09-21T07:30:00+00:00"
    assert (bob.attributes["sys"], bob.attributes["dia"], bob.attributes["bpm"]) == (
        131,
        84,
        60,
    )
    assert bob.attributes["assigned_to"] == "Bob"
    assert bob.attributes["reason"] is None
    assert (
        bob.attributes["passed"],
        bob.attributes["failed"],
        bob.attributes["unknown"],
    ) == (["time"], [], [])
    assert eve.attributes["assigned_to"] == "Bob"
    assert eve.attributes["failed"] == ["time"]
    assert eve.attributes["passed"] == []
    # Nothing that only means something for the other strategy.
    for attribute in ("reference_kg", "difference_kg", "tolerance_kg", "weight"):
        assert attribute not in bob.attributes


async def test_rules_not_matched_by_time(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    await _poll(hass, entry, _bp(BASE))

    await _poll(hass, entry, _bp("2026-09-21T12:00:00", 131, 84))

    assert _states(hass, entry, "bob", "eve") == {
        "bob": "not_matched",
        "eve": "not_matched",
    }
    bob = _decision(hass, entry, "bob")
    assert bob.attributes["assigned_to"] is None
    assert bob.attributes["reason"] == "no_match"
    assert bob.attributes["failed"] == ["time"]


async def test_rules_not_matched_by_range_and_assigned_by_range(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(
        hass,
        CUFF,
        [_user("low", "Low", sys_max=125), _user("high", "High", sys_min=130)],
    )
    await _setup(hass, entry)
    await _poll(hass, entry, _bp(BASE))

    await _poll(hass, entry, _bp("2026-09-21T07:30:00", 131, 84))
    # Somebody got it: that wins over this user's own failed rule, which the
    # attributes still show.
    assert _states(hass, entry, "low", "high") == {
        "low": "other_user",
        "high": "assigned",
    }
    low = _decision(hass, entry, "low")
    assert low.attributes["failed"] == ["sys"]
    assert low.attributes["assigned_to"] == "High"
    assert low.attributes["reason"] is None

    await _poll(hass, entry, _bp("2026-09-21T08:30:00", 127, 84))
    assert _states(hass, entry, "low", "high") == {
        "low": "not_matched",
        "high": "not_matched",
    }
    assert _decision(hass, entry, "high").attributes["reason"] == "no_match"


async def test_rules_ambiguous(hass: HomeAssistant, enable_bluetooth: None) -> None:
    entry = _entry(
        hass,
        CUFF,
        [
            _user("bob", "Bob", time_windows="06:00-09:00"),
            _user("ann", "Ann", time_windows="07:00-10:00"),
            _user("eve", "Eve", time_windows="18:00-21:00"),
        ],
    )
    await _setup(hass, entry)
    await _poll(hass, entry, _bp(BASE))

    await _poll(hass, entry, _bp("2026-09-21T07:30:00", 131, 84))

    assert _states(hass, entry, "bob", "ann", "eve") == {
        "bob": "ambiguous",
        "ann": "ambiguous",
        "eve": "not_matched",
    }
    bob = _decision(hass, entry, "bob")
    assert bob.attributes["reason"] == "ambiguous"
    assert bob.attributes["assigned_to"] is None
    assert bob.attributes["passed"] == ["time"]


async def test_rules_no_time(hass: HomeAssistant, enable_bluetooth: None) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    await _poll(hass, entry, _bp(BASE))

    await _poll(hass, entry, _bp(None, 131, 84))

    assert _states(hass, entry, "bob", "eve") == {"bob": "no_time", "eve": "no_time"}
    bob = _decision(hass, entry, "bob")
    assert bob.attributes["unknown"] == ["time"]
    assert bob.attributes["reason"] == "no_time"
    assert bob.attributes["measured_at"] is None


async def test_the_users_own_failure_beats_an_unknown_time_of_theirs(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """A rule that failed means no match, whatever else could not be checked."""
    entry = _entry(
        hass,
        CUFF,
        [
            _user("hi", "Hi", time_windows="06:00-09:00", sys_min=200),
            _user("bob", "Bob", time_windows="06:00-09:00"),
            _user("free", "Free", sys_min=100),
        ],
    )
    await _setup(hass, entry)
    await _poll(hass, entry, _bp(BASE))

    await _poll(hass, entry, _bp(None, 131, 84))

    assert _states(hass, entry, "hi", "bob", "free") == {
        "hi": "not_matched",
        "bob": "no_time",
        # Matched, but a user whose time cannot be checked might match too.
        "free": "ambiguous",
    }
    assert _decision(hass, entry, "hi").attributes["unknown"] == ["time"]
    assert _decision(hass, entry, "free").attributes["reason"] == "no_time"


# --- weight --------------------------------------------------------------------------


async def test_weight_assigned_and_other_user(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, SCALE, ALICE_BOB)
    await _setup(hass, entry)
    await _poll(hass, entry, _scale(BASE, 70.0))

    await _poll(hass, entry, _scale("2026-09-21T07:30:00", 62.4))

    alice = _decision(hass, entry, "alice")
    bob = _decision(hass, entry, "bob")
    assert (alice.state, bob.state) == ("assigned", "other_user")
    assert alice.attributes["weight"] == 62.4
    assert alice.attributes["assigned_to"] == "Alice"
    assert bob.attributes["assigned_to"] == "Alice"
    assert alice.attributes["reference_kg"] == 62.0
    assert alice.attributes["difference_kg"] == pytest.approx(0.4)
    assert alice.attributes["tolerance_kg"] == pytest.approx(1.86)
    assert bob.attributes["reference_kg"] == 80.0
    assert bob.attributes["difference_kg"] == pytest.approx(17.6)
    for attribute in ("sys", "dia", "bpm", "passed", "failed", "unknown"):
        assert attribute not in alice.attributes


async def test_weight_not_matched_when_out_of_range(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, SCALE, ALICE_BOB)
    await _setup(hass, entry)
    await _poll(hass, entry, _scale(BASE, 70.0))

    await _poll(hass, entry, _scale("2026-09-21T07:30:00", 71.0))

    assert _states(hass, entry, "alice", "bob") == {
        "alice": "not_matched",
        "bob": "not_matched",
    }
    alice = _decision(hass, entry, "alice")
    assert alice.attributes["reason"] == "out_of_range"
    assert alice.attributes["assigned_to"] is None


async def test_weight_ambiguous_only_for_the_users_that_are_too_close_to_tell(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(
        hass,
        SCALE,
        [
            _user("a", "A", expected_weight=70.0),
            _user("b", "B", expected_weight=71.0),
            _user("c", "C", expected_weight=95.0),
        ],
    )
    await _setup(hass, entry)
    await _poll(hass, entry, _scale(BASE, 50.0))

    await _poll(hass, entry, _scale("2026-09-21T07:30:00", 70.4))

    assert _states(hass, entry, "a", "b", "c") == {
        "a": "ambiguous",
        "b": "ambiguous",
        "c": "not_matched",
    }
    assert _decision(hass, entry, "a").attributes["reason"] == "ambiguous"


def test_the_tied_users_follow_the_strategy_in_every_case() -> None:
    """The decision mapping agrees with ``assign_by_weight`` on who was tied."""
    users = [
        WeightUser("a", "A", 70.0),
        WeightUser("b", "B", 71.0),
        WeightUser("c", "C", 72.5),
        WeightUser("d", "D", 95.0),
    ]
    for tenth in range(600, 1000, 3):
        result = assign_by_weight(tenth / 10, users, 3.0)
        states = {
            user.user_id: user_decision(result, user.user_id, by_weight=True)[0]
            for user in users
        }
        if result.reason == "ambiguous":
            assert "ambiguous" in states.values()
            assert states["d"] != "assigned"
        elif result.user_id is None:
            assert set(states.values()) == {"not_matched"}
        else:
            assert sorted(states.values()).count("assigned") == 1
            assert set(states.values()) <= {"assigned", "other_user"}


def test_a_user_without_a_candidate_entry_is_not_matched() -> None:
    result = Assignment(None, reason="no_match", candidates=[])
    assert user_decision(result, "x", by_weight=True) == ("not_matched", {})
    assert user_decision(result, "x", by_weight=False) == ("not_matched", {})


def test_every_state_is_listed_once() -> None:
    assert len(set(DECISIONS)) == len(DECISIONS) == 7


# --- left over and skipped ---------------------------------------------------------------


async def test_left_over_on_the_first_poll(
    hass: HomeAssistant, enable_bluetooth: None, freezer, hass_storage: dict
) -> None:
    freezer.move_to(REGISTERED)
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)

    await _poll(hass, entry, _bp("2026-09-20T07:30:00", 131, 84))

    assert _states(hass, entry, "bob", "eve") == {
        "bob": "left_over",
        "eve": "left_over",
    }
    bob = _decision(hass, entry, "bob")
    assert bob.attributes["measured_at"] == "2026-09-20T07:30:00+00:00"
    assert bob.attributes["sys"] == 131
    assert bob.attributes["assigned_to"] is None
    assert bob.attributes["reason"] is None
    for attribute in ("passed", "failed", "unknown", "reference_kg"):
        assert attribute not in bob.attributes
    assert _stored(hass_storage, entry)["users"]["bob"]["decision"]["state"] == (
        "left_over"
    )
    # The value sensors still only show assigned measurements.
    assert hass.states.get("sensor.bob_systolic").state == "unknown"


async def test_left_over_for_a_scale_and_a_record_without_a_time(
    hass: HomeAssistant, enable_bluetooth: None, freezer
) -> None:
    freezer.move_to(REGISTERED)
    entry = _entry(hass, SCALE, ALICE_BOB)
    await _setup(hass, entry)

    await _poll(hass, entry, _scale(None, 62.4))

    alice = _decision(hass, entry, "alice")
    assert alice.state == "left_over"
    assert alice.attributes["weight"] == 62.4
    assert alice.attributes["measured_at"] is None


async def test_synced_records_left_over_then_assigned_on_the_first_poll(
    hass: HomeAssistant, enable_bluetooth: None, freezer
) -> None:
    freezer.move_to(REGISTERED)
    entry = _entry(hass, SYNC_CUFF, MORNING_EVENING, _sync_options())
    await _setup(hass, entry)
    seen = _record_states(hass, entry, "bob")

    before = _bp("2026-09-21T06:30:00", 140, 90, seq=5)
    after = _bp("2026-09-21T07:30:00", 131, 84, seq=6)
    freezer.move_to("2026-09-21T20:00:00+00:00")
    await _poll(hass, entry, after, events=[before, after], next_seq=6)

    assert seen == ["left_over", "assigned"]


async def test_skipped_when_the_record_has_no_usable_values(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    await _poll(hass, entry, _bp(BASE))

    partial = _bp("2026-09-21T07:30:00", 131, 84)
    partial["dia"] = None
    await _poll(hass, entry, partial)

    assert _states(hass, entry, "bob", "eve") == {"bob": "skipped", "eve": "skipped"}
    bob = _decision(hass, entry, "bob")
    assert bob.attributes["sys"] == 131
    assert bob.attributes["dia"] is None
    assert bob.attributes["assigned_to"] is None
    assert "passed" not in bob.attributes


# --- every user, every measurement ---------------------------------------------------------


def _record_states(hass: HomeAssistant, entry, user: str) -> list[str]:
    entity_id = _decision(hass, entry, user).entity_id
    seen: list[str] = []
    hass.bus.async_listen(
        "state_changed",
        lambda ev: (
            seen.append(ev.data["new_state"].state)
            if ev.data["entity_id"] == entity_id and ev.data["new_state"]
            else None
        ),
    )
    return seen


async def test_every_user_follows_every_measurement(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    await _poll(hass, entry, _bp(BASE))

    await _poll(hass, entry, _bp("2026-09-21T12:00:00", 131, 84))
    assert _decision(hass, entry, "eve").attributes["measured_at"] == (
        "2026-09-21T12:00:00+00:00"
    )
    await _poll(hass, entry, _bp("2026-09-21T13:00:00", 125, 80))
    # The state is the same, the record is the new one, for both users.
    for user in ("bob", "eve"):
        decision = _decision(hass, entry, user)
        assert decision.state == "not_matched"
        assert decision.attributes["measured_at"] == "2026-09-21T13:00:00+00:00"
        assert decision.attributes["sys"] == 125


async def test_a_poll_without_a_new_record_changes_nothing(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    await _poll(hass, entry, _bp(BASE))
    await _poll(hass, entry, _bp("2026-09-21T07:30:00", 131, 84))
    before = {
        user: (
            _decision(hass, entry, user).state,
            _decision(hass, entry, user).attributes,
        )
        for user in ("bob", "eve")
    }
    stored = repr(_stored(hass_storage, entry))
    seen = _record_states(hass, entry, "bob")

    await _poll(hass, entry, _bp("2026-09-21T07:30:00", 131, 84))
    await _poll(hass, entry, None)

    assert not seen
    assert {
        user: (
            _decision(hass, entry, user).state,
            _decision(hass, entry, user).attributes,
        )
        for user in ("bob", "eve")
    } == before
    assert repr(_stored(hass_storage, entry)) == stored


async def test_sync_mode_updates_the_sensors_for_each_record_in_turn(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, SYNC_CUFF, MORNING_EVENING, _sync_options())
    await _setup(hass, entry)
    await _poll(hass, entry, _bp(BASE), events=[], next_seq=5)
    seen = _record_states(hass, entry, "bob")
    seen_eve = _record_states(hass, entry, "eve")
    measured: list[Any] = []
    entity_id = _decision(hass, entry, "bob").entity_id
    hass.bus.async_listen(
        "state_changed",
        lambda ev: (
            measured.append(ev.data["new_state"].attributes.get("measured_at"))
            if ev.data["entity_id"] == entity_id and ev.data["new_state"]
            else None
        ),
    )

    first = _bp("2026-09-21T07:00:00", 130, 80, seq=6)
    second = _bp("2026-09-21T12:00:00", 135, 85, seq=7)
    third = _bp("2026-09-21T19:30:00", 140, 90, seq=8)
    await _poll(hass, entry, third, events=[third, first, second], next_seq=8)

    assert seen == ["assigned", "not_matched", "other_user"]
    assert seen_eve == ["other_user", "not_matched", "assigned"]
    assert measured == [
        "2026-09-21T07:00:00+00:00",
        "2026-09-21T12:00:00+00:00",
        "2026-09-21T19:30:00+00:00",
    ]
    # Ends on the last record.
    assert _decision(hass, entry, "bob").attributes["sys"] == 140


# --- persistence -----------------------------------------------------------------------------


async def test_the_decision_comes_back_after_a_restart(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    await _poll(hass, entry, _bp(BASE))
    await _poll(hass, entry, _bp("2026-09-21T12:00:00", 131, 84))
    before = {user: _decision(hass, entry, user).attributes for user in ("bob", "eve")}
    stored = _stored(hass_storage, entry)["users"]["eve"]["decision"]
    assert stored["state"] == "not_matched"
    assert stored["reason"] == "no_match"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert _states(hass, entry, "bob", "eve") == {
        "bob": "not_matched",
        "eve": "not_matched",
    }
    assert {
        user: _decision(hass, entry, user).attributes for user in ("bob", "eve")
    } == before


async def test_an_old_store_without_decisions_shows_unknown(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    hass_storage[f"omron.users_{entry.entry_id}"] = {
        "version": 1,
        "key": f"omron.users_{entry.entry_id}",
        "data": {
            "strategy": "rules",
            "fingerprint": ["2026-09-21T07:30:00", 131, 84, 60],
            "last_seq": None,
            "users": {
                "bob": {
                    "recent": [],
                    "last": {
                        "sys": 131,
                        "measured_at": "2026-09-21T07:30:00+00:00",
                        "assignment": "rule",
                    },
                }
            },
        },
    }
    await _setup(hass, entry)

    for user in ("bob", "eve"):
        decision = _decision(hass, entry, user)
        assert decision.state == "unknown"
        assert "assigned_to" not in decision.attributes
    assert hass.states.get("sensor.bob_systolic").state == "131"

    # The next measurement fills it in.
    await _poll(hass, entry, _bp("2026-09-22T07:30:00", 120, 80))
    assert _decision(hass, entry, "bob").state == "assigned"


async def test_a_garbled_stored_decision_is_ignored(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    hass_storage[f"omron.users_{entry.entry_id}"] = {
        "version": 1,
        "key": f"omron.users_{entry.entry_id}",
        "data": {
            "strategy": "rules",
            "fingerprint": ["2026-09-21T07:30:00", 131, 84, 60],
            "users": {
                "bob": {"recent": [], "last": {}, "decision": {"state": "nonsense"}},
                "eve": {"recent": [], "last": {}, "decision": "assigned"},
            },
        },
    }
    await _setup(hass, entry)

    assert _states(hass, entry, "bob", "eve") == {"bob": "unknown", "eve": "unknown"}


# --- the entity ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "users"), [(CUFF, MORNING_EVENING), (SCALE, ALICE_BOB)]
)
async def test_the_sensor_is_a_diagnostic_enum_on_the_users_device(
    hass: HomeAssistant, enable_bluetooth: None, model: str, users: list
) -> None:
    entry = _entry(hass, model, users)
    await _setup(hass, entry)
    registry = er.async_get(hass)
    first = users[0]["subentry_id"]
    entity_id = registry.async_get_entity_id(
        "sensor", "omron", f"{entry.entry_id}_{first}_last_decision"
    )
    item = registry.async_get(entity_id)

    assert item.entity_category is er.EntityCategory.DIAGNOSTIC
    assert item.translation_key == "user_last_decision"
    assert item.disabled_by is None
    assert item.original_device_class == "enum"
    assert item.capabilities["options"] == list(DECISIONS)
    assert item.config_subentry_id == first
    # On the user's own sub-device, not the device's.
    own = registry.async_get(
        registry.async_get_entity_id(
            "sensor",
            "omron",
            f"{entry.entry_id}_{first}_" + ("weight" if model == SCALE else "systolic"),
        )
    )
    assert item.device_id == own.device_id
