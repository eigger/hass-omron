"""The token unlock owns its unlock subscription: released, or handed to the session when kept."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from blesession import WriteTimeout
from blesession.testing import FakeClient

from custom_components.omron.omron_ble import unlock as unlock_module
from custom_components.omron.omron_ble.const import UNLOCK_CHARACTERISTIC_UUID as UNLOCK
from custom_components.omron.omron_ble.unlock import _token_unlock

RX = "test-rx"




def _session(client):
    held = []
    session = SimpleNamespace(
        _client=client,
        _config=SimpleNamespace(rx_channel_uuids=[RX], model="HEM-test"),
        _ensure_services_cache=AsyncMock(),
        _debug_ble_link=lambda *_a: None,
        _unlocked=False,
        memory=SimpleNamespace(
            _rebuild_notify_handle_index_map=lambda: None,
            _on_notify_channel_data=lambda *_a: None,
            _notify_subscribed=False,
        ),
        held=held,
    )
    session.hold_unlock_channel = lambda stack, channel: held.append((stack, channel))
    return session


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(unlock_module, "_NOTIFY_SUBSCRIBE_SETTLE_SEC", 0)
    monkeypatch.setattr(unlock_module, "_UNLOCK_AUTH_WAIT_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(unlock_module, "WRITE_TIMEOUT_S", 0.05)


def _answering(client, *, noise=()):
    """A cuff that echoes the token, after any ``noise`` frames."""

    def on_write(uuid, value):
        for frame in noise:
            client.reply(frame, UNLOCK)
        client.reply(b"\x91\x00" + value[1:5], UNLOCK)

    client.on_write = on_write


@pytest.mark.asyncio
async def test_a_plain_token_unlock_releases_both_subscriptions():
    client = FakeClient()
    _answering(client)
    session = _session(client)
    await _token_unlock(session)
    assert session._unlocked
    assert client.subscribed == {}
    assert session.held == []


@pytest.mark.asyncio
async def test_a_kept_token_unlock_hands_the_unlock_subscription_to_the_session():
    client = FakeClient()
    _answering(client)
    session = _session(client)
    await _token_unlock(session, keep_notify=True)
    assert session._unlocked
    assert UNLOCK in client.subscribed, "a kept subscription must stay enabled"
    assert RX in client.subscribed
    (stack, channel), = session.held
    await stack.aclose()  # the session's release
    assert UNLOCK not in client.subscribed


@pytest.mark.asyncio
async def test_a_failed_token_unlock_is_released_even_when_kept():
    client = FakeClient()
    session = _session(client)
    with pytest.raises(ConnectionError, match="notify timeout"):
        await _token_unlock(session, keep_notify=True)
    assert client.subscribed == {}
    assert session.held == []


@pytest.mark.asyncio
async def test_an_unanswered_write_without_response_is_retried_with_response():
    client = FakeClient()
    session = _session(client)
    with pytest.raises(ConnectionError, match="notify timeout"):
        await _token_unlock(session)
    assert [response for _c, _d, response in client.writes] == [False, True]


@pytest.mark.asyncio
async def test_frames_that_are_not_the_token_echo_are_not_the_answer():
    client = FakeClient()
    _answering(client, noise=(b"\x82\x00", b"\x91\x00\x00\x00\x00\x00"))
    session = _session(client)
    await _token_unlock(session)
    assert session._unlocked


@pytest.mark.asyncio
async def test_a_hung_write_is_a_write_timeout_not_a_notify_timeout():
    client = FakeClient()
    client.write_delay_s = 3600
    session = _session(client)
    with pytest.raises(WriteTimeout):
        await _token_unlock(session)
    assert client.subscribed == {}
