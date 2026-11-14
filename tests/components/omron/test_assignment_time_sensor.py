"""Current local time sensors for registered users."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from homeassistant.util import dt as dt_util

from .test_user_derived_entities import _state
from .test_user_measurements import CUFF, SCALE, _entry, _setup, _user


@pytest.fixture
def utc_timezone():
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(timezone.utc)
    yield
    dt_util.set_default_time_zone(previous)


@pytest.mark.parametrize("model", [CUFF, SCALE])
async def test_initial_state_and_windows(
    hass, enable_bluetooth, freezer, utc_timezone, model
):
    freezer.move_to("2026-01-01T23:30:00+00:00")
    entry = _entry(
        hass,
        model,
        [
            _user("bob", "Bob", time_windows="22:00-02:00, 09:00-12:00"),
            _user("eve", "Eve"),
            _user("alice", "Alice", time_windows="00:00-24:00"),
        ],
    )
    await _setup(hass, entry)
    state = _state(hass, entry, "bob", "binary_sensor", "assignment_time")
    assert state.state == "on"
    assert state.attributes["time_windows"] == [
        {"start": "22:00", "end": "02:00"},
        {"start": "09:00", "end": "12:00"},
    ]
    unrestricted = _state(hass, entry, "eve", "binary_sensor", "assignment_time")
    assert unrestricted.state == "on"
    assert unrestricted.attributes["time_windows"] == []
    all_day = _state(hass, entry, "alice", "binary_sensor", "assignment_time")
    assert all_day.state == "on"
    assert all_day.attributes["time_windows"] == [{"start": "00:00", "end": "24:00"}]


async def test_boundary_updates_and_reload(
    hass, enable_bluetooth, freezer, utc_timezone
):
    freezer.move_to("2026-01-01T21:59:00+00:00")
    entry = _entry(
        hass, CUFF, [_user("bob", "Bob", time_windows="22:00-02:00, 09:00-12:00")]
    )
    await _setup(hass, entry)
    assert _state(hass, entry, "bob", "binary_sensor", "assignment_time").state == "off"
    for day, hour, expected in [
        (1, 22, "on"),
        (2, 2, "off"),
        (2, 9, "on"),
        (2, 12, "off"),
    ]:
        moment = datetime(2026, 1, day, hour, tzinfo=timezone.utc)
        freezer.move_to(moment)
        async_fire_time_changed(hass, moment)
        await hass.async_block_till_done()
        assert (
            _state(hass, entry, "bob", "binary_sensor", "assignment_time").state
            == expected
        )
    # Reload while inside the overnight window; the value is recomputed immediately.
    freezer.move_to("2026-01-02T23:30:00+00:00")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _state(hass, entry, "bob", "binary_sensor", "assignment_time").state == "on"
    subentry = entry.subentries["bob"]
    hass.config_entries.async_update_subentry(
        entry, subentry, data={**subentry.data, "time_windows": "08:00-10:00"}
    )
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    state = _state(hass, entry, "bob", "binary_sensor", "assignment_time")
    assert state.state == "off"
    assert state.attributes["time_windows"] == [{"start": "08:00", "end": "10:00"}]
    assert await hass.config_entries.async_unload(entry.entry_id)
    moment = datetime(2026, 1, 3, 2, tzinfo=timezone.utc)
    freezer.move_to(moment)
    async_fire_time_changed(hass, moment)
    await hass.async_block_till_done()
    assert (
        _state(hass, entry, "bob", "binary_sensor", "assignment_time").state
        == "unavailable"
    )


async def test_uses_home_assistant_local_time(
    hass, enable_bluetooth, freezer, utc_timezone
):
    dt_util.set_default_time_zone(ZoneInfo("Asia/Seoul"))
    freezer.move_to("2026-01-01T00:30:00+00:00")  # 09:30 in Seoul
    entry = _entry(hass, CUFF, [_user("bob", "Bob", time_windows="09:00-10:00")])
    await _setup(hass, entry)
    assert _state(hass, entry, "bob", "binary_sensor", "assignment_time").state == "on"


@pytest.mark.parametrize(
    ("language", "on_label", "off_label"),
    [("en", "Within", "Outside"), ("ko", "해당", "미해당")],
)
async def test_state_translations(
    hass, enable_bluetooth, language, on_label, off_label
):
    """Home Assistant loads entity-specific labels for the binary states."""
    from homeassistant.helpers.translation import async_get_translations

    translations = await async_get_translations(hass, language, "entity", {"omron"})
    prefix = "component.omron.entity.binary_sensor.user_assignment_time.state"
    assert translations[f"{prefix}.on"] == on_label
    assert translations[f"{prefix}.off"] == off_label
