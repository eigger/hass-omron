"""Users of a single-slot device: the subentry flow, sub-devices and sensors (#301)."""

from __future__ import annotations

from typing import Any

import pytest
import voluptuous as vol
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, section
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.omron.assignment import parse_windows
from custom_components.omron.config_flow import (
    OmronConfigFlow,
    OmronUserSubentryFlow,
)
from custom_components.omron.const import (
    CONF_ASSIGNMENT_STRATEGY,
    CONF_DEVICE_MODEL,
    CONF_WEIGHT_TOLERANCE_PCT,
    DOMAIN,
    STRATEGY_RULES,
    STRATEGY_WEIGHT,
    SUBENTRY_USER,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"
SCALE = "HN-300T2"
CUFF = "HEM-7600T"
MULTI_SLOT_CUFF = "HEM-7155T"


def _subentry(subentry_id: str, name: str, **data: Any) -> dict[str, Any]:
    return {
        "subentry_id": subentry_id,
        "subentry_type": SUBENTRY_USER,
        "title": name,
        "unique_id": None,
        "data": {"name": name, **data},
    }


def _form_input(
    name: str,
    *windows: str,
    ranges: dict[str, Any] | None = None,
    **top: Any,
) -> dict[str, Any]:
    """What the rules and pressure forms submit: the pickers and ranges in sections.

    ``windows`` are "HH:MM-HH:MM" texts, at most two, put into the pickers.
    """
    when: dict[str, str] = {}
    for number, window in enumerate(windows, start=1):
        start, end = window.split("-")
        when[f"window{number}_start"] = f"{start}:00"
        when[f"window{number}_end"] = f"{end}:00"
    return {"name": name, **top, "when": when, "usual_range": dict(ranges or {})}


def _sections(schema: vol.Schema) -> dict[str, section]:
    return {
        str(key): value
        for key, value in schema.schema.items()
        if isinstance(value, section)
    }


def _suggested(schema: vol.Schema) -> dict[str, Any]:
    """The suggested values of a form, with the sections' ones nested."""
    out: dict[str, Any] = {}
    for key, value in schema.schema.items():
        if isinstance(value, section):
            out[str(key)] = _suggested(value.schema)
        elif key.description:
            out[str(key)] = key.description["suggested_value"]
    return out


def _entry(
    hass: HomeAssistant,
    model: str,
    *,
    users: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title=f"{model} EEFF",
        data={CONF_DEVICE_MODEL: model},
        options=options or {},
        subentries_data=users or [],
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    # A subentry change reloads the entry in the background; let it finish.
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _start(hass: HomeAssistant, entry: MockConfigEntry, source: str = "user"):
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_USER), context={"source": source}
    )


def _device(
    hass: HomeAssistant, entry: MockConfigEntry, *, identifier=None, connection=None
):
    """A device of the entry, by identifier or connection, on any supported HA."""
    registry = dr.async_get(hass)
    if hasattr(registry, "async_get_device_by_identifier"):
        if identifier is not None:
            return registry.async_get_device_by_identifier(identifier, entry.entry_id)
        return registry.async_get_device_by_connection(connection, entry.entry_id)
    if identifier is not None:
        return registry.async_get_device(identifiers={identifier})
    return registry.async_get_device(connections={connection})


def _subentries_of(device, entry: MockConfigEntry) -> set[str]:
    if hasattr(device, "config_subentry_id"):
        return {device.config_subentry_id}
    return device.config_entries_subentries[entry.entry_id]


