"""A loaded config entry exposes runtime data and the diagnostic entities."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.omron.const import CONF_DEVICE_MODEL, DOMAIN
from custom_components.omron.data import OmronRuntimeData

ADDRESS = "AA:BB:CC:DD:EE:FF"


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
