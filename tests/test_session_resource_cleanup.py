"""Regression tests for BLE resources during cancellation and teardown."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from custom_components.omron.omron_ble import connection as connection_module
from custom_components.omron.omron_ble import session as session_module
from custom_components.omron.omron_ble.devices import get_device_config
from custom_components.omron.omron_ble.session import OmronDeviceSession


class _Client:
    def __init__(self):
        self.is_connected = True
        self.disconnected = False

    async def disconnect(self):
        self.disconnected = True
        self.is_connected = False


def test_cancel_during_post_connect_refresh_disconnects_client(monkeypatch):
    client = _Client()
    monkeypatch.setattr(connection_module, "_POST_CONNECT_BOND_SETTLE_SEC", 0)
    monkeypatch.setattr(connection_module, "_SETTLE_POLL_STEP_SEC", 0)

    async def establish(*args, **kwargs):
        return client

    async def cancelled_refresh(_client):
        raise asyncio.CancelledError

    monkeypatch.setattr(connection_module, "establish_connection", establish)
    monkeypatch.setattr(connection_module, "_bleak_refresh_services", cancelled_refresh)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await connection_module.establish_connection_with_bond_settle(
                SimpleNamespace(details={}), "test"
            )

    asyncio.run(run())
    assert client.disconnected


def test_cancel_cleanup_bounds_disconnect(monkeypatch):
    client = _Client()

    async def hanging_disconnect():
        await asyncio.sleep(3600)

    client.disconnect = hanging_disconnect
    monkeypatch.setattr(connection_module, "DISCONNECT_TIMEOUT_S", 0.02)
    monkeypatch.setattr(connection_module, "_POST_CONNECT_BOND_SETTLE_SEC", 0)
    monkeypatch.setattr(connection_module, "_SETTLE_POLL_STEP_SEC", 0)

    async def establish(*args, **kwargs):
        return client

    async def cancelled_refresh(_client):
        raise asyncio.CancelledError

    monkeypatch.setattr(connection_module, "establish_connection", establish)
    monkeypatch.setattr(connection_module, "_bleak_refresh_services", cancelled_refresh)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await connection_module.establish_connection_with_bond_settle(
                SimpleNamespace(details={}), "test"
            )

    asyncio.run(asyncio.wait_for(run(), timeout=0.5))


def test_failed_connect_releases_session_pairing_agent(monkeypatch):
    closed = False

    @asynccontextmanager
    async def pairing_agent():
        yield None

    async def fail_connect(*args, **kwargs):
        raise ConnectionError("connect failed")

    monkeypatch.setattr(session_module, "_bluez_pairing_agent", pairing_agent)
    monkeypatch.setattr(session_module, "is_local_adapter", lambda _device: True)
    monkeypatch.setattr(session_module, "establish_connection_with_bond_settle", fail_connect)

    session = OmronDeviceSession(
        SimpleNamespace(address="AA:BB:CC:DD:EE:FF"),
        get_device_config("HEM-7188T1-LEO"),
    )
    # Replace the stack opened by connect with a sentinel whose release is visible.
    class Stack:
        async def enter_async_context(self, context):
            await context.__aenter__()

        async def aclose(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr(session_module, "AsyncExitStack", Stack)

    async def run():
        with pytest.raises(ConnectionError, match="connect failed"):
            await session.connect()

    asyncio.run(run())
    assert closed


def test_aclose_bounds_memory_close(monkeypatch):
    from custom_components.omron.omron_ble import memory_protocol as memory_module

    monkeypatch.setattr(session_module, "DISCONNECT_TIMEOUT_S", 0.02)
    monkeypatch.setattr(memory_module, "DISCONNECT_TIMEOUT_S", 0.02)
    client = _Client()

    async def hang(*args, **kwargs):
        await asyncio.sleep(3600)

    client.write_gatt_char = hang
    client.stop_notify = hang
    session = OmronDeviceSession(
        SimpleNamespace(address="AA:BB:CC:DD:EE:FF"), get_device_config("HEM-7155T")
    )
    session._client = client
    session.memory._memory_session_active = True
    asyncio.run(asyncio.wait_for(session.aclose(), timeout=0.5))
    assert session._client is None
    assert client.disconnected
