"""Config flow probe links must have a single, explicit owner.

Driven through Home Assistant's flow manager; only the GATT model-number read
(the device boundary) is replaced.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

import custom_components.omron.config_flow as config_flow
from custom_components.omron.const import DOMAIN
from custom_components.omron.omron_ble.const import OMRON_MANUFACTURER_ID

from tests.components.omron.bt import service_info

ADDRESS = "AA:BB:CC:DD:EE:22"
_QUIET_MSD = bytes([0x01, 0x00, 0x00, 0x00])


class _ProbeLink:
    """Stands in for the GATT session the model-number read keeps open."""

    def __init__(self) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


def _info():
    # An unidentifiable name forces the active model-number read.
    return service_info(
        ADDRESS,
        name="BLESmart_0000123",
        manufacturer_data={OMRON_MANUFACTURER_ID: _QUIET_MSD},
    )


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> _ProbeLink:
    link = _ProbeLink()

    def _device(hass, address, *args, **kwargs):
        return BLEDevice(address=address, name="BLESmart_0000123", details={})

    async def _fetch(ble_device, *, keep_session_open: bool = False):
        assert keep_session_open
        return "HEM-7600T", link

    monkeypatch.setattr(config_flow, "async_ble_device_from_address", _device)
    monkeypatch.setattr(config_flow, "async_fetch_device_model_number", _fetch)
    return link


async def _start_discovery(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_info()
    )
    if result["step_id"] == "bluetooth_confirm":
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "select_model"
    return result


async def test_removing_a_flow_discards_its_probe_session(
    hass: HomeAssistant, enable_bluetooth: None, probe: _ProbeLink
) -> None:
    result = await _start_discovery(hass)
    assert probe.closed == 0, "the link must stay parked while the flow is open"

    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()

    assert probe.closed == 1
    assert not hass.data.get(DOMAIN, {}).get("_probe_sessions"), "bucket not emptied"


async def test_device_selection_does_not_bypass_another_flow_in_progress(
    hass: HomeAssistant, enable_bluetooth: None, probe: _ProbeLink
) -> None:
    discovery = await _start_discovery(hass)

    with patch.object(
        config_flow, "async_discovered_service_info", return_value=[_info()]
    ):
        picker = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert picker["step_id"] == "user"
        result = await hass.config_entries.flow.async_configure(
            picker["flow_id"], user_input={"address": ADDRESS}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_in_progress"
    # The discovery flow still owns the probe link.
    assert probe.closed == 0
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["flow_id"] for f in flows] == [discovery["flow_id"]]
