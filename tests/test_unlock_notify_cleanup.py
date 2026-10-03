"""Unlock notify cleanup must preserve failures and release every channel."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.omron.omron_ble.const import UNLOCK_CHARACTERISTIC_UUID
from custom_components.omron.omron_ble.devices import get_device_config
from custom_components.omron.omron_ble.session import OmronDeviceSession
from custom_components.omron.omron_ble.unlock import _token_unlock
from custom_components.omron.omron_ble import unlock as unlock_module


class FailingStopNotifyClient:
    is_connected = True

    def __init__(self):
        self.callbacks = {}
        self.stopped = []

    async def start_notify(self, characteristic, callback):
        self.callbacks[characteristic] = callback

    async def write_gatt_char(self, characteristic, _payload, response=True):
        self.callbacks[characteristic](None, bytearray(b"\x00"))

    async def stop_notify(self, characteristic):
        self.stopped.append(characteristic)
        if characteristic == UNLOCK_CHARACTERISTIC_UUID:
            raise OSError("unlock CCCD could not be disabled")


@pytest.mark.asyncio
async def test_classic_unlock_stop_error_does_not_mask_failure_or_skip_rx_cleanup(
    monkeypatch,
):
    client = FailingStopNotifyClient()
    session = OmronDeviceSession(
        SimpleNamespace(address="AA:BB:CC:DD:EE:FF"),
        get_device_config("HEM-7155T"),
    )
    session._client = client
    session._debug_ble_link = lambda *_args: None
    monkeypatch.setattr(
        "custom_components.omron.omron_ble.session._NOTIFY_SUBSCRIBE_SETTLE_SEC",
        0,
    )

    with pytest.raises(ConnectionError, match="pairing key mismatch"):
        await session.unlock()

    assert UNLOCK_CHARACTERISTIC_UUID in client.stopped
    assert session.config.rx_channel_uuids[0] in client.stopped


@pytest.mark.asyncio
async def test_token_unlock_setup_failure_still_releases_rx_subscription(monkeypatch):
    client = FailingStopNotifyClient()
    rx_uuid = "test-rx"
    session = SimpleNamespace(
        _client=client,
        _config=SimpleNamespace(rx_channel_uuids=[rx_uuid], model="HEM-test"),
        _ensure_services_cache=AsyncMock(),
        _debug_ble_link=lambda *_args: None,
        _unlocked=False,
        _unlock_notify_handler=None,
        memory=SimpleNamespace(
            _rebuild_notify_handle_index_map=lambda: None,
            _on_notify_channel_data=lambda *_args: None,
            _notify_subscribed=False,
        ),
    )
    monkeypatch.setattr(
        "custom_components.omron.omron_ble.unlock._NOTIFY_SUBSCRIBE_SETTLE_SEC",
        0,
    )

    async def start_notify(_client, characteristic, _callback, **_kwargs):
        if characteristic == UNLOCK_CHARACTERISTIC_UUID:
            raise OSError("unlock CCCD subscribe failed")

    monkeypatch.setattr(
        "custom_components.omron.omron_ble.unlock._start_notify_with_recovery",
        start_notify,
    )

    with pytest.raises(OSError, match="unlock CCCD subscribe failed"):
        await _token_unlock(session)

    assert UNLOCK_CHARACTERISTIC_UUID in client.stopped
    assert rx_uuid in client.stopped


@pytest.mark.asyncio
async def test_cancelled_unlock_bounds_a_hung_notify_cleanup(monkeypatch):
    monkeypatch.setattr(unlock_module, "DISCONNECT_TIMEOUT_S", 0.01)
    started = asyncio.Event()
    cleanup_entered = asyncio.Event()

    class HangingClient:
        async def stop_notify(self, _characteristic):
            cleanup_entered.set()
            await asyncio.Event().wait()

    async def canceled_operation():
        try:
            started.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await unlock_module._stop_notify_best_effort(
                HangingClient(), UNLOCK_CHARACTERISTIC_UUID, "test unlock"
            )

    task = asyncio.create_task(canceled_operation())
    await started.wait()
    task.cancel()
    await asyncio.wait_for(task, timeout=0.2)
    assert cleanup_entered.is_set()