def _unique_ids(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    return {
        e.unique_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }


# --- which devices offer users -----------------------------------------------------


@pytest.mark.parametrize(
    ("model", "offered"),
    [
        (SCALE, True),
        (CUFF, True),
        (MULTI_SLOT_CUFF, False),
        # Body-composition scales are out of scope.
        ("HBF-222T", False),
    ],
)
async def test_only_single_slot_devices_offer_users(
    hass: HomeAssistant, enable_bluetooth: None, model: str, offered: bool
) -> None:
    entry = _entry(hass, model)
    supported = OmronConfigFlow.async_get_supported_subentry_types(entry)
    assert supported == ({SUBENTRY_USER: OmronUserSubentryFlow} if offered else {})


# --- the flow: the fields follow the strategy ----------------------------------------


async def test_a_scale_by_weight_asks_for_a_name_and_an_expected_weight(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, SCALE)
    await _setup(hass, entry)

    result = await _start(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "weight"
    fields = {str(key): key for key in result["data_schema"].schema}
    assert set(fields) == {"name", "expected_weight", "height_cm"}
    assert isinstance(fields["expected_weight"], vol.Required)
    assert isinstance(fields["name"], vol.Required)
    assert isinstance(fields["height_cm"], vol.Optional)

    created = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Alice", "expected_weight": 62.5, "height_cm": 168},
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    (subentry,) = entry.subentries.values()
    assert subentry.subentry_type == SUBENTRY_USER
    assert subentry.title == "Alice"
    assert dict(subentry.data) == {
        "name": "Alice",
        "expected_weight": 62.5,
        "height_cm": 168,
    }
    await _unload(hass, entry)


async def test_a_scale_by_rules_asks_for_windows_and_a_weight_range(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, SCALE, options={CONF_ASSIGNMENT_STRATEGY: STRATEGY_RULES})
    await _setup(hass, entry)

    result = await _start(hass, entry)
    assert result["step_id"] == "rules"
    schema = result["data_schema"]
    # The name and the height stay at the top; the rest is in two sections.
    assert {str(key) for key in schema.schema} == {
        "name",
        "height_cm",
        "when",
        "usual_range",
    }
    sections = _sections(schema)
    assert {str(k) for k in sections["when"].schema.schema} == {
        "window1_start",
        "window1_end",
        "window2_start",
        "window2_end",
    }
    assert {str(k) for k in sections["usual_range"].schema.schema} == {
        "weight_min",
        "weight_max",
    }
    await _unload(hass, entry)


async def test_the_time_section_is_open_and_the_range_section_collapsed(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF)
    await _setup(hass, entry)
    result = await _start(hass, entry)
    sections = _sections(result["data_schema"])
    assert sections["when"].options["collapsed"] is False
    assert sections["usual_range"].options["collapsed"] is True
    await _unload(hass, entry)


async def test_a_cuff_asks_for_windows_and_pressure_ranges(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF)
    await _setup(hass, entry)

    result = await _start(hass, entry)
    assert result["step_id"] == "pressure"
    schema = result["data_schema"]
    assert {str(key) for key in schema.schema} == {"name", "when", "usual_range"}
    sections = _sections(schema)
    assert {str(k) for k in sections["usual_range"].schema.schema} == {
        "sys_min",
        "sys_max",
        "dia_min",
        "dia_max",
    }
    # The description has a placeholder for the stored windows the form cannot
    # show; there are none for a new user.
    assert result["description_placeholders"] == {"extra_windows": ""}
    created = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        _form_input("Bob", "06:00-09:00", ranges={"sys_min": 130}),
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    (subentry,) = entry.subentries.values()
    # The sections are folded back into the flat data of before.
    assert dict(subentry.data) == {
        "name": "Bob",
        "time_windows": "06:00-09:00",
        "sys_min": 130,
    }
    await _unload(hass, entry)


async def test_picked_windows_are_stored_as_text_without_seconds(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF)
    await _setup(hass, entry)
    result = await _start(hass, entry)
    user_input = _form_input("Bob", "06:00-08:00", "20:00-22:30")
    # Seconds the picker may carry are dropped.
    user_input["when"]["window1_start"] = "06:00:45"
    created = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    (subentry,) = entry.subentries.values()
    assert dict(subentry.data) == {
        "name": "Bob",
        "time_windows": "06:00-08:00, 20:00-22:30",
    }
    # Exactly what the assignment code reads.
    assert parse_windows(subentry.data["time_windows"]) == [(360, 480), (1200, 1350)]
    await _unload(hass, entry)


async def test_a_window_may_cross_midnight(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF)
    await _setup(hass, entry)
    result = await _start(hass, entry)
    created = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _form_input("Bob", "22:00-02:00")
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    (subentry,) = entry.subentries.values()
    assert subentry.data["time_windows"] == "22:00-02:00"
    assert parse_windows(subentry.data["time_windows"]) == [(1320, 120)]
    await _unload(hass, entry)


# --- the flow: validation and warnings -----------------------------------------------


def _top_level_fields(result: dict) -> set[str]:
    """The fields the frontend can put an error on: a section shows none inside it."""
    return {str(key) for key in result["data_schema"].schema}


async def test_bad_input_is_reported_on_its_field(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF, users=[_subentry("u1", "Bob", sys_min=130)])
    await _setup(hass, entry)

    result = await _start(hass, entry)
    for user_input, errors in (
        (_form_input("bob", ranges={"sys_min": 100}), {"name": "invalid_name"}),
        (_form_input("  "), {"name": "invalid_name"}),
        (
            _form_input("Eve", ranges={"sys_min": 140, "sys_max": 120}),
            {"usual_range": "invalid_range"},
        ),
    ):
        again = await hass.config_entries.subentries.async_configure(
            result["flow_id"], user_input
        )
        assert again["type"] is FlowResultType.FORM, user_input
        assert again["errors"] == errors, user_input
        assert set(again["errors"]) <= _top_level_fields(again), user_input
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("when", "errors"),
    [
        # A start without an end, an end without a start. Errors of fields inside
        # a section are reported on the section: the frontend shows no others.
        ({"window1_start": "06:00:00"}, {"when": "time_window_incomplete"}),
        ({"window1_end": "08:00:00"}, {"when": "time_window_incomplete"}),
        ({"window2_start": "20:00:00"}, {"when": "time_window_incomplete"}),
        ({"window2_end": "22:00:00"}, {"when": "time_window_incomplete"}),
        # Start equal to end (seconds do not count).
        (
            {"window1_start": "06:00:00", "window1_end": "06:00:30"},
            {"when": "time_window_empty"},
        ),
        # Each window is checked on its own.
        (
            {
                "window1_start": "06:00:00",
                "window1_end": "08:00:00",
                "window2_start": "21:00:00",
                "window2_end": "21:00:00",
            },
            {"when": "time_window_empty"},
        ),
    ],
)
async def test_a_half_filled_or_empty_window_is_reported_on_its_section(
    hass: HomeAssistant,
    enable_bluetooth: None,
    when: dict[str, str],
    errors: dict[str, str],
) -> None:
    entry = _entry(hass, CUFF)
    await _setup(hass, entry)
    result = await _start(hass, entry)
    again = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Eve", "when": when, "usual_range": {}}
    )
    assert again["type"] is FlowResultType.FORM
    assert again["step_id"] == "pressure"
    assert again["errors"] == errors
    assert set(again["errors"]) <= _top_level_fields(again)
    assert not entry.subentries
    await _unload(hass, entry)


