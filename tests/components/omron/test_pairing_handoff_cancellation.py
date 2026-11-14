"""Cancellation during the probe-to-pairing handoff."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from unittest.mock import AsyncMock

from bleak.backends.device import BLEDevice
from homeassistant.core import HomeAssistant
import pytest

from custom_components.omron import config_flow
from custom_components.omron.const import DOMAIN
from custom_components.omron.config_flow import get_device_config
from custom_components.omron.omron_ble.session import OmronDeviceSession
from custom_components.omron.session_handoff import stash_probe_session
from tests.components.omron.bt import service_info


async def test_cancel_during_agent_release_closes_taken_probe(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled handoff cannot pair or park a session after releasing its agent."""
    address = "AA:BB:CC:DD:EE:11"
    releasing = asyncio.Event()
    never = asyncio.Event()
    exits = 0

    @asynccontextmanager
    async def agent():
        nonlocal exits
        try:
            yield
        finally:
            exits += 1
            releasing.set()
            await never.wait()

    class Client:
        is_connected = True

        def __init__(self):
            self.address = address
            self.disconnect = AsyncMock(side_effect=self._disconnect)

        async def _disconnect(self):
            self.is_connected = False

    client = Client()
    probe = OmronDeviceSession.adopt(client, get_device_config("HEM-7155T"))
    probe.reclaim_ownership()
    stack = AsyncExitStack()
    await stack.enter_async_context(agent())
    probe._pairing_agent = stack
    stash_probe_session(hass, address, probe)

    flow = config_flow.OmronConfigFlow()
    flow.hass = hass
    flow._selected_model = "HEM-7155T"
    flow._discovery_info = service_info(address=address, name="HEM-7155T")
    monkeypatch.setattr(
        config_flow,
        "async_ble_device_from_address",
        lambda *_args: BLEDevice(address, "HEM-7155T", {}),
    )
    pair = AsyncMock()
    monkeypatch.setattr(config_flow, "async_pair_and_sync_device", pair)

    task = asyncio.create_task(flow._async_do_pairing())
    await asyncio.wait_for(releasing.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    client.disconnect.assert_awaited_once()
    pair.assert_not_awaited()
    assert exits == 1
    assert not hass.data[DOMAIN].get("_probe_sessions")
    assert not hass.data[DOMAIN].get("_setup_sessions")
