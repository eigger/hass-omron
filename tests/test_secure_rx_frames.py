"""Exercise the actual driver callback with synthetic encrypted secure-session replies."""
from unittest.mock import MagicMock

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from custom_components.omron.omron_ble.devices import DeviceConfig, HostPairingMode, UnlockMode
from custom_components.omron.omron_ble.omron_driver import OmronDeviceSession
from custom_components.omron.omron_ble.secure_session import SecureSession


def make_session():
    config = DeviceConfig(
        model="HEM-7188T1-LEO",
        unlock_mode=UnlockMode.SECURE_SESSION,
        host_pairing_mode=HostPairingMode.OS_BONDING,
        rx_channel_uuids=["49123040-aee8-11e1-a74d-0002a5d5c51b"],
        tx_channel_uuids=["db5b55e0-aee7-11e1-965e-0002a5d5c51b"],
    )
    session = OmronDeviceSession(MagicMock(), config)
    crypto = SecureSession(stored_ltk=bytes(16))
    crypto.state = crypto.STATE_PAIRED
    crypto.session_key = bytes(range(16))
    crypto.enc_peer_salt_nonce = bytes(range(8))
    session._secure_session = crypto
    session._expected_reply_packet_type = b"\x80\x00"
    return session


def deliver(session, plaintext):
    counter = b"\x01\x00\x00\x00"
    encrypted = AESCCM(bytes(range(16)), tag_length=8).encrypt(
        counter + b"\x00" + bytes(range(8)), plaintext, counter,
    )
    session._on_notify_channel_data(0, bytearray(b"\xc0" + counter + encrypted))


def test_encrypted_open_reply_is_not_treated_as_192_byte_frame():
    session = make_session()
    deliver(session, bytes.fromhex("0880000000100098"))
    assert session._reply_ready.is_set()
    assert session._last_reply_payload == b"\x00"


def test_authenticated_but_invalid_inner_checksum_is_rejected():
    session = make_session()
    deliver(session, bytes.fromhex("0880000000100099"))
    assert not session._reply_ready.is_set()


def test_authenticated_but_truncated_inner_frame_is_rejected():
    session = make_session()
    deliver(session, bytes.fromhex("08800000"))
    assert not session._reply_ready.is_set()
