"""A refused EEPROM read is an answer, not a truncated frame.

The device replies to a read it will not serve with the header alone and a
result code in byte 6 — the 8-byte shape `0x8f00` and the control frames
already use. Read as truncated it never sets the reply, and the caller
spends its whole retry budget waiting for data that already came back.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.omron.omron_ble.devices import get_device_config
from custom_components.omron.omron_ble.memory_protocol import MemoryReadRefused
from custom_components.omron.omron_ble.session import OmronDeviceSession

ADDRESS = 0x0E34
BLOCKSIZE = 0x10


def _session() -> OmronDeviceSession:
    # The real profile: one RX channel, so a frame arrives whole rather than
    # split across four.
    session = OmronDeviceSession(MagicMock(), get_device_config("HEM-7380T1"))
    session._require_connected = MagicMock()
    return session


def _feed(session: OmronDeviceSession, frame: bytes) -> None:
    """Deliver one frame the way the notify channel would."""
    char = MagicMock()
    char.uuid = session._config.rx_channel_uuids[0]
    session._on_notify_channel_data(char, bytearray(frame))


class TestRefusedRead:
    def test_refusal_frame_sets_the_reply_instead_of_looking_truncated(self):
        session = _session()
        # 08 8100 0e34 10 e3 40 — header, requested length, result code, crc.
        _feed(session, bytes.fromhex("0881000e3410e340"))

        assert session._reply_ready.is_set()
        assert session._last_reply_result_code == 0xE3
        assert session._last_reply_payload == b""

    def test_read_memory_block_raises_with_the_code(self):
        session = _session()

        async def reply(*_args, **_kwargs):
            _feed(session, bytes.fromhex("0881000e3410e340"))

        session.memory._write_command_and_wait_reply = AsyncMock(side_effect=reply)

        with pytest.raises(MemoryReadRefused) as caught:
            asyncio.run(session.read_memory_block(ADDRESS, BLOCKSIZE))

        assert caught.value.code == 0xE3
        assert caught.value.address == ADDRESS
        # One attempt: the device answered, so there is nothing to retry.
        assert session._write_command_and_wait_reply.await_count == 1

    def test_a_short_data_frame_is_still_treated_as_truncated(self):
        # Declared payload present but cut off mid-flight: not an answer, and
        # the retry it triggers is the right response.
        session = _session()
        _feed(session, bytes.fromhex("1881000e3410") + b"\x00" * 6)

        assert not session._reply_ready.is_set()

    def test_a_header_only_frame_with_a_zero_code_is_truncated(self):
        # Same 8-byte shape, but result code 0x00 is not a refusal. With no
        # payload behind it, it is a cut-off frame and must take the retry
        # path rather than hand the caller an empty block as success.
        session = _session()
        _feed(session, bytes.fromhex("0881000e341000a3"))

        assert not session._reply_ready.is_set()
        assert session._last_reply_payload is None

    def test_a_served_read_clears_the_result_code(self):
        session = _session()
        payload = bytes(range(0x10))
        # 6 header bytes + 16 payload + result code + crc = 24 (0x18).
        body = bytes.fromhex("1881000e3410") + payload + b"\x00"
        crc = 0
        for byte in body:
            crc ^= byte
        _feed(session, body + bytes([crc]))

        assert session._reply_ready.is_set()
        assert session._last_reply_result_code == 0
        assert session._last_reply_payload == payload
