"""Poll coordinator failure and timeout behavior against a real config entry."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from bleak.backends.device import BLEDevice
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.omron import async_poll_data
from tests.components.omron.test_init import ADDRESS, _Hang, _setup, _unload


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
