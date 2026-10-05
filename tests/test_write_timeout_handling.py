"""blesession.guarded_write 의 WriteTimeout 이 이 통합의 재시도·오류 분기에서 잘못 분류되지 않는지."""

from __future__ import annotations


from types import SimpleNamespace

import pytest
from blesession import WriteTimeout, guarded_write
from blesession.testing import FakeClient

from custom_components.omron.omron_ble import session as session_module
from custom_components.omron.omron_ble import unlock as unlock_module
from custom_components.omron.omron_ble.const import UNLOCK_CHARACTERISTIC_UUID as UNLOCK_UUID
from custom_components.omron.omron_ble.devices import get_device_config
from custom_components.omron.omron_ble.session import OmronDeviceSession


class _UnlockClient(FakeClient):
    """A cuff that answers the 0x01 unlock write with ``reply`` (or stays silent)."""

    def __init__(self, reply: bytes | None) -> None:
        super().__init__()
        self.on_write = None if reply is None else (lambda _c, _d: self.reply(reply, UNLOCK_UUID))


def _session(client) -> OmronDeviceSession:
    session = OmronDeviceSession(
        SimpleNamespace(address="AA:BB:CC:DD:EE:FF"), get_device_config("HEM-7155T")
    )
    session._client = client
    session._debug_ble_link = lambda *_args: None
    return session


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(session_module, "_NOTIFY_SUBSCRIBE_SETTLE_SEC", 0)
    monkeypatch.setattr(session_module, "_UNLOCK_AUTH_WAIT_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(unlock_module, "_UNLOCK_PROBE_WAIT_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(session_module, "WRITE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(unlock_module, "WRITE_TIMEOUT_S", 0.05)


@pytest.mark.asyncio
async def test_classic_unlock_succeeds_and_releases_both_subscriptions():
    client = _UnlockClient(b"\x81")
    session = _session(client)
    await session.unlock()
    assert session._unlocked
    assert client.subscribed == {}
    assert [data[0] for _c, data, _r in client.writes] == [0x02, 0x01]  # aggressive profile: probe, then unlock


@pytest.mark.asyncio
async def test_classic_unlock_with_a_silent_cuff_is_a_notify_timeout():
    client = _UnlockClient(None)
    with pytest.raises(ConnectionError, match="notify timeout"):
        await _session(client).unlock()
    assert client.subscribed == {}


@pytest.mark.asyncio
async def test_classic_unlock_with_a_rejected_key_is_a_key_mismatch():
    client = _UnlockClient(b"\x00")
    with pytest.raises(ConnectionError, match="pairing key mismatch"):
        await _session(client).unlock()
    assert client.subscribed == {}


@pytest.mark.asyncio
async def test_classic_unlock_does_not_turn_a_write_timeout_into_a_notify_timeout():
    """A write that never returned is the adapter or proxy, not a silent cuff."""
    client = _UnlockClient(b"\x81")
    client.write_delay_s = 3600
    with pytest.raises(WriteTimeout):
        await _session(client).unlock()
    assert client.subscribed == {}


@pytest.mark.asyncio
async def test_a_hung_write_raises_write_timeout_naming_the_step():
    client = FakeClient()
    client.write_delay_s = 3600
    with pytest.raises(WriteTimeout) as raised:
        await guarded_write(client, "uuid", b"\x01", step="unlock", response=True, timeout=0.01)
    assert raised.value.step == "unlock"
    assert isinstance(raised.value, TimeoutError)
