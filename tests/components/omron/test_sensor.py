"""Real config-entry setup, platform loading, unload and reload.

Only the Bluetooth radio is replaced (``enable_bluetooth`` and the poll
coordinator's pushed data); Home Assistant itself, the config-entry state
machine, the entity and device registries and all five platforms are real.
"""

from __future__ import annotations

import datetime

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.core import State
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.omron import PLATFORMS
from custom_components.omron.const import CONF_DEVICE_MODEL, DOMAIN

ADDRESS = "AA:BB:CC:DD:EE:FF"
BP_MODEL = "HEM-7155T"
SCALE_MODEL = "HN-300T2"

_RECORD = {
    "datetime": datetime.datetime(2026, 1, 1, 8, 0),
    "user": 1,
    "sys": 120,
    "dia": 80,
    "bpm": 60,
    "weight": 70.5,
    "mov": True,
    "ihb": True,
}


def _entry(model: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title=f"{model} EEFF",
        data={CONF_DEVICE_MODEL: model},
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


def _device(hass: HomeAssistant, entry: MockConfigEntry) -> dr.DeviceEntry | None:
    """The entry's Bluetooth device, looked up the same way on every HA version."""
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if (CONNECTION_BLUETOOTH, ADDRESS) in device.connections:
            return device
    return None


def _by_domain(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, set[str]]:
    """Registered unique ids of the entry, grouped by entity platform domain."""
    grouped: dict[str, set[str]] = {}
    for reg_entry in er.async_entries_for_config_entry(
        er.async_get(hass), entry.entry_id
    ):
        grouped.setdefault(reg_entry.domain, set()).add(reg_entry.unique_id)
    return grouped


async def _publish_record(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Hand the poll coordinator what a successful readout would produce."""
    runtime = entry.runtime_data
    runtime.device_data._update_measurement_sensors(
        _RECORD, user=None, multi_user=False
    )
    runtime.poll_coordinator.async_set_updated_data(
        runtime.device_data._finish_update()
    )
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, domain: str, unique_id: str):
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None, f"{domain} {unique_id} is not registered"
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} has no state"
    return state


async def test_blood_pressure_entry_loads_every_platform(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """A cuff gets sensor, binary_sensor, button and text entities, and no number."""
    entry = _entry(BP_MODEL)
    await _setup(hass, entry)

    grouped = _by_domain(hass, entry)
    assert grouped["binary_sensor"] == {"hem_7155t_eeff_connection"}
    assert grouped["button"] == {
        "hem_7155t_eeff_refresh_data",
        "hem_7155t_eeff_retry_pairing",
    }
    assert grouped["text"] == {"hem_7155t_eeff_device_alias"}
    # Diagnostics and the seeded measurement sensors exist at setup, so the
    # measurements can restore their last value while the cuff is asleep.
    assert {
        "hem_7155t_eeff_duration",
        "hem_7155t_eeff_last_readout",
        "hem_7155t_eeff_last_failure",
        "hem_7155t_eeff_failure_count",
        "hem_7155t_eeff_none_blood_pressure_systolic_user1",
        "hem_7155t_eeff_none_blood_pressure_systolic_user2",
    } <= grouped["sensor"]
    assert not any("signal_strength" in uid for uid in grouped["sensor"])
    assert "number" not in grouped
    assert set(grouped) <= {platform.value for platform in PLATFORMS}

    assert _state(hass, "sensor", "hem_7155t_eeff_failure_count").state == "0"
    assert _state(hass, "text", "hem_7155t_eeff_device_alias").state == BP_MODEL
    assert _state(hass, "binary_sensor", "hem_7155t_eeff_connection").state == "off"

    device = _device(hass, entry)
    assert device is not None
    assert device.manufacturer == "Omron"
    assert device.model == BP_MODEL
    assert device.name == f"{BP_MODEL} EEFF"
    assert (CONNECTION_BLUETOOTH, ADDRESS) in device.connections

    await _unload(hass, entry)


async def test_blood_pressure_readout_reaches_sensor_and_binary_states(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(BP_MODEL)
    await _setup(hass, entry)
    await _publish_record(hass, entry)

    assert (
        _state(hass, "sensor", "hem_7155t_eeff_none_blood_pressure_systolic").state
        == "120"
    )
    assert (
        _state(hass, "sensor", "hem_7155t_eeff_none_blood_pressure_diastolic").state
        == "80"
    )
    assert _state(hass, "sensor", "hem_7155t_eeff_none_heart_rate").state == "60"
    assert _state(hass, "sensor", "hem_7155t_eeff_none_pulse_pressure").state == "40.0"
    assert (
        _state(hass, "sensor", "hem_7155t_eeff_none_blood_pressure_category").state
        == "hypertension_stage_1"
    )
    assert (
        _state(hass, "binary_sensor", "hem_7155t_eeff_none_body_movement").state == "on"
    )
    assert (
        _state(hass, "binary_sensor", "hem_7155t_eeff_none_irregular_pulse").state
        == "on"
    )
    # The multi-user slots stay empty: the record carried no user number.
    assert (
        _state(
            hass, "sensor", "hem_7155t_eeff_none_blood_pressure_systolic_user2"
        ).state
        == "unknown"
    )

    await _unload(hass, entry)


async def test_scale_entry_loads_height_bmi_and_weight(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """A weight-only scale gets the Height number and BMI sensors, not BP sensors."""
    entry = _entry(SCALE_MODEL)
    await _setup(hass, entry)

    grouped = _by_domain(hass, entry)
    assert grouped["number"] == {"hn_300t2_eeff_height"}
    assert {"hn_300t2_eeff_bmi", "hn_300t2_eeff_bmi_category"} <= grouped["sensor"]
    assert grouped["button"] == {
        "hn_300t2_eeff_refresh_data",
        "hn_300t2_eeff_retry_pairing",
    }
    assert grouped["text"] == {"hn_300t2_eeff_device_alias"}
    assert not any("blood_pressure" in uid for uid in grouped["sensor"])

    await _publish_record(hass, entry)
    weight = _state(hass, "sensor", "hn_300t2_eeff_none_weight")
    assert weight.state == "70.5"
    assert weight.attributes["unit_of_measurement"] == "kg"
    # No height yet, so no BMI.
    assert _state(hass, "sensor", "hn_300t2_eeff_bmi").state == "unknown"

    height_id = er.async_get(hass).async_get_entity_id(
        "number", DOMAIN, "hn_300t2_eeff_height"
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": height_id, "value": 175},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert float(hass.states.get(height_id).state) == 175.0
    assert float(_state(hass, "sensor", "hn_300t2_eeff_bmi").state) == pytest.approx(
        70.5 / 1.75**2, abs=0.1
    )

    await _unload(hass, entry)


async def test_unload_releases_runtime_and_makes_entities_unavailable(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(BP_MODEL)
    await _setup(hass, entry)
    runtime = entry.runtime_data
    entity_ids = [
        reg_entry.entity_id
        for reg_entry in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    ]
    assert entity_ids
    assert runtime.unloading is False

    await _unload(hass, entry)

    assert runtime.unloading is True
    assert runtime.session_lock.locked() is False
    assert not runtime.background_tasks
    assert runtime.pending_forced_transfer_task is None
    assert runtime.session_reports._listeners == []
    # The entities stay registered; their states say the integration is gone.
    for entity_id in entity_ids:
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE, entity_id


async def test_reload_builds_a_fresh_runtime_and_keeps_the_device_lock(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(BP_MODEL)
    await _setup(hass, entry)
    first = entry.runtime_data
    before = _by_domain(hass, entry)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    second = entry.runtime_data
    assert second is not first
    assert first.unloading is True and second.unloading is False
    # One lock per device survives the reload, so a poll still running for the
    # old runtime cannot overlap a new session.
    assert second.session_lock is first.session_lock
    assert _by_domain(hass, entry) == before
    for unique_id in before["sensor"]:
        assert _state(hass, "sensor", unique_id).state != STATE_UNAVAILABLE

    await _unload(hass, entry)


async def test_removing_the_entry_removes_its_entities_and_device(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(BP_MODEL)
    await _setup(hass, entry)
    assert _by_domain(hass, entry)

    assert await hass.config_entries.async_remove(entry.entry_id) == {
        "require_restart": False
    }
    await hass.async_block_till_done()

    assert not er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert _device(hass, entry) is None


def _preregister(
    hass: HomeAssistant, entry: MockConfigEntry, domain: str, unique_id: str
) -> str:
    """Put an entity in the registry as an earlier run would have left it."""
    entry.add_to_hass(hass)
    return (
        er.async_get(hass)
        .async_get_or_create(
            domain, DOMAIN, unique_id, config_entry=entry, suggested_object_id=unique_id
        )
        .entity_id
    )


async def test_measurement_sensors_restore_last_value_before_first_poll(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """Both users of a two-user cuff get their own last value back (#284)."""
    entry = _entry(BP_MODEL)
    user1 = _preregister(
        hass, entry, "sensor", "hem_7155t_eeff_none_blood_pressure_systolic_user1"
    )
    user2 = _preregister(
        hass, entry, "sensor", "hem_7155t_eeff_none_blood_pressure_systolic_user2"
    )
    mock_restore_cache(hass, [State(user1, "121"), State(user2, "132")])

    await _setup(hass, entry)

    assert hass.states.get(user1).state == "121"
    assert hass.states.get(user2).state == "132"
    await _unload(hass, entry)


async def test_status_flags_restore_only_for_current_users(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """A flag of a current user returns; one left by an old alias does not."""
    entry = _entry(BP_MODEL)
    current = _preregister(
        hass, entry, "binary_sensor", "hem_7155t_eeff_none_body_movement_user2"
    )
    stale = _preregister(
        hass, entry, "binary_sensor", "hem_7155t_eeff_none_body_movement_dad"
    )
    mock_restore_cache(hass, [State(current, "on"), State(stale, "on")])

    await _setup(hass, entry)

    assert hass.states.get(current).state == "on"
    # Not re-added, so it never restored (core only writes unavailable on start).
    stale_state = hass.states.get(stale)
    assert stale_state is None or stale_state.state != "on"
    await _unload(hass, entry)


async def test_restored_entities_are_not_duplicated_by_the_first_poll(
    hass: HomeAssistant,
    enable_bluetooth: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = _entry(BP_MODEL)
    await _setup(hass, entry)
    before = _by_domain(hass, entry)

    runtime = entry.runtime_data
    runtime.device_data._update_measurement_sensors(_RECORD, user=1, multi_user=True)
    runtime.poll_coordinator.async_set_updated_data(
        runtime.device_data._finish_update()
    )
    await hass.async_block_till_done()

    assert _by_domain(hass, entry)["sensor"] == before["sensor"]
    # Home Assistant drops a second entity with a known unique id and logs it,
    # which the registry alone would not show.
    assert "does not generate unique IDs" not in caplog.text
    assert (
        _state(
            hass, "sensor", "hem_7155t_eeff_none_blood_pressure_systolic_user1"
        ).state
        == "120"
    )
    await _unload(hass, entry)


async def test_restored_status_flag_is_not_duplicated_by_the_first_poll(
    hass: HomeAssistant,
    enable_bluetooth: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A flag restored at startup is the one the first readout updates."""
    entry = _entry(BP_MODEL)
    flag = _preregister(
        hass, entry, "binary_sensor", "hem_7155t_eeff_none_body_movement_user1"
    )
    mock_restore_cache(hass, [State(flag, "off")])
    await _setup(hass, entry)
    assert hass.states.get(flag).state == "off"

    runtime = entry.runtime_data
    runtime.device_data._update_measurement_sensors(_RECORD, user=1, multi_user=True)
    runtime.poll_coordinator.async_set_updated_data(
        runtime.device_data._finish_update()
    )
    await hass.async_block_till_done()

    assert "does not generate unique IDs" not in caplog.text
    assert hass.states.get(flag).state == "on"
    await _unload(hass, entry)


async def test_status_flags_follow_the_configured_user_aliases(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """With an alias set, the alias key returns and the default key does not."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title=f"{BP_MODEL} EEFF",
        data={CONF_DEVICE_MODEL: BP_MODEL, "user_aliases": {"1": "Dad", "2": "Mom"}},
    )
    aliased = _preregister(
        hass, entry, "binary_sensor", "hem_7155t_eeff_none_body_movement_dad"
    )
    default = _preregister(
        hass, entry, "binary_sensor", "hem_7155t_eeff_none_body_movement_user1"
    )
    mock_restore_cache(hass, [State(aliased, "on"), State(default, "on")])

    await _setup(hass, entry)

    assert hass.states.get(aliased).state == "on"
    default_state = hass.states.get(default)
    assert default_state is None or default_state.state != "on"
    await _unload(hass, entry)


async def test_status_flag_without_user_suffix_is_not_restored_on_a_two_user_cuff(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """A flag left from when the model had one user is not brought back."""
    entry = _entry(BP_MODEL)
    single = _preregister(
        hass, entry, "binary_sensor", "hem_7155t_eeff_none_body_movement"
    )
    mock_restore_cache(hass, [State(single, "on")])

    await _setup(hass, entry)

    single_state = hass.states.get(single)
    assert single_state is None or single_state.state != "on"
    await _unload(hass, entry)


async def test_single_user_cuff_restores_its_status_flag(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """A one-user cuff keeps the plain flag key, with no user suffix."""
    entry = _entry("HEM-7600T")
    flag = _preregister(
        hass, entry, "binary_sensor", "hem_7600t_eeff_none_body_movement"
    )
    systolic = _preregister(
        hass, entry, "sensor", "hem_7600t_eeff_none_blood_pressure_systolic"
    )
    mock_restore_cache(hass, [State(flag, "on"), State(systolic, "118")])

    await _setup(hass, entry)

    assert hass.states.get(flag).state == "on"
    assert hass.states.get(systolic).state == "118"
    await _unload(hass, entry)


async def test_advertisement_flag_in_the_registry_does_not_break_setup(
    hass: HomeAssistant,
    enable_bluetooth: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Advertisement flags share the id shape; they are skipped, not parsed."""
    entry = _entry(BP_MODEL)
    _preregister(hass, entry, "binary_sensor", "hem_7155t_eeff_none_forced_transfer")

    await _setup(hass, entry)

    assert "Error while setting up" not in caplog.text
    assert "hem_7155t_eeff_connection" in _by_domain(hass, entry)["binary_sensor"]
    await _unload(hass, entry)


@pytest.mark.parametrize(
    "flag", ["body_movement", "cuff_fit", "irregular_pulse", "improper_position"]
)
async def test_every_status_flag_is_restored(
    hass: HomeAssistant, enable_bluetooth: None, flag: str
) -> None:
    entry = _entry(BP_MODEL)
    entity_id = _preregister(
        hass, entry, "binary_sensor", f"hem_7155t_eeff_none_{flag}_user1"
    )
    mock_restore_cache(hass, [State(entity_id, "on")])

    await _setup(hass, entry)

    assert hass.states.get(entity_id).state == "on"
    await _unload(hass, entry)


async def test_registry_entries_of_other_domains_platforms_or_devices_are_ignored(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """Only this device's own binary_sensor entries are re-created."""
    entry = _entry(BP_MODEL)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    uid = "hem_7155t_eeff_none_cuff_fit_user1"
    registry.async_get_or_create(
        "sensor", DOMAIN, uid, config_entry=entry, suggested_object_id="as_sensor"
    )
    registry.async_get_or_create(
        "binary_sensor", "other", uid, config_entry=entry, suggested_object_id="other"
    )
    registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        "hem_7155t_aaaa_none_cuff_fit_user1",
        config_entry=entry,
        suggested_object_id="other_device",
    )

    await _setup(hass, entry)

    # Re-creating any of them would register this device's own cuff-fit flag.
    assert registry.async_get_entity_id("binary_sensor", DOMAIN, uid) is None
    await _unload(hass, entry)
