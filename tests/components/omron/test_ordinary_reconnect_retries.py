"""Regression test: an ordinary reconnect must spend its retry budget.

``establish_connection_with_bond_settle`` takes ``max_attempts`` and its
docstring frames the loop as retrying connection trouble. But on an ordinary
reconnect to an already-bonded device (``pair_on_connect`` False, which is
every scheduled poll and every "Refresh Data" press once pairing is done),
the plain ``client = await establish_connection(...)`` call sat outside any
try/except. A transient failure there -- ``bleak_retry_connector`` giving up
with "failed to discover services, device disconnected" after its own
internal attempts -- propagated straight out of the loop on its first
iteration. ``max_attempts`` only ever covered the other case below it (a
connect that briefly succeeds and then drops during the post-connect
settle), never a connect that fails outright.

Only the external connection factory and timing constants are replaced. The
shared ``mock_client`` fixture supplies the returned BLE link; the real retry
and settle implementation runs unchanged. Synchronous protocol tests use
``asyncio.run`` to drive that implementation.
"""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.omron.omron_ble import connection


def test_a_connect_failure_on_an_ordinary_reconnect_is_retried(
    monkeypatch, mock_client
):
    class _BlueZError(Exception):
        pass

    monkeypatch.setattr(connection, "BleakError", _BlueZError)
    monkeypatch.setattr(connection, "_POST_CONNECT_BOND_SETTLE_SEC", 0)
    monkeypatch.setattr(connection, "_SETTLE_POLL_STEP_SEC", 0)

    calls = 0

    async def fake_establish_connection(client_class, ble_device, name, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _BlueZError(
                "X - X: Failed to connect after 4 attempt(s): failed to "
                "discover services, device disconnected"
            )
        return mock_client

    monkeypatch.setattr(connection, "establish_connection", fake_establish_connection)

    async def _run():
        ble_device = SimpleNamespace(details={})
        return await connection.establish_connection_with_bond_settle(
            ble_device, "test-device", model="HEM-TEST", max_attempts=3
        )

    client = asyncio.run(_run())

    assert calls == 2
    assert client is mock_client


def test_the_retry_budget_is_not_unlimited(monkeypatch):
    class _BlueZError(Exception):
        pass

    monkeypatch.setattr(connection, "BleakError", _BlueZError)
    monkeypatch.setattr(connection, "_POST_CONNECT_BOND_SETTLE_SEC", 0)
    monkeypatch.setattr(connection, "_SETTLE_POLL_STEP_SEC", 0)

    calls = 0

    async def always_fails(client_class, ble_device, name, **kwargs):
        nonlocal calls
        calls += 1
        raise _BlueZError("failed to discover services, device disconnected")

    monkeypatch.setattr(connection, "establish_connection", always_fails)

    async def _run():
        ble_device = SimpleNamespace(details={})
        await connection.establish_connection_with_bond_settle(
            ble_device, "test-device", model="HEM-TEST", max_attempts=3
        )

    with pytest.raises(Exception):
        asyncio.run(_run())

    assert calls == 3
