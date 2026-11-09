"""OmronDeviceSession notification reassembly and frame validation unit tests."""

from unittest.mock import MagicMock

from custom_components.omron.omron_ble.devices import DeviceConfig
from custom_components.omron.omron_ble.session import OmronDeviceSession


def _calc_crc(frame: bytearray) -> int:
    crc = 0
    for b in frame:
        crc ^= b
    return crc


def _build_valid_frame(
    packet_type: bytes, address: int, payload: bytes, response_code: int = 0x00
) -> bytearray:
    # Frame format: [len(1), type(2), addr(2), datalen(1), payload(N), rescode(1), crc(1)] (8 + N bytes)
    frame = bytearray()
    frame.append(len(payload) + 8)  # total frame length
    frame.extend(packet_type)
    frame.extend(address.to_bytes(2, "big"))
    frame.append(len(payload))
    frame.extend(payload)
    frame.append(response_code)
    frame.append(_calc_crc(frame))
    return frame


class TestRxNotificationHandling:
    def setup_method(self):
        self.config = DeviceConfig(
            model="HEM-7155T",
            rx_channel_uuids=["49123040-aee8-11e1-a74d-0002a5d5c51b"],
            tx_channel_uuids=["db5b55e0-aee7-11e1-965e-0002a5d5c51b"],
        )
        self.session = OmronDeviceSession(MagicMock(), self.config)

    def test_valid_frame_sets_reply_ready_and_payload(self):
        valid_frame = _build_valid_frame(b"\x81\x00", 0x0098, b"\x01\x02\x03\x04")
        self.session.memory._expected_reply_packet_type = b"\x81\x00"
        self.session.memory._on_notify_channel_data(0, valid_frame)

        assert self.session.memory._reply_ready.is_set()
        assert self.session.memory._last_reply_packet_type == b"\x81\x00"
        assert self.session.memory._last_reply_payload == b"\x01\x02\x03\x04"

    def test_session_open_8000_frame_accepted(self):
        # Real-device open_memory_session response: 0880000000100098 (8 bytes, byte[5]=0x10)
        # Must not be rejected as a truncated frame.
        raw = bytearray.fromhex("0880000000100098")
        self.session.memory._expected_reply_packet_type = b"\x80\x00"
        self.session.memory._on_notify_channel_data(0, raw)

        assert self.session.memory._reply_ready.is_set()
        assert self.session.memory._last_reply_packet_type == b"\x80\x00"
        assert self.session.memory._last_reply_payload == b"\x00"

    def test_session_open_8000_result_is_read_after_the_product_index(self):
        # The reply can carry the 16-byte product index; its first byte is not
        # the result code, which sits just before the checksum.
        frame = _build_valid_frame(b"\x80\x00", 0x0000, bytes([0x7F] * 16), 0x00)
        self.session.memory._expected_reply_packet_type = b"\x80\x00"
        self.session.memory._on_notify_channel_data(0, frame)

        assert self.session.memory._reply_ready.is_set()
        assert self.session.memory._last_reply_payload == b"\x00"

    def test_session_open_8000_rejection_code_is_read_from_the_end(self):
        frame = _build_valid_frame(b"\x80\x00", 0x0000, bytes(16), 0x02)
        self.session.memory._expected_reply_packet_type = b"\x80\x00"
        self.session.memory._on_notify_channel_data(0, frame)

        assert self.session.memory._last_reply_payload == b"\x02"

    def test_session_close_8f00_frame_accepted_with_nonzero_byte5(self):
        # Control frame 8f00 with non-zero byte[5] and response code 0
        raw = bytearray([0x08, 0x8F, 0x00, 0x00, 0x00, 0x10, 0x00])
        raw.append(_calc_crc(raw))
        self.session.memory._expected_reply_packet_type = b"\x8f\x00"
        self.session.memory._on_notify_channel_data(0, raw)

        assert self.session.memory._reply_ready.is_set()
        assert self.session.memory._last_reply_packet_type == b"\x8f\x00"
        assert self.session.memory._last_reply_payload == b"\x00"

    def test_memory_write_81c0_frame_accepted_with_nonzero_byte5(self):
        # Control frame 81c0 (write response) with non-zero byte[5] and response code 0
        raw = bytearray([0x08, 0x81, 0xC0, 0x00, 0x00, 0x0A, 0x00])
        raw.append(_calc_crc(raw))
        self.session.memory._expected_reply_packet_type = b"\x81\xc0"
        self.session.memory._on_notify_channel_data(0, raw)

        assert self.session.memory._reply_ready.is_set()
        assert self.session.memory._last_reply_packet_type == b"\x81\xc0"
        assert self.session.memory._last_reply_payload == b"\x00"

    def test_single_channel_declared_length_truncation_is_ignored(self):
        # Single channel frame declares 16 bytes, but only 8 bytes received
        short_frame = bytearray([16, 0x81, 0x00, 0x00, 0x00, 0x08, 0x00, 0x00])
        self.session.memory._on_notify_channel_data(0, short_frame)

        assert not self.session.memory._reply_ready.is_set()
        assert self.session.memory._last_reply_payload is None

    def test_undersized_frame_is_ignored(self):
        short_frame = bytearray([0x04, 0x81, 0x00, 0x05])
        self.session.memory._on_notify_channel_data(0, short_frame)

        assert not self.session.memory._reply_ready.is_set()
        assert self.session.memory._last_reply_payload is None

    def test_truncated_frame_is_ignored_without_synthetic_ff(self):
        # Frame claims 16 bytes payload, but only 4 bytes are actually present
        frame = bytearray(
            [24, 0x81, 0x00, 0x00, 0x98, 16, 0xAA, 0xBB, 0xCC, 0xDD, 0x00]
        )
        frame.append(_calc_crc(frame))
        self.session.memory._expected_reply_packet_type = b"\x81\x00"
        self.session.memory._on_notify_channel_data(0, frame)

        # Must not set reply_ready and must not produce fake 0xFF bytes
        assert not self.session.memory._reply_ready.is_set()
        assert self.session.memory._last_reply_payload is None

    def test_unexpected_reply_packet_type_is_ignored(self):
        valid_frame = _build_valid_frame(b"\x80\x00", 0x0000, b"\x00" * 4)
        # Expected is read reply 8100, but incoming is late session open reply 8000
        self.session.memory._expected_reply_packet_type = b"\x81\x00"
        self.session.memory._on_notify_channel_data(0, valid_frame)

        assert not self.session.memory._reply_ready.is_set()

    def test_error_frame_8f00_is_always_accepted(self):
        # Device reports error frame 8f00 with error code 3 in payload/byte 6
        error_frame = _build_valid_frame(b"\x8f\x00", 0x0000, b"\x03")
        # Even when waiting for 8100, 8f00 error frame must be accepted so caller gets error code
        self.session.memory._expected_reply_packet_type = b"\x81\x00"
        self.session.memory._on_notify_channel_data(0, error_frame)

        assert self.session.memory._reply_ready.is_set()
        assert self.session.memory._last_reply_packet_type == b"\x8f\x00"
        assert self.session.memory._last_reply_payload == b"\x03"

    def test_oversized_packet_size_is_rejected(self):
        multi_config = DeviceConfig(
            model="HEM-7322T",
            rx_channel_uuids=[
                "51220002-0000-1000-8000-00805f9b34fb",
                "51220003-0000-1000-8000-00805f9b34fb",
                "51220004-0000-1000-8000-00805f9b34fb",
                "51220005-0000-1000-8000-00805f9b34fb",
            ],
            tx_channel_uuids=[
                "51220001-0000-1000-8000-00805f9b34fb",
                "51220006-0000-1000-8000-00805f9b34fb",
            ],
        )
        session = OmronDeviceSession(MagicMock(), multi_config)
        session.memory._notify_handle_to_channel = {10: 0}

        # packet_size = 70 (exceeds 64 byte 4-channel max)
        session.memory._on_notify_channel_data(10, bytearray([70] + [0] * 15))
        assert not session.memory._reply_ready.is_set()
        assert session.memory._channel_fragments[0] is None

    def test_channel_zero_clears_stale_fragments_on_multi_channel(self):
        multi_config = DeviceConfig(
            model="HEM-7322T",
            rx_channel_uuids=[
                "51220002-0000-1000-8000-00805f9b34fb",
                "51220003-0000-1000-8000-00805f9b34fb",
                "51220004-0000-1000-8000-00805f9b34fb",
                "51220005-0000-1000-8000-00805f9b34fb",
            ],
            tx_channel_uuids=[
                "51220001-0000-1000-8000-00805f9b34fb",
                "51220006-0000-1000-8000-00805f9b34fb",
            ],
        )
        session = OmronDeviceSession(MagicMock(), multi_config)
        session.memory._notify_handle_to_channel = {10: 0, 11: 1, 12: 2, 13: 3}

        # Put a stale fragment on channel 1
        session.memory._channel_fragments[1] = bytearray(b"\xde\xad\xbe\xef" * 4)

        # Receiving new start on channel 0 resets fragments
        session.memory._on_notify_channel_data(
            10,
            bytearray([24, 0x81, 0x00, 0x00, 0x98, 16, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]),
        )

        # Channel 1 should have been cleared when channel 0 arrived
        assert session.memory._channel_fragments[1] is None
        assert not session.memory._reply_ready.is_set()

    def test_write_command_raises_connection_error_on_8f00_device_rejection(self):
        import asyncio
        import pytest

        client = MagicMock()

        async def fake_write(uuid, data, response=True):
            # Simulate device responding with 8f00 error code 3 immediately
            error_frame = _build_valid_frame(b"\x8f\x00", 0x0000, b"\x03")
            self.session.memory._on_notify_channel_data(0, error_frame)

        client.write_gatt_char.side_effect = fake_write
        self.session._client = client

        cmd = bytearray.fromhex("0801000098100081")

        with pytest.raises(
            ConnectionError, match="Device rejected command 080100 .*code 0x03"
        ):
            asyncio.run(self.session.memory._write_command_and_wait_reply(cmd))

    def test_failed_session_sends_error_stop_command(self):
        import asyncio

        client = MagicMock()
        writes = []

        async def fake_write(uuid, data, response=True):
            writes.append(bytes(data).hex())
            # Reply 8f00 to close command
            close_reply = bytearray([0x08, 0x8F, 0x00, 0x00, 0x00, 0x00, 0x00])
            close_reply.append(_calc_crc(close_reply))
            self.session.memory._on_notify_channel_data(0, close_reply)

        client.write_gatt_char.side_effect = fake_write
        client.is_connected = True
        self.session._client = client
        self.session.memory._memory_session_active = True

        asyncio.run(self.session.memory.close_memory_session(error=True))
        assert writes == ["080fffffff0000f8"]
        assert not self.session.memory.memory_session_active

    def test_handled_read_refusal_does_not_fail_session_close(self):
        import asyncio
        from custom_components.omron.omron_ble.memory_protocol import MemoryReadRefused

        client = MagicMock()
        writes = []

        async def fake_write(uuid, data, response=True):
            writes.append(bytes(data).hex())
            # Reply with refusal code 0x02 for read, and 8f00 for close
            if data[1] == 0x01:
                # 8 bytes refusal frame: byte[5]=0x10, byte[6]=0x02
                refusal = bytearray([0x08, 0x81, 0x00, 0x00, 0x98, 0x10, 0x02])
                refusal.append(_calc_crc(refusal))
                self.session.memory._on_notify_channel_data(0, refusal)
            elif data[1] == 0x0F:
                close_reply = bytearray([0x08, 0x8F, 0x00, 0x00, 0x00, 0x00, 0x00])
                close_reply.append(_calc_crc(close_reply))
                self.session.memory._on_notify_channel_data(0, close_reply)

        client.write_gatt_char.side_effect = fake_write
        client.is_connected = True
        self.session._client = client
        self.session.memory._memory_session_active = True

        async def run_read_and_close():
            try:
                await self.session.memory.read_memory_block(0x0098, 0x10)
            except MemoryReadRefused:
                pass
            await self.session.memory.close_memory_session()

        asyncio.run(run_read_and_close())
        assert writes[-1] == "080f000000000007"
        assert not self.session.memory.memory_session_active

    def test_memory_session_context_manager_marks_failure_on_exception(self):
        import asyncio
        from unittest.mock import AsyncMock
        import pytest

        client = MagicMock()
        client.start_notify = AsyncMock()
        client.stop_notify = AsyncMock()
        writes = []

        async def fake_write(uuid, data, response=True):
            writes.append(bytes(data).hex())
            if data[1] == 0x00:
                # Reply to open
                open_reply = bytearray.fromhex("0880000000100098")
                self.session.memory._on_notify_channel_data(0, open_reply)
            elif data[1] == 0x0F:
                close_reply = bytearray([0x08, 0x8F, 0x00, 0x00, 0x00, 0x00, 0x00])
                close_reply.append(_calc_crc(close_reply))
                self.session.memory._on_notify_channel_data(0, close_reply)

        client.write_gatt_char.side_effect = fake_write
        client.is_connected = True
        self.session._client = client

        async def run_context():
            async with self.session.memory.memory_session():
                raise RuntimeError("Simulated failure inside session")

        with pytest.raises(RuntimeError):
            asyncio.run(run_context())

        assert writes[-1] == "080fffffff0000f8"
        assert not self.session.memory.memory_session_active

    def test_unopened_session_does_not_send_stop_command(self):
        import asyncio

        client = MagicMock()
        writes = []

        async def fake_write(uuid, data, response=True):
            writes.append(bytes(data).hex())

        client.write_gatt_char.side_effect = fake_write
        self.session._client = client
        self.session.memory._memory_session_active = False

        asyncio.run(self.session.memory.close_memory_session())
        assert writes == []

    def test_missed_notification_recovered_by_direct_read(self, monkeypatch):
        import asyncio

        multi_config = DeviceConfig(
            model="HEM-7322T",
            rx_channel_uuids=[
                "51220002-0000-1000-8000-00805f9b34fb",
                "51220003-0000-1000-8000-00805f9b34fb",
                "51220004-0000-1000-8000-00805f9b34fb",
                "51220005-0000-1000-8000-00805f9b34fb",
            ],
            tx_channel_uuids=[
                "51220001-0000-1000-8000-00805f9b34fb",
                "51220006-0000-1000-8000-00805f9b34fb",
            ],
        )
        session = OmronDeviceSession(MagicMock(), multi_config)
        session._debug_ble_link = lambda *_a: None
        client = MagicMock()
        writes = []

        full_frame = _build_valid_frame(b"\x81\x00", 0x0010, bytes(range(16)))
        # Total frame is 24 bytes -> 16 bytes on ch0, 8 bytes on ch1
        ch0_data = full_frame[:16]
        ch1_data = full_frame[16:]

        async def fake_write(uuid, data, response=True):
            writes.append(bytes(data).hex())
            # Deliver only channel 0 notification; channel 1 notification is missed!
            session.memory._on_notify_channel_data(
                multi_config.rx_channel_uuids[0], ch0_data
            )

        async def fake_read(uuid):
            if uuid == multi_config.rx_channel_uuids[1]:
                return bytes(ch1_data)
            return b""

        client.write_gatt_char.side_effect = fake_write
        client.read_gatt_char.side_effect = fake_read
        client.is_connected = True
        session._client = client

        # Short timeout to trigger recovery quickly
        monkeypatch.setattr(
            "custom_components.omron.omron_ble.memory_protocol._MEMORY_PROTOCOL_REPLY_TIMEOUT_SEC",
            0.05,
        )

        cmd = bytearray.fromhex("0801000010100009")
        session.memory._expected_reply_packet_type = b"\x81\x00"
        asyncio.run(session.memory._write_command_and_wait_reply(cmd, timeout=0.05))

        # Command was written only once! Recovery read ch1 directly
        assert len(writes) == 1
        assert session.memory._reply_ready.is_set()
        assert session.memory._last_reply_payload == bytes(range(16))
