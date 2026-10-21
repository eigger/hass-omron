"""Pairing button deadlines and entry-owned cancellation through HA services."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from bleak.backends.device import BLEDevice
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from tests.components.omron.test_init import ADDRESS, _Hang, _setup, _unload


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
