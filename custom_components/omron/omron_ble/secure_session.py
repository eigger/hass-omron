"""OMRON BLE Secure Session protocol.

Implements the application-layer secure session handshake and encrypted
transport used by OMRON BLE healthcare devices over the fe4a GATT service.
"""

from __future__ import annotations

import struct
import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESCCM

# Protocol constants
PACKET_HEADER_SIZE = 13
MAX_COMMAND_SIZE = 88
MAX_DATA_SIZE = 231
PROTOCOL_SALT = bytes.fromhex("6c888391aaf5a53860370bdb5a6083be")


def _require_cryptography():
    """Import cryptography lazily; raise a clear error if unavailable."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ec  # noqa: F401
        from cryptography.hazmat.primitives.ciphers import algorithms  # noqa: F401
        from cryptography.hazmat.primitives import cmac  # noqa: F401
        from cryptography.hazmat.primitives.ciphers.aead import AESCCM  # noqa: F401
        from cryptography.hazmat.primitives import serialization  # noqa: F401
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Secure session module needs the 'cryptography' package (ships with HA)"
        ) from exc


class SecureSession:
    """Manages a single secure session over a BLE connection."""

    # Cryptographic state machine stages
    STATE_IDLE = 0
    STATE_PAIR_REQ_SENT = 1
    STATE_ENC_REQ_SENT = 2
    STATE_CHALLENGE_REQ_SENT = 3
    STATE_PAIRED = 4

    def __init__(self, stored_ltk: bytes | None = None) -> None:
        """Initialize a secure session.

        Args:
            stored_ltk: Previously stored 16-byte LTK (or 80-byte
                        extended token containing the LTK) for reconnection.
                        Pass *None* for initial bonding.
        """
        self.state = self.STATE_IDLE
        self.is_bonding = stored_ltk is None

        # Packet counters
        self.own_packet_counter = 0
        self.last_peer_packet_counter = 0

        # Cryptographic buffers
        self._eph_private = None  # ec.EllipticCurvePrivateKey
        self.own_public_key_bytes = None
        self.own_salt = None  # 7 bytes
        self.own_challenge = None  # 16 bytes

        self.peer_salt = None  # 7 bytes
        self.peer_challenge = None  # 16 bytes
        self.peer_public_key_bytes = None

        self.shared_secret = None
        self.mac_key = None
        
        self.session_key = None

        # Encryption-stage buffers
        self.enc_own_challenge = None  # 16 bytes
        self.enc_own_salt = None  # 28 bytes
        self.enc_peer_salt_nonce = None  # 4 bytes from peer Encryption Response

        # Temp cache for bonding flow step splitting
        self._cached_start_enc_req = None

        # Accept either a raw 16-byte LTK or an 80-byte extended token
        if stored_ltk is not None:
            if len(stored_ltk) == 80:
                self._ltk = stored_ltk[:16]
            elif len(stored_ltk) == 16:
                self._ltk = stored_ltk
            else:
                raise ValueError(f"Stored LTK must be 16 or 80 bytes, got {len(stored_ltk)}")
        else:
            self._ltk = None

    @property
    def ltk(self) -> bytes | None:
        """Return the derived Long-Term Key to persist it."""
        return self._ltk

    @staticmethod
    def _aes_cmac(key: bytes, message: bytes) -> bytes:
        _require_cryptography()
        from cryptography.hazmat.primitives import cmac
        from cryptography.hazmat.primitives.ciphers import algorithms
        c = cmac.CMAC(algorithms.AES(key))
        c.update(message)
        return c.finalize()

    # -- Stage 1: Pairing Request (cmd 0x01) --
    def build_pair_req(self) -> bytes:
        """Build a 89-byte Pairing Request.

        Returns:
            ``0x70 0x01 || salt(7) || challenge(16) || pubkey(64)``
        """
        _require_cryptography()
        from cryptography.hazmat.primitives.asymmetric import ec

        if self.state != self.STATE_IDLE:
            raise RuntimeError("build_pair_req is only valid in IDLE state.")
        if not self.is_bonding:
            raise RuntimeError("Cannot request pairing in reconnect mode.")

        # Generate Ephemeral SECP256R1 Key Pair
        self._eph_private = ec.generate_private_key(ec.SECP256R1())
        public_numbers = self._eph_private.public_key().public_numbers()

        # Encode coordinates in little-endian form for the BLE transport
        x_bytes = public_numbers.x.to_bytes(32, "big")
        y_bytes = public_numbers.y.to_bytes(32, "big")
        self.own_public_key_bytes = x_bytes[::-1] + y_bytes[::-1]

        # Random nonces for key derivation
        self.own_salt = secrets.token_bytes(7)
        self.own_challenge = secrets.token_bytes(16)

        # Frame header (0x70) + command ID (0x01)
        packet = b"\x70\x01" + self.own_salt + self.own_challenge + self.own_public_key_bytes

        self.state = self.STATE_PAIR_REQ_SENT
        return packet

    # -- Stage 2: Pairing Response (cmd 0x81) & key derivation --
    def process_pair_resp(self, resp: bytes) -> None:
        """Process the peer's 89-byte Pairing Response and derive bonding keys."""
        if self.state != self.STATE_PAIR_REQ_SENT:
            raise RuntimeError("process_pair_resp requires STATE_PAIR_REQ_SENT.")
        if len(resp) != 89:
            raise ValueError(f"Invalid pairing response length: {len(resp)}")
        # Peer (device) responses set the top bit of the frame header rather
        # than reusing the request's 0x70 — confirmed via HCI btsnoop
        # (HEM-7188T1-LEO): the device replies with 0xf0 0x81, not 0x70 0x81.
        if resp[:2] != b"\xf0\x81":
            raise ValueError(f"Invalid pairing response header: {resp[:2].hex()}")

        # Parse response fields after the 2-byte header
        # [2:9] salt, [9:25] challenge, [25:89] public key
        self.peer_salt = resp[2:9]
        self.peer_challenge = resp[9:25]
        
        peer_x = resp[25:57][::-1]
        peer_y = resp[57:89][::-1]
        self.peer_public_key_bytes = peer_x + peer_y

        # Pre-compute the encryption start request (driver may split send/receive)
        self._cached_start_enc_req = self._get_start_enc_req_internal()

    def _get_start_enc_req_internal(self) -> bytes:
        """Derive LTK (on bonding) and build a 46-byte Start Encryption Request."""
        _require_cryptography()
        from cryptography.hazmat.primitives.asymmetric import ec

        if self.is_bonding:
            # Reconstruct peer public key
            peer_public_numbers = ec.EllipticCurvePublicNumbers(
                x=int.from_bytes(self.peer_public_key_bytes[:32], "big"),
                y=int.from_bytes(self.peer_public_key_bytes[32:], "big"),
                curve=ec.SECP256R1()
            )
            peer_public_key = peer_public_numbers.public_key()

            # ECDH shared secret derivation (32 bytes)
            self.shared_secret = self._eph_private.exchange(ec.ECDH(), peer_public_key)

            # MacKey = AES-CMAC(protocol_salt, shared_secret)
            self.mac_key = self._aes_cmac(PROTOCOL_SALT, self.shared_secret)

            # LTK = AES-CMAC(MacKey, KDF message block)
            #   0x01 || "btle" || own_challenge || peer_challenge || own_salt || peer_salt || 0x0001
            kdf_msg = (
                b"\x01btle"
                + self.own_challenge
                + self.peer_challenge
                + self.own_salt
                + self.peer_salt
                + b"\x01\x00"
            )
            self._ltk = self._aes_cmac(self.mac_key, kdf_msg)

        # Fresh challenge and random salt for the encryption stage
        self.enc_own_challenge = secrets.token_bytes(16)
        salt1 = secrets.token_bytes(8)
        salt2 = secrets.token_bytes(4)
        self.enc_own_salt = salt1 + salt2 + b"\x00" * 16

        # Frame header (0x70) + command ID (0x05)
        packet = b"\x70\x05" + self.enc_own_challenge + self.enc_own_salt
        
        self.state = self.STATE_ENC_REQ_SENT
        return packet

    # -- Stage 3: Start Encryption Request (cmd 0x05) --
    def build_start_enc_req(self) -> bytes:
        """Return the Start Encryption Request (cached or freshly generated)."""
        if self._cached_start_enc_req is not None:
            val = self._cached_start_enc_req
            self._cached_start_enc_req = None
            return val
        return self._get_start_enc_req_internal()

    # -- Stage 4: Mutual challenge verification (cmd 0x06 / 0x86) --
    def build_challenge_req(self, start_enc_resp: bytes) -> bytes:
        """Process the 46-byte Encryption Response and build a Challenge Request."""
        _require_cryptography()
        from cryptography.hazmat.primitives.ciphers.aead import AESCCM

        if self.state != self.STATE_ENC_REQ_SENT:
            raise RuntimeError("build_challenge_req requires STATE_ENC_REQ_SENT.")
        if len(start_enc_resp) != 46:
            raise ValueError(f"Peer Encryption Response must be 46 bytes, got {len(start_enc_resp)}")
        # Peer responses use header 0xf0, not 0x70 — see process_pair_resp.
        if start_enc_resp[:2] != b"\xf0\x85":
            raise ValueError(f"Invalid Encryption Response headers: {start_enc_resp[:2].hex()}")

        # Extract peer's encryption challenge and salt
        peer_enc_challenge = start_enc_resp[2:18]
        peer_enc_salt = start_enc_resp[18:46]
        # 8-byte device value embedded in every CCM nonce (bytes 5..13).
        self.enc_peer_salt_nonce = peer_enc_salt[8:16]

        # Session key = AES-CMAC(LTK, peer_salt[:8] || own_salt[:8])
        session_kdf_msg = peer_enc_salt[0:8] + self.enc_own_salt[0:8]
        self.session_key = self._aes_cmac(self._ltk, session_kdf_msg)
        
        self.own_packet_counter = 0
        self.last_peer_packet_counter = 0

        # Challenge payload: peer_challenge(16) || padding(16) || random(4)
        challenge_payload = peer_enc_challenge + b"\x00" * 16 + secrets.token_bytes(4)

        # 13-byte CCM nonce: counter(4 LE, =0) || direction(1, 0x80=send) || device(8)
        ccm_nonce = b"\x00\x00\x00\x00" + b"\x80" + self.enc_peer_salt_nonce
        ccm_aad = b"\x00\x00\x00\x00"

        # AES-CCM encrypt with 8-byte tag
        aesccm = AESCCM(self.session_key, tag_length=8)
        ciphertext = aesccm.encrypt(ccm_nonce, challenge_payload, ccm_aad)

        # Frame header (0x70) + command ID (0x06)
        packet = b"\x70\x06" + ciphertext
        
        self.state = self.STATE_CHALLENGE_REQ_SENT
        return packet

    def process_challenge_resp(self, resp: bytes) -> None:
        """Process the peer's 46-byte Challenge Response and finalize the session."""
        _require_cryptography()
        from cryptography.hazmat.primitives.ciphers.aead import AESCCM

        if self.state != self.STATE_CHALLENGE_REQ_SENT:
            raise RuntimeError("process_challenge_resp requires STATE_CHALLENGE_REQ_SENT.")
        if len(resp) != 46:
            raise ValueError(f"Peer Challenge Response must be 46 bytes, got {len(resp)}")
        # Peer responses use header 0xf0, not 0x70 — see process_pair_resp.
        if resp[:2] != b"\xf0\x86":
            raise ValueError(f"Invalid Challenge Response headers: {resp[:2].hex()}")

        ciphertext = resp[2:]
        # 13-byte CCM nonce: counter(4 LE, =0) || direction(1, 0x00=recv) || device(8)
        ccm_nonce = b"\x00\x00\x00\x00" + b"\x00" + self.enc_peer_salt_nonce
        ccm_aad = b"\x00\x00\x00\x00"

        aesccm = AESCCM(self.session_key, tag_length=8)
        try:
            plaintext = aesccm.decrypt(ccm_nonce, ciphertext, ccm_aad)
        except Exception as exc:
            raise ValueError("Challenge decryption failed (tag mismatch).") from exc

        # Mutual authentication: verify the peer echoed our challenge
        if plaintext[:16] != self.enc_own_challenge:
            raise ValueError("Mutual authentication failed: challenge mismatch.")

        self.state = self.STATE_PAIRED

    # -- Encrypted data transport --
    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt a data-plane command packet.

        Returns:
            ``0xC0 || counter(4 LE) || zero(8) || ciphertext || tag(8)`` (13-byte header)
        """
        _require_cryptography()
        from cryptography.hazmat.primitives.ciphers.aead import AESCCM

        if self.state != self.STATE_PAIRED:
            raise RuntimeError("encrypt is only valid in PAIRED state.")
        if len(plaintext) > MAX_DATA_SIZE:
            raise ValueError(f"Plaintext exceeds max size: {len(plaintext)}")

        self.own_packet_counter += 1
        counter_bytes = self.own_packet_counter.to_bytes(4, "little")

        # 13-byte CCM nonce: counter(4 LE) || direction(0x80=send) || device(8)
        nonce = counter_bytes + b"\x80" + self.enc_peer_salt_nonce
        aad = counter_bytes

        aesccm = AESCCM(self.session_key, tag_length=8)
        ciphertext = aesccm.encrypt(nonce, plaintext, aad)

        # 13-byte transport header: 0xC0 || counter(4 LE) || zero(8)
        header = b"\xc0" + counter_bytes + b"\x00" * (PACKET_HEADER_SIZE - 5)
        return header + ciphertext

    def decrypt(self, packet: bytes) -> bytes:
        """Decrypt an incoming data-plane response packet."""
        _require_cryptography()
        from cryptography.hazmat.primitives.ciphers.aead import AESCCM

        if self.state != self.STATE_PAIRED:
            raise RuntimeError("decrypt is only valid in PAIRED state.")
        if len(packet) < PACKET_HEADER_SIZE:
            raise ValueError("Encrypted data packet too short.")
        if packet[0] != 0xc0:
            raise ValueError(f"Invalid packet prefix ID: {packet[0]:02x}")

        # Replay protection: monotonically increasing counter
        peer_counter = int.from_bytes(packet[1:5], "little")
        if peer_counter <= self.last_peer_packet_counter:
            raise ValueError(
                f"Replay Attack Detected! Counter {peer_counter} <= last {self.last_peer_packet_counter}"
            )

        counter_bytes = packet[1:5]
        ciphertext = packet[PACKET_HEADER_SIZE:]

        # 13-byte CCM nonce: counter(4 LE) || direction(0x00=recv) || device(8)
        nonce = counter_bytes + b"\x00" + self.enc_peer_salt_nonce
        aad = counter_bytes

        aesccm = AESCCM(self.session_key, tag_length=8)
        try:
            plaintext = aesccm.decrypt(nonce, ciphertext, aad)
        except Exception as exc:
            raise ValueError("Decryption failed: authentication tag mismatch.") from exc

        self.last_peer_packet_counter = peer_counter
        return plaintext
