"""_pair_custom_key end to end over a fake link: the key-programming loop and the key ack."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from blesession.testing import FakeClient

from custom_components.omron.omron_ble import unlock as unlock_module
from custom_components.omron.omron_ble.const import UNLOCK_CHARACTERISTIC_UUID as UNLOCK
from custom_components.omron.omron_ble.unlock import _pair_custom_key

RX = "test-rx"
KEY = bytearray(range(16))


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    for name, value in (
        ("_PAIRING_SETTLE_DEFAULT_SEC", 0.1),
        ("_PAIRING_PROG_WAIT_TIMEOUT_SEC", 0.05),
        ("_PAIRING_KEY_ACK_WAIT_TIMEOUT_SEC", 0.05),
        ("WRITE_TIMEOUT_S", 0.05),
    ):
        monkeypatch.setattr(unlock_module, name, value)


def _session(client):
    return SimpleNamespace(
        _client=client,
        _config=SimpleNamespace(
            rx_channel_uuids=[RX], model="HEM-test", aggressive_gatt_timing=False
        ),
    )


def _cuff(client, *, ready_after=0, key_ack=b"\x80"):
    """Answers the Nth 0x02 request with 0x82 (programming mode) and the key write with ``key_ack``."""
    seen = {"requests": 0}

    def on_write(_uuid, data):
        if data[0] == 0x02:
            seen["requests"] += 1
            if seen["requests"] > ready_after:
                client.reply(b"\x82\x00", UNLOCK)
        elif data[0] == 0x00 and key_ack is not None:
            client.reply(key_ack, UNLOCK)

    client.on_write = on_write
    return seen


def _commands(client):
    return [data[0] for _c, data, _r in client.writes]


@pytest.mark.asyncio
async def test_pairing_programs_the_key_and_releases_both_subscriptions():
    client = FakeClient()
    _cuff(client)
    await _pair_custom_key(_session(client), KEY)
    assert _commands(client) == [0x02, 0x00]
    assert client.subscribed == {}


@pytest.mark.asyncio
async def test_a_programming_mode_frame_that_arrives_late_is_not_asked_for_again():
    """The cuff answers during the pause between two attempts: that answer counts."""
    client = FakeClient()
    _cuff(client, ready_after=99)  # never answers a 0x02 itself
    loop = asyncio.get_running_loop()
    # 0.1 s initial settle, then attempt 1 writes and waits 0.05 s, then pauses
    # 0.1 s (until ~0.25): land inside that pause
    loop.call_later(0.2, client.reply, b"\x82\x00", UNLOCK)

    def key_ack(_uuid, data):
        if data[0] == 0x00:
            client.reply(b"\x80", UNLOCK)

    client.on_write = key_ack
    await _pair_custom_key(_session(client), KEY)
    assert _commands(client) == [0x02, 0x00], "the 0x02 was sent again"


@pytest.mark.asyncio
async def test_a_cuff_that_never_enters_programming_mode_is_reported_and_released():
    client = FakeClient()
    _cuff(client, ready_after=99)
    with pytest.raises(ConnectionError, match="key programming mode"):
        await _pair_custom_key(_session(client), KEY)
    assert _commands(client) == [0x02] * 5
    assert client.subscribed == {}


@pytest.mark.asyncio
async def test_a_key_that_is_not_acknowledged_is_reported_and_released():
    client = FakeClient()
    _cuff(client, key_ack=None)
    with pytest.raises(ConnectionError, match="Failed to program pairing key"):
        await _pair_custom_key(_session(client), KEY)
    assert client.subscribed == {}


@pytest.mark.asyncio
async def test_the_last_frame_after_the_key_write_is_the_answer():
    client = FakeClient()

    def on_write(_uuid, data):
        if data[0] == 0x02:
            client.reply(b"\x82\x00", UNLOCK)
        else:
            client.reply(b"\x82\x00", UNLOCK)  # a state frame first ...
            client.reply(b"\x80", UNLOCK)  # ... then the verdict

    client.on_write = on_write
    await _pair_custom_key(_session(client), KEY)