async def test_a_user_with_no_rules_is_warned_but_can_still_be_saved(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF)
    await _setup(hass, entry)

    result = await _start(hass, entry)
    warned = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _form_input("Bob")
    )
    assert warned["type"] is FlowResultType.FORM
    assert warned["step_id"] == "confirm_no_rules"
    assert warned["description_placeholders"]["name"] == "Bob"
    assert not entry.subentries

    created = await hass.config_entries.subentries.async_configure(
        warned["flow_id"], {}
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert [s.title for s in entry.subentries.values()] == ["Bob"]
    await _unload(hass, entry)


async def test_a_single_user_without_rules_gets_the_plain_warning_when_others_have_rules(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(
        hass, CUFF, users=[_subentry("u1", "Bob", time_windows="06:00-09:00")]
    )
    await _setup(hass, entry)
    result = await _start(hass, entry)
    warned = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _form_input("Eve")
    )
    assert warned["step_id"] == "confirm_no_rules"
    await _unload(hass, entry)


async def test_two_users_without_rules_get_the_stronger_warning(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(
        hass,
        CUFF,
        users=[
            _subentry("u1", "Bob"),
            _subentry("u2", "Cy", sys_min=130),
            _subentry("u3", "Di"),
        ],
    )
    await _setup(hass, entry)

    result = await _start(hass, entry)
    warned = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _form_input("Eve")
    )
    # The shared warning replaces the plain one; it names the others without rules.
    assert warned["type"] is FlowResultType.FORM
    assert warned["step_id"] == "confirm_no_rules_shared"
    assert warned["description_placeholders"] == {"name": "Eve", "others": "Bob, Di"}
    assert "Eve" not in [s.title for s in entry.subentries.values()]

    # It still allows saving.
    created = await hass.config_entries.subentries.async_configure(
        warned["flow_id"], {}
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert "Eve" in [s.title for s in entry.subentries.values()]
    await _unload(hass, entry)


async def test_editing_a_user_does_not_count_them_as_the_other_without_rules(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF, users=[_subentry("u1", "Bob")])
    await _setup(hass, entry)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_USER),
        context={"source": "reconfigure", "subentry_id": "u1"},
    )
    warned = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _form_input("Bob")
    )
    assert warned["step_id"] == "confirm_no_rules"
    await _unload(hass, entry)


