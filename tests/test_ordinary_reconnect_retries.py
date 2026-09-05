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

conftest replaces bleak/bleak_retry_connector with MagicMock, so the two
names this test needs to control -- ``BleakError`` and ``establish_connection``
-- are monkeypatched on the module the same way test_session_cccd_persistence
already does for ``BleakError``, and async functions run via ``asyncio.run``
rather than a pytest-asyncio plugin, matching the rest of this test suite.
"""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.omron.omron_ble import omron_driver


class _FakeClient:
    """Enough of a BleakClient for the settle/refresh steps to no-op cleanly."""

    def __init__(self):
        self.is_connected = True

    async def disconnect(self):
        pass


def test_a_connect_failure_on_an_ordinary_reconnect_is_retried(monkeypatch):
    class _BlueZError(Exception):
        pass

    monkeypatch.setattr(omron_driver, "BleakError", _BlueZError)
    monkeypatch.setattr(omron_driver, "_POST_CONNECT_BOND_SETTLE_SEC", 0)
    monkeypatch.setattr(omron_driver, "_SETTLE_POLL_STEP_SEC", 0)

    calls = 0

    async def fake_establish_connection(client_class, ble_device, name, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _BlueZError(
                "X - X: Failed to connect after 4 attempt(s): failed to "
                "discover services, device disconnected"
            )
        return _FakeClient()

    monkeypatch.setattr(omron_driver, "establish_connection", fake_establish_connection)

    async def _run():
        ble_device = SimpleNamespace(details={})
        return await omron_driver.establish_connection_with_bond_settle(
            ble_device, "test-device", model="HEM-TEST", max_attempts=3
        )

    client = asyncio.run(_run())

    assert calls == 2
    assert isinstance(client, _FakeClient)


def test_the_retry_budget_is_not_unlimited(monkeypatch):
    class _BlueZError(Exception):
        pass

    monkeypatch.setattr(omron_driver, "BleakError", _BlueZError)
    monkeypatch.setattr(omron_driver, "_POST_CONNECT_BOND_SETTLE_SEC", 0)
    monkeypatch.setattr(omron_driver, "_SETTLE_POLL_STEP_SEC", 0)

    calls = 0

    async def always_fails(client_class, ble_device, name, **kwargs):
        nonlocal calls
        calls += 1
        raise _BlueZError("failed to discover services, device disconnected")

    monkeypatch.setattr(omron_driver, "establish_connection", always_fails)

    async def _run():
        ble_device = SimpleNamespace(details={})
        await omron_driver.establish_connection_with_bond_settle(
            ble_device, "test-device", model="HEM-TEST", max_attempts=3
        )

    with pytest.raises(Exception):
        asyncio.run(_run())

    assert calls == 3
