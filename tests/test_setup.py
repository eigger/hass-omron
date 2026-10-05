"""A loaded config entry exposes runtime data and the diagnostic entities."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from sensor_state_data import SensorUpdate
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.omron import process_service_info
from custom_components.omron.const import CONF_DEVICE_MODEL, DOMAIN
from custom_components.omron.data import OmronRuntimeData
from custom_components.omron.omron_ble.const import OMRON_MANUFACTURER_ID

from bt import service_info

ADDRESS = "AA:BB:CC:DD:EE:FF"
# Format 0x01, forced-transfer bit (0x40), no registered users.
_DATA_PENDING = bytes([0x01, 0x40, 0x00, 0x00])


async def test_setup_binds_runtime_and_diagnostic_entities(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """No cuff in range: setup still loads, and diagnostics are real entities."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="HEM-7155T EEFF",
        data={CONF_DEVICE_MODEL: "HEM-7155T"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = entry.runtime_data
    assert isinstance(runtime, OmronRuntimeData)
    assert runtime.address == ADDRESS
    assert runtime.model == "HEM-7155T"
    assert runtime.session_lock.locked() is False
    assert runtime.pending_forced_transfer is False
    assert DOMAIN not in hass.data or entry.entry_id not in hass.data[DOMAIN]

    unique_ids = {
        registry_entry.unique_id
        for registry_entry in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }
    assert "hem_7155t_eeff_connection" in unique_ids
    assert "hem_7155t_eeff_duration" in unique_ids
    assert "hem_7155t_eeff_last_failure" in unique_ids
    assert "hem_7155t_eeff_failure_count" in unique_ids
    assert "hem_7155t_eeff_last_readout" in unique_ids
    assert "hem_7155t_eeff_device_alias" in unique_ids
    assert "hem_7155t_eeff_refresh_data" in unique_ids
    assert "hem_7155t_eeff_retry_pairing" in unique_ids

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_unload_cancels_a_drain_waiting_on_the_session_lock(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """The drain blocks on the session lock. Unload has to cancel it, or it
    wakes up later and drives a coordinator that is already gone."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="HEM-7155T EEFF",
        data={CONF_DEVICE_MODEL: "HEM-7155T"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = entry.runtime_data
    await runtime.session_lock.acquire()
    process_service_info(
        entry,
        service_info(
            ADDRESS,
            name="HEM-7155T",
            manufacturer_data={OMRON_MANUFACTURER_ID: _DATA_PENDING},
        ),
    )
    task = runtime.pending_forced_transfer_task
    assert task is not None
    assert task.done() is False

    assert await hass.config_entries.async_unload(entry.entry_id)
    runtime.session_lock.release()
    await hass.async_block_till_done()
    assert task.cancelled()


async def test_zero_interval_stops_scheduled_polls_but_not_manual_ones(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """Options scan_interval 0 beats data's 300: no timer, Refresh Data still polls."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="HEM-7155T EEFF",
        data={CONF_DEVICE_MODEL: "HEM-7155T", CONF_SCAN_INTERVAL: 300},
        options={CONF_SCAN_INTERVAL: 0},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.omron.async_poll_data", new_callable=AsyncMock
    ) as poll:
        poll.return_value = SensorUpdate(
            title=None, devices={}, entity_descriptions={}, entity_values={}, binary_entity_descriptions={}, binary_entity_values={}
        )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.poll_coordinator
        assert coordinator.update_interval is None

        # The one poll made at setup waits on a short sleep; firing time
        # changed also fires the loop timers that end it.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
        await hass.async_block_till_done()
        poll.assert_called_once()

        poll.reset_mock()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(days=2))
        await hass.async_block_till_done()
        poll.assert_not_called()

        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.hem_7155t_eeff_refresh_data"},
            blocking=True,
        )
        await hass.async_block_till_done()
        poll.assert_called_once()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_failure_sensors_follow_the_session_reports(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """Failure Count / Last Failure come from SessionReports via the listener
    async_setup_entry binds; unload unbinds it and a reload starts fresh."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="HEM-7155T EEFF",
        data={CONF_DEVICE_MODEL: "HEM-7155T"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    count_id = registry.async_get_entity_id(
        "sensor", DOMAIN, "hem_7155t_eeff_failure_count"
    )
    last_id = registry.async_get_entity_id(
        "sensor", DOMAIN, "hem_7155t_eeff_last_failure"
    )
    assert count_id is not None and last_id is not None
    assert hass.states.get(count_id).state == "0"

    reports = entry.runtime_data.session_reports
    reports.record({"operation": "poll", "success": False, "error": "x"})
    await hass.async_block_till_done()

    assert hass.states.get(count_id).state == "1"
    last_state = hass.states.get(last_id).state
    assert last_state not in ("unknown", "unavailable")
    # A timestamp sensor's state drops the microseconds.
    assert dt_util.parse_datetime(last_state) == reports.last_failure_at.replace(
        microsecond=0
    )

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert reports._listeners == []

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.session_reports is not reports
    assert hass.states.get(count_id).state == "0"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