async def test_overlapping_time_windows_warn_without_blocking(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(
        hass, CUFF, users=[_subentry("u1", "Bob", time_windows="06:00-09:00")]
    )
    await _setup(hass, entry)

    result = await _start(hass, entry)
    # The overlap is worked out from the picked windows: the second one here.
    warned = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _form_input("Eve", "20:00-21:00", "08:00-10:00")
    )
    assert warned["step_id"] == "confirm_overlap"
    assert warned["description_placeholders"] == {"name": "Eve", "others": "Bob"}

    created = await hass.config_entries.subentries.async_configure(
        warned["flow_id"], {}
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert sorted(s.title for s in entry.subentries.values()) == ["Bob", "Eve"]
    await _unload(hass, entry)


async def test_touching_windows_and_overlapping_values_do_not_warn(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(
        hass,
        CUFF,
        users=[_subentry("u1", "Bob", time_windows="06:00-09:00", sys_min=120)],
    )
    await _setup(hass, entry)

    result = await _start(hass, entry)
    created = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        _form_input("Eve", "09:00-10:00", ranges={"sys_min": 125}),
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await _unload(hass, entry)


async def test_a_weight_user_gets_no_rule_warnings(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, SCALE)
    await _setup(hass, entry)
    result = await _start(hass, entry)
    created = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Alice", "expected_weight": 60}
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await _unload(hass, entry)


async def test_a_user_can_be_edited(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(
        hass,
        SCALE,
        users=[_subentry("u1", "Alice", expected_weight=62.0, height_cm=168)],
    )
    await _setup(hass, entry)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_USER),
        context={"source": "reconfigure", "subentry_id": "u1"},
    )
    assert result["step_id"] == "weight"
    suggested = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description
    }
    assert suggested == {"name": "Alice", "expected_weight": 62.0, "height_cm": 168}

    # The name is her own, so keeping it is fine; clearing the height drops it.
    done = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Alice", "expected_weight": 63.5}
    )
    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    subentry = entry.subentries["u1"]
    assert dict(subentry.data) == {"name": "Alice", "expected_weight": 63.5}
    await _unload(hass, entry)


async def _reconfigure(hass: HomeAssistant, entry: MockConfigEntry, subentry_id: str):
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_USER),
        context={"source": "reconfigure", "subentry_id": subentry_id},
    )


