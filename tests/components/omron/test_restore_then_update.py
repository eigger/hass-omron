"""After a restart the restored value shows until a real reading replaces it."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, State
from pytest_homeassistant_custom_component.common import mock_restore_cache
from sensor_state_data import SensorUpdate

from custom_components.omron import _merge_poll_sensor_update

from tests.components.omron.test_sensor import (
    _RECORD,
    _entry,
    _preregister,
    _publish_record,
    _setup,
    _unload,
)

SINGLE_USER_MODEL = "HEM-7600T"
TWO_USER_MODEL = "HEM-7155T"


async def _publish(hass: HomeAssistant, entry, record: dict, *, user, multi_user):
    runtime = entry.runtime_data
    runtime.device_data._update_measurement_sensors(
        record, user=user, multi_user=multi_user
    )
    runtime.poll_coordinator.async_set_updated_data(
        runtime.device_data._finish_update()
    )
    await hass.async_block_till_done()


async def test_a_reading_replaces_the_restored_value_and_stays(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(SINGLE_USER_MODEL)
    systolic = _preregister(
        hass, entry, "sensor", "hem_7600t_eeff_none_blood_pressure_systolic"
    )
    flag = _preregister(
        hass, entry, "binary_sensor", "hem_7600t_eeff_none_body_movement"
    )
    mock_restore_cache(hass, [State(systolic, "118"), State(flag, "off")])

    await _setup(hass, entry)
    assert hass.states.get(systolic).state == "118"
    assert hass.states.get(flag).state == "off"

    await _publish_record(hass, entry)
    assert hass.states.get(systolic).state == "120"
    assert hass.states.get(flag).state == "on"

    # A later reading replaces it again.
    await _publish(
        hass, entry, {**_RECORD, "sys": 131, "mov": False}, user=None, multi_user=False
    )
    assert hass.states.get(systolic).state == "131"
    assert hass.states.get(flag).state == "off"
    await _unload(hass, entry)


async def test_a_poll_without_a_reading_keeps_the_live_value(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """The restored value does not come back when a later poll has nothing new.

    An entity falls back to its restored value when the data has no value for
    it, so what keeps the live value is the poll merging its result into the
    previous data. This goes through that merge.
    """
    entry = _entry(SINGLE_USER_MODEL)
    systolic = _preregister(
        hass, entry, "sensor", "hem_7600t_eeff_none_blood_pressure_systolic"
    )
    flag = _preregister(
        hass, entry, "binary_sensor", "hem_7600t_eeff_none_body_movement"
    )
    mock_restore_cache(hass, [State(systolic, "118"), State(flag, "off")])
    await _setup(hass, entry)
    await _publish_record(hass, entry)
    assert hass.states.get(systolic).state == "120"

    runtime = entry.runtime_data
    nothing_new = SensorUpdate(
        title=None,
        devices={},
        entity_descriptions={},
        entity_values={},
        binary_entity_descriptions={},
        binary_entity_values={},
        events={},
    )
    runtime.poll_coordinator.async_set_updated_data(
        _merge_poll_sensor_update(runtime.poll_coordinator.data, nothing_new)
    )
    await hass.async_block_till_done()

    assert hass.states.get(systolic).state == "120"
    assert hass.states.get(flag).state == "on"
    await _unload(hass, entry)


async def test_only_the_user_who_measured_gets_a_new_value(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """The other user keeps the restored reading and flag."""
    entry = _entry(TWO_USER_MODEL)
    uid = "hem_7155t_eeff_none_"
    user1 = _preregister(hass, entry, "sensor", f"{uid}blood_pressure_systolic_user1")
    user2 = _preregister(hass, entry, "sensor", f"{uid}blood_pressure_systolic_user2")
    flag1 = _preregister(hass, entry, "binary_sensor", f"{uid}body_movement_user1")
    flag2 = _preregister(hass, entry, "binary_sensor", f"{uid}body_movement_user2")
    mock_restore_cache(
        hass,
        [
            State(user1, "121"),
            State(user2, "132"),
            State(flag1, "off"),
            State(flag2, "off"),
        ],
    )

    await _setup(hass, entry)
    assert hass.states.get(user1).state == "121"
    assert hass.states.get(user2).state == "132"

    await _publish(hass, entry, _RECORD, user=1, multi_user=True)

    assert hass.states.get(user1).state == "120"
    assert hass.states.get(flag1).state == "on"
    assert hass.states.get(user2).state == "132"
    assert hass.states.get(flag2).state == "off"
    await _unload(hass, entry)
