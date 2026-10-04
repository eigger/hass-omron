"""Unlock and custom-key programming for one connected session.

``OmronDeviceSession.unlock`` and ``pair`` stay the entry points. This module
holds the ack checks and the handshakes those entries dispatch to.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import contextlib
import logging
import secrets

from blesession import (
    WRITE_TIMEOUT_S,
    NotificationTimeout,
    Notifications,
    WriteTimeout,
    guarded_write,
    start_notify_with_recovery,
    stop_notify_best_effort,
)

from .connection import (
    _NOTIFY_SUBSCRIBE_SETTLE_SEC,
    _bleak_refresh_services,
)
from .const import UNLOCK_CHARACTERISTIC_UUID
from .secure_flow import establish_secure_session
from .util import _hex

_LOGGER = logging.getLogger(__name__)

_OS_BOND_RETRY_DELAY_SEC: float = 0.5
_UNLOCK_PROBE_WAIT_TIMEOUT_SEC: float = 2.0
_UNLOCK_AUTH_WAIT_TIMEOUT_SEC: float = 5.0
_PAIRING_SETTLE_AGGRESSIVE_SEC: float = 0.25
_PAIRING_SETTLE_DEFAULT_SEC: float = 1.0
_PAIRING_PROG_WAIT_TIMEOUT_SEC: float = 2.0
_PAIRING_KEY_ACK_WAIT_TIMEOUT_SEC: float = 5.0
_PAIR_UNLOCK_ATTEMPTS_AGGRESSIVE: int = 10
_PAIR_UNLOCK_ATTEMPTS_DEFAULT: int = 5


def _is_unlock_key_programming_ready(resp: bytes | bytearray | None) -> bool:
    """Unlock notify: key programming mode ready (prefix 0x82; sub-type is in byte 1, not matched)."""
    if resp is None or len(resp) < 1:
        return False
    return resp[0] == 0x82


def _is_unlock_pairing_key_ack(resp: bytes | bytearray | None) -> bool:
    """Unlock notify: new pairing key accepted (prefix 0x80; sub-type in byte 1, not matched)."""
    if resp is None or len(resp) < 1:
        return False
    return resp[0] == 0x80


def _is_unlock_auth_key_ack(resp: bytes | bytearray | None) -> bool:
    """Unlock notify: current pairing key accepted for auth/unlock (prefix 0x81)."""
    if resp is None or len(resp) < 1:
        return False
    return resp[0] == 0x81


def _is_token_unlock_ack(resp: bytes | bytearray | None, token: bytes) -> bool:
    """Token-unlock notify: prefix 0x91, status 0x00, and 4-byte token echo.

    The device echoes the exact 4 host-chosen bytes, so we both check the
    success status and confirm the echo matches the value we sent.
    """
    if resp is None or len(resp) < 6:
        return False
    return resp[0] == 0x91 and resp[1] == 0x00 and bytes(resp[2:6]) == token


async def _maybe_send_unlock_probe(session, replies: Notifications) -> None:
    """Best-effort 0x02 probe used by aggressive classic timing profiles.

    ``request`` drops what arrived before the write, so a late reply to the
    probe is not taken for the answer to the unlock that follows.
    """
    if not session._config.aggressive_gatt_timing:
        return
    try:
        await replies.request(
            UNLOCK_CHARACTERISTIC_UUID,
            b'\x02' + b'\x00' * 16,
            timeout=_UNLOCK_PROBE_WAIT_TIMEOUT_SEC,
            write_timeout=WRITE_TIMEOUT_S,
            step="unlock_probe",
            response=True,
        )
    except Exception:
        pass

def _latest(replies: Notifications, current: bytes | None) -> bytes | None:
    """The newest frame queued, or ``current`` when nothing new arrived.

    The pairing exchange answers with whatever the cuff sent last, not the
    first frame after a write: a state frame can follow an earlier one.
    """
    arrived = replies.clear()
    return arrived[-1] if arrived else current


async def _apply_pairing_settle_delay(session, aggressive_timing: bool) -> None:
    """Wait briefly after RX notify before unlock subscribe."""
    if aggressive_timing:
        await asyncio.sleep(_PAIRING_SETTLE_AGGRESSIVE_SEC)
        await _bleak_refresh_services(session._client)
    else:
        await asyncio.sleep(_PAIRING_SETTLE_DEFAULT_SEC)


async def _secure_unlock(session) -> None:
    """Authenticate the application session, establishing or resuming it.

    A pairing session runs the full initialization and keeps the credential
    the device leaves behind only once it accepts the close; every later
    session replays that credential and writes nothing. Losing the
    credential costs the user another pass through the cuff's -P- window,
    so it is only ever replaced by a completed initialization.
    """
    if not session._pairing_session and session._credential is None:
        raise ConnectionError(
            f"No stored transport credential for {session._config.model}; "
            "re-add the device while it is in pairing mode"
        )
    credential = await establish_secure_session(
        session,
        # Re-pairing establishes a fresh credential rather than resuming
        # one, which is also what the device expects in its -P- window.
        stored_ltk=None if session._pairing_session else session._credential,
        now=dt.datetime.now(),
    )
    if credential != session._credential:
        session._credential = credential
        session._new_credential = credential



async def _token_unlock(session, *, keep_notify: bool = False) -> None:
    """Unlock via the stateless 0x11/0x91 token handshake.

    The host sends ``0x11 + 4 nonce bytes + 15 zero pad`` (a 20-byte frame,
    matching the official app's write-without-response) and the device
    replies on the same characteristic with ``0x91 0x00 + echo``.  The 4
    bytes are not a stored secret — confirmed via HCI btsnoop where the same
    device echoed two different host-chosen values — so a fresh random nonce
    is used each time and verified against the echo.

    HCI btsnoop (HEM-7142T2) shows the official app enables the RX-channel
    CCCD before the unlock-channel CCCD, then issues the 0x11 write — mirror
    that ordering here (same RX pre-notify priming as CLASSIC_KEY unlock).

    With ``keep_notify`` the CCCD subscriptions are left in place on the way
    out. SECURE_SESSION devices need this: both CCCDs must be enabled once
    and the token handshake and the ECDH pairing request must run
    back-to-back on those same subscriptions. Unsubscribing in between
    makes the device reject the pairing request (0xff 0x26).
    """
    token = secrets.token_bytes(4)
    packet = b"\x11" + token + b"\x00" * 15
    rx_notify_primed = False

    def _is_ack(data: bytes) -> bool:
        return _is_token_unlock_ack(data, token)

    await session._ensure_services_cache()

    # Official app: RX notify CCCD (h=33) before unlock CCCD (h=28).
    #
    # When the subscription is kept for the life of the link, prime it with
    # the real handler rather than a dead callback: the memory session then
    # inherits it instead of calling start_notify on an already-enabled
    # CCCD, which the backends reject and recover from by writing the CCCD
    # back to 0x0000 first — the very churn keep_notify exists to avoid.
    try:
        session.memory._rebuild_notify_handle_index_map()
        await start_notify_with_recovery(
            session._client,
            session._config.rx_channel_uuids[0],
            session.memory._on_notify_channel_data if keep_notify else (lambda _h, _d: None),
        )
        rx_notify_primed = True
        if keep_notify:
            session.memory._notify_subscribed = True
        await asyncio.sleep(_NOTIFY_SUBSCRIBE_SETTLE_SEC)
    except asyncio.CancelledError:
        if rx_notify_primed:
            await stop_notify_best_effort(session._client, session._config.rx_channel_uuids[0])
        raise
    except Exception as exc:
        _LOGGER.debug("token unlock RX pre-notify prime skipped: %s", exc)

    session._debug_ble_link("token_unlock_before_notify")
    # The unlock subscription is owned by this stack. A kept subscription is
    # handed to the session with pop_all(); anything else is released here.
    subscriptions = contextlib.AsyncExitStack()
    replies: Notifications | None = None
    try:
        replies = await subscriptions.enter_async_context(
            Notifications(
                session._client,
                UNLOCK_CHARACTERISTIC_UUID,
                settle=_NOTIFY_SUBSCRIBE_SETTLE_SEC,
                recover=True,
            )
        )
        # Prefer write-without-response (ATT Write Command, per btsnoop); fall
        # back to write-with-response when stacks/proxies drop command writes.
        for use_response in (False, True):
            _LOGGER.debug(
                "Token unlock write nonce=%s response=%s",
                token.hex(),
                use_response,
            )
            try:
                # A frame that is not the 4-byte token echo is not the answer:
                # keep waiting for it until the timeout.
                await replies.request(
                    UNLOCK_CHARACTERISTIC_UUID,
                    packet,
                    timeout=_UNLOCK_AUTH_WAIT_TIMEOUT_SEC,
                    write_timeout=WRITE_TIMEOUT_S,
                    step="unlock",
                    response=use_response,
                    accept=_is_ack,
                )
                break
            except WriteTimeout:
                raise
            except NotificationTimeout:
                if use_response:
                    session._debug_ble_link("token_unlock_notify_timeout")
                    raise ConnectionError(
                        "Token unlock failed: notify timeout"
                    ) from None
                _LOGGER.debug(
                    "Token unlock notify timeout with response=False; "
                    "retrying write with response=True"
                )

        session._unlocked = True
        _LOGGER.debug("Token unlock OK (nonce=%s)", token.hex())
    finally:
        if keep_notify and session._unlocked and replies is not None:
            session.hold_unlock_channel(subscriptions.pop_all(), replies)
            session._debug_ble_link("token_unlock_keep_notify")
        else:
            await subscriptions.aclose()
            if rx_notify_primed:
                await stop_notify_best_effort(session._client, session._config.rx_channel_uuids[0])
            session._debug_ble_link("token_unlock_after_stop_notify")


async def _pair_custom_key(session, pair_key: bytearray) -> None:
    """Program a new custom pairing key on classic profiles."""
    if len(pair_key) != 16:
        raise ValueError(f"Pairing key must be 16 bytes, got {len(pair_key)}")

    aggressive_timing = session._config.aggressive_gatt_timing
    if aggressive_timing:
        await _bleak_refresh_services(session._client)
        unlock_attempts, unlock_retry_delay = _PAIR_UNLOCK_ATTEMPTS_AGGRESSIVE, _OS_BOND_RETRY_DELAY_SEC
        key_max_retries = 5
    else:
        unlock_attempts, unlock_retry_delay = _PAIR_UNLOCK_ATTEMPTS_DEFAULT, _PAIRING_SETTLE_DEFAULT_SEC
        key_max_retries = 5

    # The subscriptions are released on every way out, in the reverse order
    # they were made: unlock first, then the RX one that triggers SMP.
    subscriptions = contextlib.AsyncExitStack()
    try:
        # This subscribe triggers SMP; its failure is the only record of why
        # the link dies a moment later (#2).
        _LOGGER.debug("Enabling RX notification to trigger BLE pairing")
        rx_notify_error: str | None = None
        try:
            await subscriptions.enter_async_context(
                Notifications(session._client, session._config.rx_channel_uuids[0])
            )
        except Exception as exc:
            rx_notify_error = f"{type(exc).__name__}: {exc}"
            _LOGGER.debug("Ignored error starting RX notify: %s", exc)

        await _apply_pairing_settle_delay(session, aggressive_timing)

        if not getattr(session._client, "is_connected", True):
            # Dead already: no point retrying the unlock subscribe ten times.
            raise ConnectionError(
                "The cuff dropped the link right after the pairing request"
                + (f" ({rx_notify_error})" if rx_notify_error else "")
                + ". Make sure it shows the blinking -P- symbol and that no "
                "phone is connected to it."
            )

        replies: Notifications | None = None
        for attempt in range(unlock_attempts):
            try:
                replies = await subscriptions.enter_async_context(
                    Notifications(session._client, UNLOCK_CHARACTERISTIC_UUID)
                )
                break
            except Exception as exc:
                _LOGGER.debug(
                    "Unlock characteristic not ready (%s/%s): %s",
                    attempt + 1,
                    unlock_attempts,
                    exc,
                )
                if not getattr(session._client, "is_connected", True):
                    # The link is gone: retrying cannot bring the
                    # characteristic back, and the "not found" message below
                    # would send the user off clearing caches for a GATT
                    # database that was never the problem. Typically the cuff
                    # dropped us because its SMP request went unanswered.
                    raise ConnectionError(
                        "Device disconnected while subscribing to "
                        f"{UNLOCK_CHARACTERISTIC_UUID} during pairing "
                        f"({type(exc).__name__}: {exc})."
                        + (
                            " The pairing request that preceded it also failed"
                            f" ({rx_notify_error})."
                            if rx_notify_error
                            else ""
                        )
                        + " The cuff dropped the link — make sure it shows the "
                        "blinking -P- symbol and that no phone is connected to it."
                    ) from exc
                if aggressive_timing:
                    await _bleak_refresh_services(session._client)
                await asyncio.sleep(unlock_retry_delay)
        if replies is None:
            raise ConnectionError(
                f"Characteristic {UNLOCK_CHARACTERISTIC_UUID} was not found! "
                "Try clearing Bluetooth cache, or remove the device from OS Bluetooth and retry in -P- mode."
            )

        max_retries = key_max_retries
        entered_programming = False
        last_notify: bytes | None = None
        notify_samples: list[str] = []
        write_failures = 0
        resp: bytes | None = None
        for attempt in range(max_retries):
            # The newest frame the cuff has sent so far: one that came in after
            # the last wait gave up (during the settle pause) still counts.
            resp = _latest(replies, resp)
            if _is_unlock_key_programming_ready(resp):
                _LOGGER.debug("Entered key programming mode after %d attempt(s)", attempt)
                entered_programming = True
                break

            replies.clear()
            try:
                await guarded_write(
                    session._client, UNLOCK_CHARACTERISTIC_UUID, b'\x02' + b'\x00' * 16,
                    step="pairing", response=True,
                )
            except Exception as exc:
                write_failures += 1
                _LOGGER.debug("Key programming write attempt %d failed: %s", attempt + 1, exc)

            try:
                resp = await replies.next(_PAIRING_PROG_WAIT_TIMEOUT_SEC, step="pairing")
                resp = _latest(replies, resp)
            except NotificationTimeout:
                resp = None
            if resp:
                last_notify = bytes(resp)
                if len(notify_samples) < 10:
                    notify_samples.append(f"#{attempt + 1}:{_hex(resp)}")
            if _is_unlock_key_programming_ready(resp):
                _LOGGER.debug("Entered key programming mode after %d attempt(s)", attempt + 1)
                entered_programming = True
                break

            _LOGGER.debug(
                "Key programming attempt %d/%d got: %s",
                attempt + 1, max_retries,
                resp[:2].hex() if resp else "None",
            )
            await asyncio.sleep(_PAIRING_SETTLE_DEFAULT_SEC)

        if not entered_programming:
            _LOGGER.error(
                "Key programming mode not reached: model=%s aggressive_gatt_timing=%s "
                "unlock_uuid=%s attempts=%s write_failures=%s "
                "expected_notify_first_byte=0x82 last_notify_hex=%s samples=%s",
                session._config.model,
                aggressive_timing,
                UNLOCK_CHARACTERISTIC_UUID,
                max_retries,
                write_failures,
                _hex(last_notify) if last_notify else "None",
                notify_samples or ["(no notifications)"],
            )
            raise ConnectionError(
                "Could not enter key programming mode. "
                "Is the device in pairing mode? (hold bluetooth button until -P- appears)"
            )

        replies.clear()
        try:
            await guarded_write(
                session._client, UNLOCK_CHARACTERISTIC_UUID, b'\x00' + pair_key,
                step="pairing_key", response=True,
            )
        except Exception as exc:
            _LOGGER.error("Failed to write new key: %s", exc)

        try:
            resp = await replies.next(_PAIRING_KEY_ACK_WAIT_TIMEOUT_SEC, step="pairing_key")
            resp = _latest(replies, resp)
        except NotificationTimeout:
            resp = None
    finally:
        await subscriptions.aclose()

    if not _is_unlock_pairing_key_ack(resp):
        raise ConnectionError(f"Failed to program pairing key. Response: {resp.hex() if resp else 'None'}")

    _LOGGER.debug("Device paired successfully with new key")
    await asyncio.sleep(_PAIRING_SETTLE_DEFAULT_SEC)
