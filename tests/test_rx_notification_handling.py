"""OmronDeviceSession notification reassembly and frame validation unit tests."""
from unittest.mock import MagicMock

from custom_components.omron.omron_ble.devices import DeviceConfig
from custom_components.omron.omron_ble.omron_driver import OmronDeviceSession


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
        self.session._expected_reply_packet_type = b"\x81\x00"
        self.session._on_notify_channel_data(0, valid_frame)

        assert self.session._reply_ready.is_set()
        assert self.session._last_reply_packet_type == b"\x81\x00"
        assert self.session._last_reply_payload == b"\x01\x02\x03\x04"

    def test_undersized_frame_is_ignored(self):
        short_frame = bytearray([0x04, 0x81, 0x00, 0x05])
        self.session._on_notify_channel_data(0, short_frame)

        assert not self.session._reply_ready.is_set()
        assert self.session._last_reply_payload is None

    def test_truncated_frame_is_ignored_without_synthetic_ff(self):
        # Frame claims 16 bytes payload, but only 4 bytes are actually present
        frame = bytearray([24, 0x81, 0x00, 0x00, 0x98, 16, 0xAA, 0xBB, 0xCC, 0xDD, 0x00])
        frame.append(_calc_crc(frame))
        self.session._expected_reply_packet_type = b"\x81\x00"
        self.session._on_notify_channel_data(0, frame)

        # Must not set reply_ready and must not produce fake 0xFF bytes
        assert not self.session._reply_ready.is_set()
        assert self.session._last_reply_payload is None

    def test_unexpected_reply_packet_type_is_ignored(self):
        valid_frame = _build_valid_frame(b"\x80\x00", 0x0000, b"\x00" * 4)
        # Expected is read reply 8100, but incoming is late session open reply 8000
        self.session._expected_reply_packet_type = b"\x81\x00"
        self.session._on_notify_channel_data(0, valid_frame)

        assert not self.session._reply_ready.is_set()

    def test_error_frame_8f00_is_always_accepted(self):
        # Device reports error frame 8f00 with error code 3 in payload/byte 6
        error_frame = _build_valid_frame(b"\x8f\x00", 0x0000, b"\x03")
        # Even when waiting for 8100, 8f00 error frame must be accepted so caller gets error code
        self.session._expected_reply_packet_type = b"\x81\x00"
        self.session._on_notify_channel_data(0, error_frame)

        assert self.session._reply_ready.is_set()
        assert self.session._last_reply_packet_type == b"\x8f\x00"
        assert self.session._last_reply_payload == b"\x03"

    def test_oversized_packet_size_is_rejected(self):
        multi_config = DeviceConfig(
            model="HEM-7322T",
            rx_channel_uuids=[
                "51220002-0000-1000-8000-00805f9b34fb",
                "51220003-0000-1000-8000-00805f9b34fb",
                "51220004-0000-1000-8000-00805f9b34fb",
                "51220005-0000-1000-8000-00805f9b34fb",
            ],
            tx_channel_uuids=["51220001-0000-1000-8000-00805f9b34fb"],
        )
        session = OmronDeviceSession(MagicMock(), multi_config)
        session._notify_handle_to_channel = {10: 0}

        # packet_size = 70 (exceeds 64 byte 4-channel max)
        session._on_notify_channel_data(10, bytearray([70] + [0] * 15))
        assert not session._reply_ready.is_set()
        assert session._channel_fragments[0] is None

    def test_channel_zero_clears_stale_fragments_on_multi_channel(self):
        multi_config = DeviceConfig(
            model="HEM-7322T",
            rx_channel_uuids=[
                "51220002-0000-1000-8000-00805f9b34fb",
                "51220003-0000-1000-8000-00805f9b34fb",
                "51220004-0000-1000-8000-00805f9b34fb",
                "51220005-0000-1000-8000-00805f9b34fb",
            ],
            tx_channel_uuids=["51220001-0000-1000-8000-00805f9b34fb"],
        )
        session = OmronDeviceSession(MagicMock(), multi_config)
        session._notify_handle_to_channel = {10: 0, 11: 1, 12: 2, 13: 3}

        # Put a stale fragment on channel 1
        session._channel_fragments[1] = bytearray(b"\xde\xad\xbe\xef" * 4)

        # Receiving new start on channel 0 resets fragments
        session._on_notify_channel_data(10, bytearray([24, 0x81, 0x00, 0x00, 0x98, 16, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]))

        # Channel 1 should have been cleared when channel 0 arrived
        assert session._channel_fragments[1] is None
        assert not session._reply_ready.is_set()

    def test_write_command_raises_connection_error_on_8f00_device_rejection(self):
        import asyncio
        import pytest

        client = MagicMock()

        async def fake_write(uuid, data, response=True):
            # Simulate device responding with 8f00 error code 3 immediately
            error_frame = _build_valid_frame(b"\x8f\x00", 0x0000, b"\x03")
            self.session._on_notify_channel_data(0, error_frame)

        client.write_gatt_char.side_effect = fake_write
        self.session._client = client

        cmd = bytearray.fromhex("0801000098100081")

        with pytest.raises(ConnectionError, match="Device rejected command 080100 .*code 0x03"):
            asyncio.run(self.session._write_command_and_wait_reply(cmd))