async def test_editing_prefills_the_pickers_and_ranges_through_the_sections(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(
        hass,
        CUFF,
        users=[
            _subentry(
                "u1",
                "Bob",
                time_windows="06:00-08:00, 22:00-02:00",
                sys_min=130,
                dia_max=95,
            )
        ],
    )
    await _setup(hass, entry)

    result = await _reconfigure(hass, entry, "u1")
    assert result["step_id"] == "pressure"
    assert _suggested(result["data_schema"]) == {
        "name": "Bob",
        "when": {
            "window1_start": "06:00:00",
            "window1_end": "08:00:00",
            "window2_start": "22:00:00",
            "window2_end": "02:00:00",
        },
        "usual_range": {"sys_min": 130, "dia_max": 95},
    }
    # Two windows fit the form: nothing to warn about.
    assert result["description_placeholders"] == {"extra_windows": ""}

    # Submitting what the form shows keeps the user as it was.
    done = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Bob",
            "when": {
                "window1_start": "06:00:00",
                "window1_end": "08:00:00",
                "window2_start": "22:00:00",
                "window2_end": "02:00:00",
            },
            "usual_range": {"sys_min": 130, "dia_max": 95},
        },
    )
    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert dict(entry.subentries["u1"].data) == {
        "name": "Bob",
        "time_windows": "06:00-08:00, 22:00-02:00",
        "sys_min": 130,
        "dia_max": 95,
    }
    await _unload(hass, entry)


