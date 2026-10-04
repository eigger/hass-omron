"""The session's held unlock subscription: handed over, replaced, released; and the secure flow on it."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from blesession import Notifications, WriteTimeout
from blesession.testing import FakeClient

from custom_components.omron.omron_ble import secure_flow
from custom_components.omron.omron_ble.const import UNLOCK_CHARACTERISTIC_UUID as UNLOCK
from custom_components.omron.omron_ble.devices import UnlockMode, get_device_config
from custom_components.omron.omron_ble.secure_flow import (
    ASYNC_NOTICE_UUID,
    establish_secure_session,
)
from custom_components.omron.omron_ble.session import OmronDeviceSession


async def _open(client, uuid=UNLOCK):
    stack = AsyncExitStack()
    channel = await stack.enter_async_context(Notifications(client, uuid))
    return stack, channel


def _session(client):
    session = OmronDeviceSession(
        SimpleNamespace(address="AA:BB:CC:DD:EE:FF"), get_device_config("HEM-7188T1-LEO")
    )
    session._client = client
    session._debug_ble_link = lambda *_a: None
    return session


@pytest.mark.asyncio
async def test_a_held_channel_stays_subscribed_until_released():
    client = FakeClient()
    session = _session(client)
    stack, channel = await _open(client)
    session.hold_unlock_channel(stack, channel)
    assert session._unlock_channel is channel
    assert UNLOCK in client.subscribed
    await session.release_unlock_channel()
    assert session._unlock_channel is None and session._unlock_stack is None
    assert client.subscribed == {}


@pytest.mark.asyncio
async def test_holding_a_new_channel_replaces_the_old_one_without_closing_it():
    """Closing the old stack would unsubscribe the characteristic the new one just subscribed."""
    client = FakeClient()
    session = _session(client)
    first = await _open(client)
    session.hold_unlock_channel(*first)
    second = await _open(client)  # same characteristic, subscribed again
    session.hold_unlock_channel(*second)
    assert UNLOCK in client.subscribed
    await session.release_unlock_channel()
    assert client.subscribed == {}


@pytest.mark.asyncio
async def test_reset_session_state_releases_the_held_channel():
    client = FakeClient()
    session = _session(client)
    session.hold_unlock_channel(*await _open(client))
    await client.start_notify(ASYNC_NOTICE_UUID, lambda *_: None)
    await session.reset_session_state()
    assert client.subscribed == {}
    assert session._unlock_channel is None


# -- the secure flow on a real Notifications ---------------------------------------


class _Crypto:
    def __init__(self, stored_ltk=None):
        self.ltk = stored_ltk or bytes(range(16))

    def build_start_enc_req(self):
        return b"\x70\x05"

    def build_challenge_req(self, response):
        return b"\x70\x06"

    def process_challenge_resp(self, response):
        pass

    def encrypt(self, payload):
        return b"\xc0"

    def decrypt(self, payload):
        return b"\xa0\x01\x00\x01"


_ANSWERS = {b"\x70\x05": b"\xf0\x85", b"\x70\x06": b"\xf0\x86", b"\xc0": b"\xc0"}


class _Client(FakeClient):
    async def read_gatt_char(self, uuid):
        return b"synthetic"


def _secure_session(client):
    session = _session(client)
    session._config = SimpleNamespace(
        model="HEM-7188T1-LEO",
        unlock_mode=UnlockMode.SECURE_SESSION,
        rx_channel_uuids=["rx"],
    )
    session._pairing_session = False
    session.memory = SimpleNamespace(
        _rebuild_notify_handle_index_map=lambda: None,
        _on_notify_channel_data=lambda *_: None,
        _notify_subscribed=False,
    )

    async def ensure_cache():
        return None

    session._ensure_services_cache = ensure_cache
    return session


def _cuff(client):
    def on_write(uuid, data):
        if data[0] == 0x11:
            client.reply(b"\x91\x00" + data[1:5], UNLOCK)
        else:
            client.reply(_ANSWERS[bytes(data)], UNLOCK)

    client.on_write = on_write


def _establish(session):
    async def run():
        return await establish_secure_session(
            session, stored_ltk=bytes(range(16)), now=datetime(2026, 9, 6), timeout=0.5
        )

    with patch.object(secure_flow, "SecureSession", _Crypto), patch.object(
        secure_flow, "start_notify_with_recovery", _noop_subscribe
    ), patch.object(secure_flow.asyncio, "sleep", _no_sleep):
        return asyncio.run(run())


async def _noop_subscribe(*_a, **_k):
    return None


async def _no_sleep(_seconds):
    return None


def test_the_secure_flow_runs_token_and_exchanges_on_one_held_subscription():
    client = _Client()
    _cuff(client)
    session = _secure_session(client)
    assert _establish(session) == bytes(range(16))
    commands = [data[0] for _c, data, _r in client.writes]
    assert commands == [0x11, 0x70, 0x70, 0xC0]
    assert session._unlock_channel is not None
    assert UNLOCK in client.subscribed, "the subscription must outlive the exchange"


def test_a_secure_exchange_whose_write_hangs_is_a_write_timeout():
    client = _Client()
    _cuff(client)
    session = _secure_session(client)
    original = client.write_gatt_char
    count = {"n": 0}

    async def write(char, data, response=False):
        count["n"] += 1
        if count["n"] > 1:  # the token goes through, the first exchange hangs
            await asyncio.Event().wait()
        await original(char, data, response)

    client.write_gatt_char = write
    with patch.object(secure_flow, "WRITE_TIMEOUT_S", 0.05):
        with pytest.raises(WriteTimeout):
            _establish(session)
    assert session._unlocked is False
    assert session._secure_session is None
