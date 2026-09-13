"""Application-layer secure session: establish a credential, then resume it.

This is the whole ``UnlockMode.SECURE_SESSION`` path. A device on it keeps its
own credential independent of the BLE bond: the first session authenticates,
initializes the device and keeps the credential the device leaves behind, and
every later session replays that credential and writes nothing.

Contributed for HEM-7188T1-LEO by the #24 reporter and hardware-verified there
on Linux/BlueZ and macOS. Nothing here is written for that model specifically:
every address comes out of the catalog fields the profile already carries.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import secrets

from .devices import UnlockMode
from .omron_driver import (
    _SECURE_HANDSHAKE_WAIT_TIMEOUT_SEC,
    UNLOCK_CHARACTERISTIC_UUID,
    _secure_error_frame_code,
)
from .secure_session import SecureSession
from .settings_mirror import SettingsMirrorLayout, clock_block

# The secure flow's initialization is the same mirror write; kept under its
# old name so callers and tests importing it from here keep working.
SecureInitLayout = SettingsMirrorLayout

_LOGGER = logging.getLogger(__name__)

# Vendor characteristic that carries unsolicited device notices. Subscribed
# alongside the control and data channels because the working reference does.
ASYNC_NOTICE_UUID = "8858eb40-aee8-11e1-bb67-0002a5d5c51b"
# Device Information reads the reference performs before the ECDH exchange.
_PRE_HANDSHAKE_DIS_UUIDS = (
    "00002a26-0000-1000-8000-00805f9b34fb",  # Firmware Revision String
    "00002a28-0000-1000-8000-00805f9b34fb",  # Software Revision String
)
# System authentication, sent encrypted once the session key exists.
_SYSTEM_AUTH_REQUEST = b"\x20\x01\x00" + bytes(17)


async def prepare_secure_token(session, timeout: float) -> None:
    """Run the pre-handshake reads, subscriptions and 0x11/0x91 token exchange.

    The token step has to reach the device on the same subscriptions the ECDH
    exchange then uses: re-adding the unlock CCCD in between makes the device
    reject the pairing request.
    """
    for uuid in _PRE_HANDSHAKE_DIS_UUIDS:
        await session._client.read_gatt_char(uuid)
        _LOGGER.debug("Secure session: device information read %s", uuid[:8])
    token = secrets.token_bytes(4)
    accepted = asyncio.Event()

    def token_reply(_char, data):
        if len(data) >= 6 and data[:2] == b"\x91\x00" and data[2:6] == token:
            accepted.set()

    def control_dispatch(char, data):
        handler = session._unlock_notify_handler
        if handler is not None:
            handler(char, data)

    def async_notice(_char, data):
        _LOGGER.debug("Secure session: async notification bytes=%d", len(data))

    session._unlock_notify_handler = token_reply
    # Through the recovery path, not raw start_notify: on a profile that keeps
    # its subscriptions, BlueZ can still hold the CCCD from the previous
    # connection, and a retry re-enters here with them enabled (#92).
    await session._ensure_services_cache()
    await session._start_notify_with_recovery(
        UNLOCK_CHARACTERISTIC_UUID, control_dispatch
    )
    session._rebuild_notify_handle_index_map()
    await session._start_notify_with_recovery(
        session._config.rx_channel_uuids[0], session._on_notify_channel_data
    )
    session._notify_subscribed = True
    await session._start_notify_with_recovery(ASYNC_NOTICE_UUID, async_notice)
    _LOGGER.debug("Secure session: subscribed control, rx and async channels")
    await asyncio.sleep(0.75)
    await session._client.write_gatt_char(
        UNLOCK_CHARACTERISTIC_UUID, b"\x11" + token + bytes(15), response=True
    )
    await asyncio.wait_for(accepted.wait(), timeout)
    _LOGGER.debug("Secure session: token accepted")


async def establish_secure_session(
    session,
    *,
    stored_ltk: bytes | None,
    now: datetime,
    timeout: float = _SECURE_HANDSHAKE_WAIT_TIMEOUT_SEC,
) -> bytes:
    """Authenticate and, for a pairing session, initialize the device.

    Returns the credential to store, bound to this device and adapter. Any
    exception means the caller must close the connection and keep whatever
    credential it already had: a credential from a half-finished initialization
    does not authenticate later.
    """
    if session._config.unlock_mode != UnlockMode.SECURE_SESSION:
        raise ValueError("The secure session path requires unlock_mode=SECURE_SESSION")
    pairing = session._pairing_session
    if pairing != (stored_ltk is None):
        raise ValueError(
            "Explicit pairing requires a new key; resume requires a stored key"
        )
    if stored_ltk is not None and len(stored_ltk) != 16:
        raise ValueError("A stored secure-session credential must be 16 bytes")
    layout = SecureInitLayout(session._config) if pairing else None

    crypto = SecureSession(stored_ltk=stored_ltk)
    # Key generation and lazy crypto imports must not delay token -> request.
    first_request = crypto.build_pair_req() if pairing else crypto.build_start_enc_req()
    replies: asyncio.Queue = asyncio.Queue()

    def receive(_char, data):
        replies.put_nowait(bytes(data))

    async def exchange(packet: bytes, prefix: bytes) -> bytes:
        _LOGGER.debug(
            "Secure session TX stage=%s bytes=%d", prefix.hex(), len(packet)
        )
        await session._client.write_gatt_char(
            UNLOCK_CHARACTERISTIC_UUID, packet, response=True
        )
        response = await asyncio.wait_for(replies.get(), timeout)
        if not response.startswith(prefix):
            error = _secure_error_frame_code(response)
            if error is not None:
                # A refusal, not a malformed reply: the request itself is
                # well-formed, so this is device state. Say what to do about it.
                raise ConnectionError(
                    f"Device rejected the secure session (error frame "
                    f"0x{response[0]:02x}, code 0x{error:02x}) at stage "
                    f"{prefix.hex()}; the cuff may already be registered to "
                    f"another host, or is not in pairing mode. Put it in "
                    f"pairing mode, or fully unpair/factory-reset it, and "
                    f"try again."
                )
            # Only a protocol discriminator, never the challenge or key payload.
            raise ConnectionError(
                f"Unexpected secure-session response: expected={prefix.hex()} "
                f"received_prefix={response[:2].hex()} bytes={len(response)}"
            )
        return response

    try:
        await prepare_secure_token(session, timeout)
        session._unlocked = False
        session._secure_session = crypto
        session._unlock_notify_handler = receive
        if pairing:
            response = await exchange(first_request, b"\xf0\x81")
            crypto.process_pair_resp(response)
        start_request = crypto.build_start_enc_req() if pairing else first_request
        response = await exchange(start_request, b"\xf0\x85")
        response = await exchange(crypto.build_challenge_req(response), b"\xf0\x86")
        crypto.process_challenge_resp(response)
        request = crypto.encrypt(_SYSTEM_AUTH_REQUEST)
        response = crypto.decrypt(await exchange(request, b"\xc0"))
        if len(response) != 4 or response[0] != 0xA0 or response[2] != 0:
            raise ConnectionError("Secure session system authentication rejected")
        session._unlocked = True

        if layout is not None:
            await session.open_memory_session()
            head = await session.read_memory_block(
                layout.head_read_address, layout.head_read_size
            )
            tail = await session.read_memory_block(
                layout.clock_read_address, layout.clock_read_size
            )
            if len(head) != layout.head_read_size or len(tail) != layout.clock_read_size:
                raise ConnectionError("Incomplete secure initialization settings")
            block = clock_block(tail, now, layout.clock_write_size)
            await session.write_memory_block(
                layout.head_write_address,
                bytearray(head[: layout.head_write_size]),
            )
            await session.write_memory_block(
                layout.clock_write_address, bytearray(block)
            )
            await session.close_memory_session()
            # close_memory_session only warns on a rejection. Never commit a
            # credential on one: the device did not accept the initialization.
            if (
                session._last_reply_packet_type != b"\x8f\x00"
                or session._last_reply_payload != b"\x00"
            ):
                raise ConnectionError("Secure initialization close was not accepted")
        if crypto.ltk is None or len(crypto.ltk) != 16:
            raise ConnectionError("Missing authenticated secure-session credential")
        return crypto.ltk
    except BaseException:
        session._unlocked = False
        raise
    finally:
        session._unlock_notify_handler = None
