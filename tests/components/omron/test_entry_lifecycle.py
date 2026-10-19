"""Regression coverage for BLE work tied to Home Assistant entry lifetimes.

That setup returns before the first poll, and that the first poll only starts
once every platform's entities exist, is covered by
``test_setup_does_not_wait_for_the_first_poll`` in
``test_entry_lifecycle_behavior.py``. This file keeps the lock sharing and the
update-listener ordering around the initial poll.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.omron import _session_lock_for
from custom_components.omron.const import (
    CONF_DEVICE_MODEL,
    CONF_TRANSPORT_CREDENTIAL,
    DOMAIN,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"


def test_session_lock_is_shared_across_runtime_recreation():
    hass = SimpleNamespace(data={})
    first = _session_lock_for(hass, "AA:BB:CC:DD:EE:FF")
    second = _session_lock_for(hass, "AA:BB:CC:DD:EE:FF")
    other = _session_lock_for(hass, "AA:BB:CC:DD:EE:00")

    assert first is second
    assert first is not other


async def test_a_credential_stored_by_the_initial_poll_does_not_reload_the_entry(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """The update listener is registered before the initial poll runs, so the
    poll's own credential write is consumed instead of reloading the entry out
    from under the coordinator that is still running that poll."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="HEM-7155T EEFF",
        data={CONF_DEVICE_MODEL: "HEM-7155T"},
    )
    entry.add_to_hass(hass)
    finished = asyncio.Event()
    runtimes = []

    async def poll_that_establishes_a_credential(hass_arg, entry_arg):
        runtime = entry_arg.runtime_data
        runtimes.append(runtime)
        # What the real poll does once a session produced a credential.
        runtime.device_data.pending_credential = bytes(range(16))
        from custom_components.omron import _persist_transport_credential

        _persist_transport_credential(hass_arg, entry_arg, runtime.device_data)
        await hass_arg.async_block_till_done()
        finished.set()
        return runtime.device_data._finish_update()

    with (
        patch(
            "custom_components.omron.async_poll_data",
            poll_that_establishes_a_credential,
        ),
        patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload_entry,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert not finished.is_set()

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
        async with asyncio.timeout(5):
            await finished.wait()
        await hass.async_block_till_done()

        assert entry.data[CONF_TRANSPORT_CREDENTIAL] == bytes(range(16)).hex()
        reload_entry.assert_not_awaited()
        # The listener ran and consumed the marker.
        assert runtimes[0].credential_write is False
        assert entry.runtime_data is runtimes[0]

        # An ordinary options change afterwards still reloads.
        hass.config_entries.async_update_entry(entry, options={"scan_interval": 600})
        await hass.async_block_till_done()
        reload_entry.assert_awaited_once()

        assert await hass.config_entries.async_unload(entry.entry_id)
