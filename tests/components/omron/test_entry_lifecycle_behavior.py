"""BLE work stays bounded by, and owned by, a real loaded config entry.

These run the integration's own setup, advertisement callback, button
service and unload against a real Home Assistant. Only the device boundary
is replaced: the GATT call a session would make blocks until it is cancelled.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from bleak.backends.device import BLEDevice
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.omron import async_poll_data, process_service_info
from custom_components.omron.const import CONF_DEVICE_MODEL, DOMAIN
from custom_components.omron.omron_ble.const import OMRON_MANUFACTURER_ID

from bt import service_info

ADDRESS = "AA:BB:CC:DD:EE:FF"
# Format 0x03 status byte: 0x04 = invalid time, 0x08 = pairing mode.
_INVALID_TIME = bytes([0x03, 0x04, 0x00, 0x00, 0x00])
_PAIRING_MODE = bytes([0x03, 0x08, 0x00, 0x00, 0x00])


class _Hang:
    """A GATT call that never returns on its own; records that it was cancelled."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def __call__(self, *args, **kwargs):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


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
    # Let the setup-time poll (no cuff in range, so it ends at once) finish, so
    # the entry's task set holds only what a test starts.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    async with asyncio.timeout(5):
        while entry.runtime_data.background_tasks:
            await asyncio.sleep(0.01)
    return entry


