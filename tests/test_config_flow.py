"""The model step is driven through Home Assistant's flow manager.

An unidentified cuff must not arrive with a model already filled in. The
fallback profile is a real EEPROM map, so confirming it reads the cuff
through the wrong layout and reports no error (#45).
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import SOURCE_BLUETOOTH
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
import voluptuous as vol

from custom_components.omron.const import CONF_DEVICE_MODEL, DOMAIN
from custom_components.omron.omron_ble.const import OMRON_MANUFACTURER_ID

from bt import service_info

ADDRESS = "AA:BB:CC:DD:EE:11"
# Format 0x01, no status flags, one user slot of sequence bytes. Enough for
# ``supported()``; the name is what the model step reads.
_QUIET_MSD = bytes([0x01, 0x00, 0x00, 0x00])


async def _open_model_step(hass: HomeAssistant, name: str):
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=service_info(
            ADDRESS,
            name=name,
            manufacturer_data={OMRON_MANUFACTURER_ID: _QUIET_MSD},
        ),
    )
    if result["step_id"] == "bluetooth_confirm":
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "select_model"
    return result


def _field(schema: vol.Schema, key: str) -> vol.Marker:
    return next(marker for marker in schema.schema if marker == key)


def _default(marker: vol.Marker):
    """Home Assistant wraps a provided default in a zero-arg factory."""
    value = marker.default
    return value() if callable(value) else value


async def test_an_unidentified_cuff_leaves_the_model_empty(
    hass: HomeAssistant, enable_bluetooth: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing in the name is a catalog id, and no radio is present to probe."""
    with caplog.at_level(logging.WARNING):
        result = await _open_model_step(hass, "BLESmart_0000123")

    model = _field(result["data_schema"], CONF_DEVICE_MODEL)
    assert model.default is vol.UNDEFINED
    assert "has to be picked by hand" in caplog.text

    interval = _field(result["data_schema"], CONF_SCAN_INTERVAL)
    assert _default(interval) == 300


async def test_a_named_cuff_preselects_that_model(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    result = await _open_model_step(hass, "HEM-7155T")
    model = _field(result["data_schema"], CONF_DEVICE_MODEL)
    assert _default(model) == "HEM-7155T"
