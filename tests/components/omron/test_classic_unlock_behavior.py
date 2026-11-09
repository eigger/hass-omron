"""Classic unlock behavior tests: 0x02 probe scoping and write-response fallback."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from blesession.testing import FakeClient

from custom_components.omron.omron_ble import session as session_module
from custom_components.omron.omron_ble import unlock as unlock_module
from custom_components.omron.omron_ble.const import UNLOCK_CHARACTERISTIC_UUID as UNLOCK
from custom_components.omron.omron_ble.devices import DeviceConfig, HostPairingMode
from custom_components.omron.omron_ble.session import OmronDeviceSession


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(session_module, "_NOTIFY_SUBSCRIBE_SETTLE_SEC", 0)
    monkeypatch.setattr(session_module, "_UNLOCK_AUTH_WAIT_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(session_module, "WRITE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(unlock_module, "_NOTIFY_SUBSCRIBE_SETTLE_SEC", 0)
    monkeypatch.setattr(unlock_module, "_UNLOCK_PROBE_WAIT_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(unlock_module, "WRITE_TIMEOUT_S", 0.05)


def _classic_session(client, *, aggressive: bool = False) -> OmronDeviceSession:
    config = DeviceConfig(
        model="HEM-7155T",
        host_pairing_mode=HostPairingMode.CUSTOM_KEY,
        aggressive_gatt_timing=aggressive,
        rx_channel_uuids=["test-rx"],
        tx_channel_uuids=["test-tx"],
    )
    session = OmronDeviceSession(
        SimpleNamespace(address="AA:BB:CC:DD:EE:FF"),
        config,
    )
    session._client = client
    session._ensure_services_cache = AsyncMock()
    session._debug_ble_link = lambda *_a: None
    return session


@pytest.mark.asyncio
async def test_probe_skipped_when_not_aggressive():
    client = FakeClient()
    session = _classic_session(client, aggressive=False)

    def on_write(uuid, value):
        if value[0] == 0x01:
            client.reply(b"\x81\x00", UNLOCK)

    client.on_write = on_write
    await session.unlock(b"\x11" * 16)

    assert session._unlocked
    writes = client.writes
    # Probe 0x02 must NOT have been sent
    assert not any(data[0] == 0x02 for _u, data, _r in writes)
    # Only auth key write 0x01
    assert any(data[0] == 0x01 for _u, data, _r in writes)


@pytest.mark.asyncio
async def test_probe_sent_when_aggressive():
    client = FakeClient()
    session = _classic_session(client, aggressive=True)

    def on_write(uuid, value):
        if value[0] == 0x02:
            client.reply(b"\x82\x00", UNLOCK)
        elif value[0] == 0x01:
            client.reply(b"\x81\x00", UNLOCK)

    client.on_write = on_write
    await session.unlock(b"\x11" * 16)

    assert session._unlocked
    writes = client.writes
    assert any(data[0] == 0x02 for _u, data, _r in writes)
    assert any(data[0] == 0x01 for _u, data, _r in writes)


@pytest.mark.asyncio
async def test_auth_write_retries_with_response_on_notification_timeout():
    client = FakeClient()
    session = _classic_session(client, aggressive=False)

    attempts = 0

    def on_write(uuid, value):
        nonlocal attempts
        if value[0] == 0x01:
            attempts += 1
            # First attempt (response=False): drop notification
            # Second attempt (response=True): send ack
            if attempts == 2:
                client.reply(b"\x81\x00", UNLOCK)

    client.on_write = on_write
    await session.unlock(b"\x11" * 16)

    assert session._unlocked
    auth_writes = [
        (data[0], resp) for _u, data, resp in client.writes if data[0] == 0x01
    ]
    # First attempt response=False, second attempt response=True
    assert auth_writes == [(0x01, False), (0x01, True)]


@pytest.mark.asyncio
async def test_auth_write_fails_when_both_retries_time_out():
    client = FakeClient()
    session = _classic_session(client, aggressive=False)

    with pytest.raises(ConnectionError, match="Unlock failed: notify timeout"):
        await session.unlock(b"\x11" * 16)

    assert not session._unlocked
    auth_writes = [
        (data[0], resp) for _u, data, resp in client.writes if data[0] == 0x01
    ]
    assert auth_writes == [(0x01, False), (0x01, True)]