def _advertise(entry: MockConfigEntry, payload: bytes) -> None:
    entry.runtime_data.last_attempt_time = 0.0  # past the cooldown
    process_service_info(
        entry,
        service_info(
            ADDRESS,
            name="HEM-7155T",
            manufacturer_data={OMRON_MANUFACTURER_ID: payload},
        ),
    )


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_time_sync_session_from_an_advertisement_has_a_deadline(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """A wedged GATT call gives the session lock back instead of holding it."""
    entry = await _setup(hass)
    runtime = entry.runtime_data
    hang = _Hang()
    with (
        patch("custom_components.omron.BLE_SESSION_TIMEOUT_SECONDS", 0.2),
        patch("custom_components.omron.SETTLE_DELAY_SECONDS", 0),
        patch.object(runtime.device_data, "async_sync_time", hang),
    ):
        _advertise(entry, _INVALID_TIME)
        assert runtime.device_data.invalid_time is True
        assert len(runtime.background_tasks) == 1
        task = next(iter(runtime.background_tasks))

        async with asyncio.timeout(5):
            await hang.cancelled.wait()
            await asyncio.wait([task])

    assert task.done() and not task.cancelled()
    assert runtime.session_lock.locked() is False
    assert not runtime.background_tasks
    await _unload(hass, entry)


async def test_pairing_session_from_an_advertisement_has_a_deadline(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = await _setup(hass)
    runtime = entry.runtime_data
    hang = _Hang()
    with (
        patch("custom_components.omron.BLE_SESSION_TIMEOUT_SECONDS", 0.2),
        patch("custom_components.omron.SETTLE_DELAY_SECONDS", 0),
        patch.object(runtime.device_data, "async_retry_pairing", hang),
    ):
        _advertise(entry, _PAIRING_MODE)
        assert runtime.device_data.pairing_mode is True
        task = next(iter(runtime.background_tasks))
        async with asyncio.timeout(5):
            await hang.cancelled.wait()
            await asyncio.wait([task])

    assert runtime.session_lock.locked() is False
    await _unload(hass, entry)


async def test_unload_cancels_an_advertisement_session_and_frees_the_lock(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """The entry owns the session task: unload cancels it and waits for it."""
    entry = await _setup(hass)
    runtime = entry.runtime_data
    hang = _Hang()
    with (
        patch("custom_components.omron.SETTLE_DELAY_SECONDS", 0),
        patch.object(runtime.device_data, "async_sync_time", hang),
    ):
        _advertise(entry, _INVALID_TIME)
        task = next(iter(runtime.background_tasks))
        async with asyncio.timeout(5):
            await hang.started.wait()
        assert runtime.session_lock.locked() is True

        await _unload(hass, entry)

    assert hang.cancelled.is_set()
    assert task.done()
    assert runtime.session_lock.locked() is False
    assert not runtime.background_tasks


async def test_unload_waits_for_tasks_before_discarding_parked_sessions(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """A session task cannot park a new link after the parked ones are discarded."""
    entry = await _setup(hass)
    runtime = entry.runtime_data
    hang = _Hang()
    seen: list[bool] = []

    async def discard(hass_arg: HomeAssistant, address: str) -> None:
        seen.append(hang.cancelled.is_set() and not runtime.session_lock.locked())

    with (
        patch("custom_components.omron.SETTLE_DELAY_SECONDS", 0),
        patch.object(runtime.device_data, "async_sync_time", hang),
        patch("custom_components.omron.discard_handoff_session", discard),
    ):
        _advertise(entry, _INVALID_TIME)
        async with asyncio.timeout(5):
            await hang.started.wait()
        await _unload(hass, entry)

    assert seen == [True]


async def test_poll_has_a_deadline_and_fails_the_refresh(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """A poll stuck inside the BLE stack times out, frees the lock, marks failure."""
    entry = await _setup(hass)
    runtime = entry.runtime_data
    hang = _Hang()
    device = BLEDevice(ADDRESS, "HEM-7155T", {})
    with (
        patch("custom_components.omron.POLL_TIMEOUT_SECONDS", 0.2),
        patch(
            "custom_components.omron.async_ble_device_from_address", return_value=device
        ),
        patch.object(runtime.device_data, "async_poll", hang),
    ):
        async with asyncio.timeout(5):
            with pytest.raises(TimeoutError):
                await async_poll_data(hass, entry)

    assert hang.cancelled.is_set()
    assert runtime.session_lock.locked() is False
    await _unload(hass, entry)


async def test_pairing_button_has_a_deadline(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = await _setup(hass)
    runtime = entry.runtime_data
    hang = _Hang()
    device = BLEDevice(ADDRESS, "HEM-7155T", {})
    with (
        patch("custom_components.omron.button.BLE_SESSION_TIMEOUT_SECONDS", 0.2),
        patch(
            "custom_components.omron.button.async_ble_device_from_address",
            return_value=device,
        ),
        patch(
            "custom_components.omron.button.poll_parked_session",
            AsyncMock(return_value=False),
        ),
        patch.object(runtime.device_data, "async_retry_pairing", hang),
    ):
        async with asyncio.timeout(5):
            with pytest.raises(HomeAssistantError, match="Failed to retry pairing"):
                await hass.services.async_call(
                    "button",
                    "press",
                    {"entity_id": "button.hem_7155t_eeff_retry_pairing"},
                    blocking=True,
                )

    assert hang.cancelled.is_set()
    assert runtime.session_lock.locked() is False
    assert not runtime.background_tasks
    await _unload(hass, entry)


async def test_pairing_button_operation_is_cancelled_by_unload(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """A press is a service call, not an entry task: it must still be tracked."""
    entry = await _setup(hass)
    runtime = entry.runtime_data
    hang = _Hang()
    device = BLEDevice(ADDRESS, "HEM-7155T", {})
    with (
        patch(
            "custom_components.omron.button.async_ble_device_from_address",
            return_value=device,
        ),
        patch(
            "custom_components.omron.button.poll_parked_session",
            AsyncMock(return_value=False),
        ),
        patch.object(runtime.device_data, "async_retry_pairing", hang),
    ):
        press = hass.async_create_task(
            hass.services.async_call(
                "button",
                "press",
                {"entity_id": "button.hem_7155t_eeff_retry_pairing"},
                blocking=True,
            )
        )
        async with asyncio.timeout(5):
            await hang.started.wait()
        assert len(runtime.background_tasks) == 1

        await _unload(hass, entry)

        await asyncio.wait([press], timeout=5)

    assert hang.cancelled.is_set()
    assert runtime.session_lock.locked() is False
    assert not runtime.background_tasks


async def test_setup_does_not_wait_for_the_first_poll(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """Setup returns with every platform loaded while the first poll is still
    blocked, and that poll only starts once the entities exist."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="HEM-7155T EEFF",
        data={CONF_DEVICE_MODEL: "HEM-7155T"},
    )
    entry.add_to_hass(hass)
    registered_at_poll: list[int] = []
    started = asyncio.Event()

    async def blocked_poll(*args, **kwargs):
        registered_at_poll.append(
            len(er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id))
        )
        started.set()
        await asyncio.Event().wait()

    with patch("custom_components.omron.async_poll_data", blocked_poll):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert not started.is_set()

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
        async with asyncio.timeout(5):
            await started.wait()
        assert registered_at_poll[0] >= 8

        await _unload(hass, entry)


async def test_failed_poll_surfaces_as_update_failed(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """An ordinary BLE error is an expected UpdateFailed, not a stack trace."""
    entry = await _setup(hass)
    runtime = entry.runtime_data
    device = BLEDevice(ADDRESS, "HEM-7155T", {})
    with (
        patch(
            "custom_components.omron.async_ble_device_from_address", return_value=device
        ),
        patch.object(
            runtime.device_data, "async_poll", AsyncMock(side_effect=OSError("asleep"))
        ),
    ):
        with pytest.raises(UpdateFailed, match="asleep"):
            await async_poll_data(hass, entry)
    assert runtime.session_lock.locked() is False
    await _unload(hass, entry)


async def test_unload_cancels_a_forced_transfer_drain_waiting_on_the_lock(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """The latched forced-transfer drain is entry-owned: unload cancels it while
    it waits for the session lock, and it is tracked with the other tasks."""
    entry = await _setup(hass)
    runtime = entry.runtime_data
    await runtime.session_lock.acquire()
    try:
        # Format 0x01, forced-transfer bit (0x40), no registered users.
        _advertise(entry, bytes([0x01, 0x40, 0x00, 0x00]))
        task = runtime.pending_forced_transfer_task
        assert task is not None and not task.done()
        assert task in runtime.background_tasks

        await _unload(hass, entry)
    finally:
        runtime.session_lock.release()

    assert task.cancelled()
    assert runtime.pending_forced_transfer_task is None
    assert not runtime.background_tasks
