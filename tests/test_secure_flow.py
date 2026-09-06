"""Session ordering/persistence gates; crypto wire vectors are tested separately."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.omron.omron_ble.devices import UnlockMode
from custom_components.omron.omron_ble.secure_flow import (
    clock_block,
    establish_secure_session,
    prepare_secure_token,
)


class Crypto:
    def __init__(self, stored_ltk=None):
        self.ltk = stored_ltk or bytes(range(16))

    def build_pair_req(self):
        return b"\x70\x01"

    def process_pair_resp(self, response):
        assert response == b"\xf0\x81"

    def build_start_enc_req(self):
        return b"\x70\x05"

    def build_challenge_req(self, response):
        assert response == b"\xf0\x85"
        return b"\x70\x06"

    def process_challenge_resp(self, response):
        assert response == b"\xf0\x86"

    def encrypt(self, payload):
        assert payload == b"\x20\x01\x00" + bytes(17)
        return b"\xc0"

    def decrypt(self, payload):
        assert payload == b"\xc0"
        return b"\xa0\x01\x00\x01"


def make_session(pairing, close_code=b"\x00"):
    events = []
    session = SimpleNamespace(
        _config=SimpleNamespace(
            model="HEM-7188T1-LEO",
            unlock_mode=UnlockMode.SECURE_SESSION,
            settings_read_address=0x0010,
            settings_write_address=0x0054,
            settings_time_sync_bytes=[0x2C, 0x3C],
            index_pointer_layout={"index_region_byte_size": 0x18},
        ),
        _pairing_session=pairing,
        _token_unlock=AsyncMock(), open_memory_session=AsyncMock(),
        read_memory_block=AsyncMock(side_effect=[bytes(range(44)), bytes(range(24))]),
        write_memory_block=AsyncMock(),
        _last_reply_packet_type=None, _last_reply_payload=None,
    )

    async def write(uuid, packet, response):
        assert response is True
        events.append(packet)
        answer = {b"\x70\x01": b"\xf0\x81", b"\x70\x05": b"\xf0\x85",
                  b"\x70\x06": b"\xf0\x86", b"\xc0": b"\xc0"}[packet]
        session._unlock_notify_handler(None, answer)

    async def close():
        session._last_reply_packet_type = b"\x8f\x00"
        session._last_reply_payload = close_code

    session._client = SimpleNamespace(write_gatt_char=write)
    session.close_memory_session = AsyncMock(side_effect=close)
    return session, events


def run(session, key=None):
    with patch("custom_components.omron.omron_ble.secure_flow.SecureSession", Crypto), patch(
        "custom_components.omron.omron_ble.secure_flow.prepare_secure_token", new_callable=AsyncMock
    ):
        return asyncio.run(establish_secure_session(session, stored_ltk=key, now=datetime(2026, 9, 6, 12, 30, 45)))


def test_resume_never_pairs_or_writes_settings():
    session, events = make_session(False)
    assert run(session, bytes(range(16))) == bytes(range(16))
    assert events == [b"\x70\x05", b"\x70\x06", b"\xc0"]
    session.open_memory_session.assert_not_awaited()
    session.write_memory_block.assert_not_awaited()


def test_pairing_returns_key_only_after_close():
    session, events = make_session(True)
    assert run(session) == bytes(range(16))
    assert events[0] == b"\x70\x01"
    session.close_memory_session.assert_awaited_once()
    calls = session.write_memory_block.await_args_list
    assert calls[0].args == (0x0054, bytearray(range(24)))
    assert calls[1].args[0] == 0x0080


def test_rejected_close_does_not_return_key():
    session, _ = make_session(True, close_code=b"\x01")
    with pytest.raises(ConnectionError, match="close"):
        run(session)
    assert session._unlocked is False


def test_timeout_does_not_return_key():
    session, _ = make_session(True)
    session.close_memory_session = AsyncMock(side_effect=TimeoutError())
    with pytest.raises(TimeoutError):
        run(session)
    assert session._unlocked is False


def test_resume_without_key_is_rejected_before_io():
    session, events = make_session(False)
    with pytest.raises(ValueError, match="resume requires"):
        run(session)
    assert events == []
    session._token_unlock.assert_not_awaited()


def test_request_is_precomputed_before_token_preparation():
    for pairing in (True, False):
        events = []
        class TracedCrypto(Crypto):
            def build_pair_req(self):
                events.append("pair_request")
                return super().build_pair_req()

            def build_start_enc_req(self):
                events.append("start_request")
                return super().build_start_enc_req()

        async def prepared(_session, _timeout):
            events.append("token")

        session, _ = make_session(pairing)
        with patch("custom_components.omron.omron_ble.secure_flow.SecureSession", TracedCrypto), patch(
            "custom_components.omron.omron_ble.secure_flow.prepare_secure_token", prepared
        ):
            asyncio.run(establish_secure_session(session, stored_ltk=None if pairing else bytes(16),
                                     now=datetime(2026, 9, 6)))
        assert events[:2] == ["pair_request" if pairing else "start_request", "token"]


def test_clock_preserves_unrelated_settings():
    source = bytes(range(24))
    block = clock_block(source, datetime(2026, 9, 6, 12, 30, 45), 16)
    assert block[:4] == source[:4]
    assert block[4] == source[4] | 1
    assert block[5:8] == source[5:8]
    assert block[8:14] == bytes([26, 9, 6, 12, 30, 45])
    assert block[14] == sum(block[:14]) % 256
    assert block[15] == source[15]


def test_prepare_reads_then_subscribes_control_rx_async_before_token():
    events, handlers = [], {}
    control = "b305b680-aee7-11e1-a730-0002a5d5c51b"
    async def read(uuid):
        events.append(("read", uuid[:8]))
        return b"synthetic"
    async def subscribe(uuid, handler):
        handlers[uuid.lower()] = handler
        events.append(("notify", uuid.lower()))
    async def write(uuid, value, response):
        assert response is True
        assert len(value) == 20 and value[0] == 0x11
        events.append(("token", uuid.lower()))
        handlers[control](None, b"\x91\x00" + value[1:5])
    session = SimpleNamespace(
        _client=SimpleNamespace(read_gatt_char=read, start_notify=subscribe, write_gatt_char=write),
        _config=SimpleNamespace(rx_channel_uuids=["rx"]),
        _rebuild_notify_handle_index_map=lambda: None,
        _on_notify_channel_data=lambda *_: None,
    )
    asyncio.run(prepare_secure_token(session, 1))
    assert events == [("read", "00002a26"), ("read", "00002a28"),
                      ("notify", control), ("notify", "rx"),
                      ("notify", "8858eb40-aee8-11e1-bb67-0002a5d5c51b"),
                      ("token", control)]
    assert session._notify_subscribed
