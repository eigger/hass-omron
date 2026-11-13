"""Users linked to a person: someone who was away is not a candidate.

The person's state is looked up in the recorder at the time the device says the
measurement was taken. Without a person nothing changes.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.omron.const import (
    EVENT_UNASSIGNED_MEASUREMENT,
    SUBENTRY_USER,
)

from .user_harness import (
    add_entry,
    bp_event,
    poll,
    scale_event,
    sensor_state,
    setup_entry,
    user,
)

LOOKUP = "custom_components.omron.users._async_person_state_at"

# 71 kg is between 70 and 72: with both as candidates it is too close to call.
TWINS = [
    user("alice", "Alice", expected_weight=70.0, person="person.alice"),
    user("bob", "Bob", expected_weight=72.0, person="person.bob"),
]


def _states(**states: str | None):
    async def lookup(hass, entity_id, moment):
        return states.get(entity_id.removeprefix("person."))

    return AsyncMock(side_effect=lookup)


async def _measure(hass, entry, lookup, event=None, *, patched=True):
    await poll(hass, entry, scale_event("2026-09-20T07:00:00", 60.0))  # baseline
    event = event or scale_event("2026-09-21T07:30:00", 71.0)
    if patched:
        with patch(LOOKUP, lookup):
            await poll(hass, entry, event)
    else:
        await poll(hass, entry, event)


async def test_without_a_person_nobody_is_asked_and_nothing_changes(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    unassigned = async_capture_events(hass, EVENT_UNASSIGNED_MEASUREMENT)
    entry = add_entry(
        hass,
        "HN-300T2",
        [
            user("alice", "Alice", expected_weight=70.0),
            user("bob", "Bob", expected_weight=72.0),
        ],
    )
    await setup_entry(hass, entry)
    lookup = _states()
    await _measure(hass, entry, lookup)
    lookup.assert_not_called()
    assert [e.data["reason"] for e in unassigned] == ["ambiguous"]


async def test_a_person_who_was_home_stays_a_candidate(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    unassigned = async_capture_events(hass, EVENT_UNASSIGNED_MEASUREMENT)
    entry = add_entry(hass, "HN-300T2", TWINS)
    await setup_entry(hass, entry)
    lookup = _states(alice="home", bob="home")
    await _measure(hass, entry, lookup)
    assert [e.data["reason"] for e in unassigned] == ["ambiguous"]
    # Asked at the time of the measurement, in UTC, once per linked person.
    moments = [call.args[2] for call in lookup.await_args_list]
    assert len(moments) == 2
    assert moments[0] == dt.datetime(2026, 9, 21, 7, 30, tzinfo=dt.UTC)


async def test_a_person_who_was_away_is_not_a_candidate(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    unassigned = async_capture_events(hass, EVENT_UNASSIGNED_MEASUREMENT)
    entry = add_entry(hass, "HN-300T2", TWINS)
    await setup_entry(hass, entry)
    await _measure(hass, entry, _states(alice="home", bob="not_home"))
    # Alice alone: 71 is within her tolerance, and nobody is close behind.
    assert sensor_state(hass, entry, "alice", "weight").state == "71.0"
    assert sensor_state(hass, entry, "bob", "weight").state == "unknown"
    assert not unassigned


async def test_any_state_but_home_counts_as_away(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, "HN-300T2", TWINS)
    await setup_entry(hass, entry)
    await _measure(hass, entry, _states(alice="work", bob="home"))
    assert sensor_state(hass, entry, "bob", "weight").state == "71.0"
    assert sensor_state(hass, entry, "alice", "weight").state == "unknown"


async def test_no_recorded_state_keeps_the_user_a_candidate(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    unassigned = async_capture_events(hass, EVENT_UNASSIGNED_MEASUREMENT)
    entry = add_entry(hass, "HN-300T2", TWINS)
    await setup_entry(hass, entry)
    await _measure(hass, entry, _states(alice=None, bob=None))
    assert [e.data["reason"] for e in unassigned] == ["ambiguous"]


async def test_unknown_and_unavailable_are_no_information(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    unassigned = async_capture_events(hass, EVENT_UNASSIGNED_MEASUREMENT)
    entry = add_entry(hass, "HN-300T2", TWINS)
    await setup_entry(hass, entry)
    await _measure(hass, entry, _states(alice="unavailable", bob="unknown"))
    assert [e.data["reason"] for e in unassigned] == ["ambiguous"]


async def test_a_measurement_without_a_time_is_not_filtered(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    unassigned = async_capture_events(hass, EVENT_UNASSIGNED_MEASUREMENT)
    entry = add_entry(hass, "HN-300T2", TWINS)
    await setup_entry(hass, entry)
    lookup = _states(alice="home", bob="not_home")
    await _measure(hass, entry, lookup, scale_event(None, 71.0))
    lookup.assert_not_called()
    assert [e.data["reason"] for e in unassigned] == ["ambiguous"]


async def test_without_the_recorder_nobody_is_filtered(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    assert "recorder" not in hass.config.components
    unassigned = async_capture_events(hass, EVENT_UNASSIGNED_MEASUREMENT)
    entry = add_entry(hass, "HN-300T2", TWINS)
    await setup_entry(hass, entry)
    await _measure(hass, entry, None, patched=False)
    assert [e.data["reason"] for e in unassigned] == ["ambiguous"]


async def test_a_failing_lookup_is_no_information(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    unassigned = async_capture_events(hass, EVENT_UNASSIGNED_MEASUREMENT)
    entry = add_entry(hass, "HN-300T2", TWINS)
    await setup_entry(hass, entry)
    # The recorder is said to be loaded but there is none to ask.
    hass.config.components.add("recorder")
    await _measure(hass, entry, None, patched=False)
    assert [e.data["reason"] for e in unassigned] == ["ambiguous"]


async def test_when_everyone_was_away_nobody_gets_it_and_it_is_announced(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    unassigned = async_capture_events(hass, EVENT_UNASSIGNED_MEASUREMENT)
    entry = add_entry(hass, "HN-300T2", TWINS)
    await setup_entry(hass, entry)
    await _measure(hass, entry, _states(alice="not_home", bob="work"))
    (event,) = unassigned
    assert event.data["reason"] == "nobody_home"
    assert event.data["weight"] == 71.0 and event.data["measurement_id"]
    assert event.data["candidates"] == [
        {
            "user_id": "alice",
            "name": "Alice",
            "person": "person.alice",
            "state": "not_home",
        },
        {"user_id": "bob", "name": "Bob", "person": "person.bob", "state": "work"},
    ]
    logged = entry.runtime_data.users.find_logged(event.data["measurement_id"])
    assert logged["user"] is None and logged["reason"] == "nobody_home"
    assert sensor_state(hass, entry, "alice", "weight").state == "unknown"


async def test_a_user_without_a_person_is_never_removed(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(
        hass,
        "HN-300T2",
        [
            user("alice", "Alice", expected_weight=70.0),
            user("bob", "Bob", expected_weight=72.0, person="person.bob"),
        ],
    )
    await setup_entry(hass, entry)
    lookup = _states(bob="not_home")
    await _measure(hass, entry, lookup)
    # Only Bob is asked about; with him out Alice is the one left.
    assert [call.args[1] for call in lookup.await_args_list] == ["person.bob"]
    assert sensor_state(hass, entry, "alice", "weight").state == "71.0"


async def test_the_filter_works_for_the_rule_strategy_of_a_cuff_too(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(
        hass,
        "HEM-7600T",
        [
            user("bob", "Bob", time_windows="06:00-09:00", person="person.bob"),
            user("eve", "Eve", time_windows="06:00-09:00", person="person.eve"),
        ],
    )
    await setup_entry(hass, entry)
    await poll(hass, entry, bp_event("2026-09-20T07:00:00"))
    with patch(LOOKUP, _states(bob="home", eve="not_home")):
        await poll(hass, entry, bp_event("2026-09-21T07:30:00", 131, 84))
    assert sensor_state(hass, entry, "bob", "systolic").state == "131"
    assert sensor_state(hass, entry, "eve", "systolic").state == "unknown"


# --- the real recorder -------------------------------------------------------------------


@pytest.fixture
def recorder_db_url() -> str:
    """An in-memory database. The plugin's own fixture refuses to run after
    ``hass`` exists, which the custom-integration autouse fixture makes so."""
    return "sqlite://"


async def test_the_state_is_the_one_the_recorder_has_for_that_moment(
    recorder_mock, hass: HomeAssistant, enable_bluetooth: None, freezer
) -> None:
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )

    from custom_components.omron.users import _async_person_state_at

    freezer.move_to("2026-09-21 06:00:00+00:00")
    hass.states.async_set("person.bob", "home")
    freezer.move_to("2026-09-21 09:00:00+00:00")
    hass.states.async_set("person.bob", "not_home")
    freezer.move_to("2026-09-21 12:00:00+00:00")
    await async_wait_recording_done(hass)

    async def at(hour: int, minute: int = 0) -> str | None:
        return await _async_person_state_at(
            hass,
            "person.bob",
            dt.datetime(2026, 9, 21, hour, minute, tzinfo=dt.UTC),
        )

    assert await at(7, 30) == "home"
    assert await at(9, 0) == "not_home"
    assert await at(10, 0) == "not_home"
    # Before anything was recorded there is no state for that moment.
    assert await at(5, 0) is None
    # A person nobody recorded.
    assert (
        await _async_person_state_at(
            hass, "person.nobody", dt.datetime(2026, 9, 21, 7, tzinfo=dt.UTC)
        )
        is None
    )


async def test_through_the_real_recorder_an_away_person_is_left_out(
    recorder_mock, hass: HomeAssistant, enable_bluetooth: None, freezer
) -> None:
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )

    freezer.move_to("2026-09-21 06:00:00+00:00")
    hass.states.async_set("person.alice", "home")
    hass.states.async_set("person.bob", "not_home")
    freezer.move_to("2026-09-21 12:00:00+00:00")
    await async_wait_recording_done(hass)
    entry = add_entry(hass, "HN-300T2", TWINS)
    await setup_entry(hass, entry)
    await _measure(hass, entry, None, patched=False)
    assert sensor_state(hass, entry, "alice", "weight").state == "71.0"


# --- the form -------------------------------------------------------------------------------


async def test_the_person_is_an_optional_field_and_is_kept(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = add_entry(hass, "HN-300T2", [])
    await setup_entry(hass, entry)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_USER), context={"source": "user"}
    )
    key = next(k for k in result["data_schema"].schema if str(k) == "person")
    assert not isinstance(key.default, str)  # optional, no default
    created = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Alice", "expected_weight": 62.0, "person": "person.alice"},
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    (subentry,) = entry.subentries.values()
    assert subentry.data["person"] == "person.alice"
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_the_recorder_is_an_after_dependency_only() -> None:
    import json
    from pathlib import Path

    manifest = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "custom_components"
            / "omron"
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["after_dependencies"] == ["recorder"]
    assert "recorder" not in manifest["dependencies"]
