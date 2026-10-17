"""Regression contract for BP5465 poll reliability semantics.

Each of these runs the integration's own code. The forced-transfer latch, the
poll error propagation and the forced-transfer device-disappearance path are
exercised by ``test_bp5465_reliability_behavior.py`` and
``test_entry_lifecycle_behavior.py``; what is added here is the explicit
forced-transfer wait behind a live session lock and the parser's fail-closed
paths.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from bleak.backends.device import BLEDevice
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.omron import async_poll_data
from custom_components.omron.const import CONF_DEVICE_MODEL, DOMAIN

from poll_fakes import (
    ADDRESS,
    FakeSession,
    ble_device,
    parser_with_session,
    raising,
    succeed,
)


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="HEM-7155T EEFF",
        data={CONF_DEVICE_MODEL: "HEM-7155T"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    async with asyncio.timeout(5):
        while entry.runtime_data.background_tasks:
            await asyncio.sleep(0.01)
    return entry


async def test_explicit_forced_transfer_can_wait_behind_one_active_session(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """A forced-transfer refresh queues for the lock; an ordinary poll skips."""
    entry = await _setup(hass)
    runtime = entry.runtime_data
    device = BLEDevice(ADDRESS, "HEM-7155T", {})
    update = runtime.device_data._finish_update()
    poll = AsyncMock(return_value=update)
    with (
        patch(
            "custom_components.omron.async_ble_device_from_address", return_value=device
        ),
        patch.object(runtime.device_data, "async_poll", poll),
    ):
        await runtime.session_lock.acquire()
        try:
            # Ordinary scheduled poll: try-acquire only, no BLE session.
            await async_poll_data(hass, entry)
            poll.assert_not_awaited()

            # Forced transfer: waits for the active owner instead of skipping.
            runtime.force_poll_after_lock = True
            forced = asyncio.create_task(async_poll_data(hass, entry))
            await asyncio.sleep(0.05)
            assert not forced.done()
            poll.assert_not_awaited()
        finally:
            runtime.session_lock.release()

        async with asyncio.timeout(5):
            await forced

    poll.assert_awaited_once()
    assert runtime.force_poll_after_lock is False
    assert runtime.session_lock.locked() is False
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_parser_connection_failure_is_a_warning_and_propagates(caplog):
    session = FakeSession(unlock_steps=[raising(ConnectionError("link dropped"))])
    session.has_service = AsyncMock(side_effect=ConnectionError("link dropped"))
    data = parser_with_session(session)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ConnectionError, match="link dropped"):
            await data.async_poll(ble_device())

    levels = {r.levelno for r in caplog.records if "Poll " in r.getMessage()}
    assert logging.WARNING in levels and logging.ERROR not in levels


async def test_parser_propagates_unexpected_poll_failures(caplog):
    session = FakeSession()
    session.has_service = AsyncMock(side_effect=RuntimeError("synthetic boom"))
    data = parser_with_session(session)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RuntimeError, match="synthetic boom"):
            await data.async_poll(ble_device())

    failed = [r for r in caplog.records if "Poll failed model=" in r.getMessage()]
    assert failed and failed[0].levelno == logging.ERROR


async def test_missing_required_service_fails_closed():
    session = FakeSession(has_service=False)
    data = parser_with_session(session)

    with pytest.raises(ConnectionError, match="Required service"):
        await data.async_poll(ble_device())

    data._poll_device_readout.assert_not_awaited()
    assert session.trace.failed_stage == "services"


async def test_fallback_memory_readout_failure_fails_the_poll(monkeypatch, caplog):
    """Three failed memory-session opens, then a fallback readout that fails:
    the poll fails rather than returning a quiet success."""
    from custom_components.omron.omron_ble import parser as parser_module

    real_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await real_sleep(0)

    monkeypatch.setattr(parser_module.asyncio, "sleep", fast_sleep)

    steps = [raising(OSError(f"open {n}")) for n in range(3)]
    steps.append(raising(OSError("fallback readout broke")))
    session = FakeSession(unlock_steps=steps)
    data = parser_with_session(session)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(OSError, match="fallback readout broke"):
            await data.async_poll(ble_device())

    assert session.unlock_calls == 4
    assert any(
        "Fallback memory session readout failed" in r.getMessage()
        for r in caplog.records
    )
    data._poll_device_readout.assert_not_awaited()


async def test_a_successful_memory_session_open_reads_out_and_returns(monkeypatch):
    """The failure cases above only mean something if success still succeeds."""
    session = FakeSession(unlock_steps=[succeed])
    data = parser_with_session(session)

    result = await data.async_poll(ble_device())

    assert result is not None
    data._poll_device_readout.assert_awaited_once()
