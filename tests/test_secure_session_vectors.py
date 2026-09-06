"""Synthetic secure-session wire-format regression tests.

No real credentials, device access, or medical records are used.
These vectors do not establish end-to-end HA or other-model compatibility.
"""
import unittest

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from custom_components.omron.omron_ble.secure_session import SecureSession


class X2SecureVectors(unittest.TestCase):
    def data_session(self):
        session = SecureSession(stored_ltk=bytes(range(16)))
        session.state = session.STATE_PAIRED
        session.session_key = bytes(range(16))
        session.enc_peer_salt_nonce = bytes(range(8))
        return session

    def peer_packet(self, counter=1, payload=b"synthetic-response"):
        count = counter.to_bytes(4, "little")
        sealed = AESCCM(bytes(range(16)), tag_length=8).encrypt(
            count + b"\x00" + bytes(range(8)), payload, count,
        )
        return b"\xc0" + count + sealed

    def test_receive_five_byte_header(self):
        session = self.data_session()
        self.assertEqual(session.decrypt(self.peer_packet()), b"synthetic-response")
        self.assertEqual(session.last_peer_packet_counter, 1)

    def test_invalid_tag_does_not_consume_counter(self):
        session = self.data_session()
        packet = self.peer_packet()
        with self.assertRaises(ValueError):
            session.decrypt(packet[:-1] + bytes([packet[-1] ^ 1]))
        self.assertEqual(session.last_peer_packet_counter, 0)
        self.assertEqual(session.decrypt(packet), b"synthetic-response")

    def test_repeated_response_rejected(self):
        session = self.data_session()
        packet = self.peer_packet()
        session.decrypt(packet)
        with self.assertRaises(ValueError):
            session.decrypt(packet)

    def test_valid_tag_but_wrong_challenge_rejected(self):
        session = self.data_session()
        session.state = session.STATE_CHALLENGE_REQ_SENT
        session.enc_own_challenge = bytes(range(16, 32))
        sealed = AESCCM(session.session_key, tag_length=8).encrypt(
            bytes(5) + bytes(range(8)), bytes(36), bytes(4),
        )
        with self.assertRaisesRegex(ValueError, "challenge mismatch"):
            session.process_challenge_resp(b"\xf0\x86" + sealed)
        self.assertNotEqual(session.state, session.STATE_PAIRED)

    def test_challenge_uses_both_nonce_contributions(self):
        session = SecureSession(stored_ltk=bytes(range(16)))
        session.build_start_enc_req()
        session.enc_own_salt = bytes(range(28))
        peer_salt = bytes(range(32, 60))
        peer_challenge = bytes(range(64, 80))
        frame = session.build_challenge_req(b"\xf0\x85" + peer_challenge + peer_salt)
        nonce = bytes(4) + b"\x80" + peer_salt[8:12] + session.enc_own_salt[8:12]
        plaintext = AESCCM(session.session_key, tag_length=8).decrypt(nonce, frame[2:], bytes(4))
        self.assertEqual(plaintext[:32], peer_challenge + bytes(16))

    def test_transport_has_five_byte_header(self):
        session = SecureSession(stored_ltk=bytes(range(16)))
        session.state = session.STATE_PAIRED
        session.session_key = bytes(range(16))
        session.enc_peer_salt_nonce = bytes(range(8))
        plaintext = b"synthetic-vector"
        counter = b"\x01\x00\x00\x00"
        ciphertext = AESCCM(session.session_key, tag_length=8).encrypt(
            counter + b"\x80" + bytes(range(8)), plaintext, counter,
        )
        self.assertEqual(session.encrypt(plaintext), b"\xc0" + counter + ciphertext)