async def test_editing_a_scale_user_by_rules_prefills_height_and_weight_range(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(
        hass,
        SCALE,
        options={CONF_ASSIGNMENT_STRATEGY: STRATEGY_RULES},
        users=[
            _subentry(
                "u1", "Alice", time_windows="21:00-23:00", height_cm=168, weight_min=55
            )
        ],
    )
    await _setup(hass, entry)
    result = await _reconfigure(hass, entry, "u1")
    assert result["step_id"] == "rules"
    assert _suggested(result["data_schema"]) == {
        "name": "Alice",
        "height_cm": 168,
        "when": {"window1_start": "21:00:00", "window1_end": "23:00:00"},
        "usual_range": {"weight_min": 55},
    }
    # Clearing a picker pair and a range drops them from the stored data.
    done = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Alice",
            "height_cm": 168,
            "when": {"window1_start": "20:00:00", "window1_end": "22:00:00"},
            "usual_range": {},
        },
    )
    assert done["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()
    assert dict(entry.subentries["u1"].data) == {
        "name": "Alice",
        "height_cm": 168,
        "time_windows": "20:00-22:00",
    }
    await _unload(hass, entry)


async def test_a_user_with_more_than_two_stored_windows_is_shown_them_and_a_note(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    # 4.6.0-beta.1 took any number of windows in its text field.
    entry = _entry(
        hass,
        CUFF,
        users=[
            _subentry("u1", "Bob", time_windows="06:00-07:00, 12:00-13:00, 20:00-21:00")
        ],
    )
    await _setup(hass, entry)

    result = await _reconfigure(hass, entry, "u1")
    note = result["description_placeholders"]["extra_windows"]
    # The note lists the window the pickers cannot show, not the ones they do,
    # and says what saving does.
    assert "20:00-21:00" in note
    assert "06:00-07:00" not in note and "12:00-13:00" not in note
    assert "{windows}" not in note
    assert "two" in note
    # The pickers hold the first two.
    assert _suggested(result["data_schema"])["when"] == {
        "window1_start": "06:00:00",
        "window1_end": "07:00:00",
        "window2_start": "12:00:00",
        "window2_end": "13:00:00",
    }

    # The note stays when the form is shown again after an error.
    bad = _form_input("Bob", "06:00-07:00", ranges={"sys_min": 140, "sys_max": 120})
    again = await hass.config_entries.subentries.async_configure(result["flow_id"], bad)
    assert again["errors"] == {"usual_range": "invalid_range"}
    assert again["description_placeholders"]["extra_windows"] == note

    # Saving keeps the two shown windows only.
    done = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _form_input("Bob", "06:00-07:00", "12:00-13:00")
    )
    assert done["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()
    assert entry.subentries["u1"].data["time_windows"] == "06:00-07:00, 12:00-13:00"
    await _unload(hass, entry)


async def test_the_note_about_extra_windows_is_translated(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    hass.config.language = "ko"
    entry = _entry(
        hass,
        CUFF,
        users=[
            _subentry("u1", "Bob", time_windows="06:00-07:00, 12:00-13:00, 20:00-21:00")
        ],
    )
    await _setup(hass, entry)
    result = await _reconfigure(hass, entry, "u1")
    note = result["description_placeholders"]["extra_windows"]
    assert "20:00-21:00" in note and "06:00-07:00" not in note
    assert "시간대" in note
    await _unload(hass, entry)


# --- subentry changes reload the entry ---------------------------------------------


async def test_adding_a_user_reloads_the_entry_and_adds_the_sensors(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, SCALE)
    await _setup(hass, entry)
    before = _unique_ids(hass, entry)
    runtime = entry.runtime_data

    result = await _start(hass, entry)
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Alice", "expected_weight": 62.5}
    )
    await hass.async_block_till_done()

    assert entry.runtime_data is not runtime
    added = _unique_ids(hass, entry) - before
    (subentry_id,) = entry.subentries
    assert added == {
        f"{entry.entry_id}_{subentry_id}_weight",
        f"{entry.entry_id}_{subentry_id}_bmi",
    }
    await _unload(hass, entry)


# --- sub-devices and sensors -------------------------------------------------------------


async def test_a_scale_user_has_a_sub_device_with_weight_and_bmi(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(
        hass,
        SCALE,
        users=[_subentry("u1", "Alice", expected_weight=62.0, height_cm=170)],
    )
    await _setup(hass, entry)

    user_device = _device(hass, entry, identifier=(DOMAIN, f"{entry.entry_id}_u1"))
    assert user_device is not None
    assert user_device.name == "Alice"
    assert user_device.manufacturer == "Omron"
    assert _subentries_of(user_device, entry) == {"u1"}
    main = _device(hass, entry, connection=("bluetooth", ADDRESS))
    assert main is not None and main.id != user_device.id
    assert user_device.via_device_id == main.id

    registry = er.async_get(hass)
    weight = registry.async_get(
        registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_u1_weight")
    )
    assert weight.device_id == user_device.id
    assert weight.config_subentry_id == "u1"
    assert weight.translation_key == "user_weight"
    assert hass.states.get(weight.entity_id).state == "unknown"

    # The scale's own entities are still there, on the scale's own device.
    own = er.async_entries_for_device(registry, main.id)
    assert any(e.translation_key == "bmi" for e in own)
    assert not any(e.config_subentry_id for e in own)
    await _unload(hass, entry)


async def test_a_cuff_user_has_the_reading_metric_and_flag_entities(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF, users=[_subentry("u1", "Bob", sys_min=130)])
    await _setup(hass, entry)

    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_u1_"
    own = {
        e.unique_id.removeprefix(prefix): e
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.unique_id.startswith(prefix)
    }
    assert set(own) == {
        # The reading.
        "systolic",
        "diastolic",
        "pulse",
        "measured_at",
        # Worked out from it, as the cuff's own sensors are.
        "pulse_pressure",
        "mean_arterial_pressure_estimated",
        "blood_pressure_category",
        "shock_index",
        "rate_pressure_product",
        # The status flags the HEM-7600T's records carry (binary sensors).
        "irregular_pulse",
        "body_movement",
        "cuff_fit",
        "improper_position",
    }
    assert {e.domain for e in own.values()} == {"sensor", "binary_sensor"}
    assert own["irregular_pulse"].domain == "binary_sensor"
    assert own["systolic"].original_device_class is None
    assert own["measured_at"].original_device_class == "timestamp"
    await _unload(hass, entry)


async def test_each_user_gets_a_device_of_their_own(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(
        hass,
        CUFF,
        users=[
            _subentry("u1", "Bob", time_windows="06:00-09:00"),
            _subentry("u2", "Eve", time_windows="18:00-21:00"),
        ],
    )
    await _setup(hass, entry)

    ids = {
        _device(hass, entry, identifier=(DOMAIN, f"{entry.entry_id}_{sub}")).id
        for sub in ("u1", "u2")
    }
    assert len(ids) == 2
    await _unload(hass, entry)


async def test_without_users_nothing_changes(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict[str, Any]
) -> None:
    entry = _entry(hass, SCALE)
    await _setup(hass, entry)

    assert entry.runtime_data.users is None
    assert not any(
        e.config_subentry_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    )
    main = _device(hass, entry, connection=("bluetooth", ADDRESS))
    assert main.identifiers == set()
    assert not [key for key in hass_storage if key.startswith("omron.users_")]
    await _unload(hass, entry)


async def test_a_multi_slot_cuff_ignores_stray_user_subentries(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, MULTI_SLOT_CUFF, users=[_subentry("u1", "Bob")])
    await _setup(hass, entry)
    assert entry.runtime_data.users is None
    assert not any(
        e.config_subentry_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    )
    await _unload(hass, entry)


# --- the options ----------------------------------------------------------------------------


async def test_a_scale_offers_the_strategy_and_the_tolerance(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, SCALE)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = {str(key): key for key in result["data_schema"].schema}
    assert CONF_ASSIGNMENT_STRATEGY in schema
    assert CONF_WEIGHT_TOLERANCE_PCT in schema
    assert schema[CONF_ASSIGNMENT_STRATEGY].default() == STRATEGY_WEIGHT

    done = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: 300,
            CONF_ASSIGNMENT_STRATEGY: STRATEGY_RULES,
            CONF_WEIGHT_TOLERANCE_PCT: 4,
        },
    )
    assert done["type"] is FlowResultType.CREATE_ENTRY
    # Both survive the options flow's rebuilt dict.
    assert done["data"][CONF_ASSIGNMENT_STRATEGY] == STRATEGY_RULES
    assert done["data"][CONF_WEIGHT_TOLERANCE_PCT] == 4.0
    assert entry.options[CONF_ASSIGNMENT_STRATEGY] == STRATEGY_RULES


async def test_the_options_are_kept_when_saved_again(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(
        hass,
        SCALE,
        options={
            CONF_SCAN_INTERVAL: 300,
            CONF_ASSIGNMENT_STRATEGY: STRATEGY_RULES,
            CONF_WEIGHT_TOLERANCE_PCT: 4.5,
        },
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = {str(key): key for key in result["data_schema"].schema}
    # The form shows what is saved, so submitting it as is changes nothing.
    assert schema[CONF_ASSIGNMENT_STRATEGY].default() == STRATEGY_RULES
    assert schema[CONF_WEIGHT_TOLERANCE_PCT].default() == 4.5
    done = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 300}
    )
    assert done["data"][CONF_ASSIGNMENT_STRATEGY] == STRATEGY_RULES
    assert done["data"][CONF_WEIGHT_TOLERANCE_PCT] == 4.5


async def test_the_tolerance_is_limited_to_one_to_five_percent(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, SCALE)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    for pct in (0.5, 6):
        with pytest.raises(vol.Invalid):
            await hass.config_entries.options.async_configure(
                result["flow_id"],
                {CONF_SCAN_INTERVAL: 300, CONF_WEIGHT_TOLERANCE_PCT: pct},
            )


@pytest.mark.parametrize("model", [CUFF, MULTI_SLOT_CUFF])
async def test_a_cuff_has_no_strategy_to_choose(
    hass: HomeAssistant, enable_bluetooth: None, model: str
) -> None:
    entry = _entry(hass, model)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    names = {str(key) for key in result["data_schema"].schema}
    assert CONF_ASSIGNMENT_STRATEGY not in names
    assert CONF_WEIGHT_TOLERANCE_PCT not in names
    done = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 300}
    )
    assert CONF_ASSIGNMENT_STRATEGY not in done["data"]
