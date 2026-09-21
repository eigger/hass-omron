"""One connected BLE session to an Omron device: connection lifecycle, pairing and unlock (the memory protocol is mixed in from memory_protocol)."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import secrets
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, AsyncIterator

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from .bluez import (
    _bluez_agent_pair,
    _bluez_device_path,
    _bluez_is_paired,
    _bluez_pairing_agent,
    _bluez_remove_device,
    _is_non_fatal_os_pairing_error,
    _is_stale_bond_auth_error,
    _log_pairing_failure_detail,
    is_local_adapter,
)
from .connection import (
    _bleak_clear_cache,
    _bleak_refresh_services,
    establish_connection_with_bond_settle,
)
from .const import (
    MODEL_NUMBER_UUID,
    SERVICE_CHANGED_UUID,
    UNLOCK_CHARACTERISTIC_UUID,
)
from .devices import DeviceConfig, HostPairingMode, UnlockMode
from .memory_protocol import _NOTIFY_SUBSCRIBE_SETTLE_SEC, MemoryProtocolMixin
from .secure_flow import ASYNC_NOTICE_UUID, establish_secure_session
from .session_trace import SessionTrace, traced
from .util import _hex

_LOGGER = logging.getLogger(__name__)

# Polling step while waiting for the device to end a session itself.
_PEER_CLOSE_POLL_STEP_SEC: float = 0.25
_OS_BOND_REFRESH_DELAY_SEC: float = 0.3
_OS_BOND_RETRY_DELAY_SEC: float = 0.5
_UNLOCK_PROBE_WAIT_TIMEOUT_SEC: float = 2.0
_UNLOCK_AUTH_WAIT_TIMEOUT_SEC: float = 5.0
_PAIRING_SETTLE_AGGRESSIVE_SEC: float = 0.25
_PAIRING_SETTLE_DEFAULT_SEC: float = 1.0
_PAIRING_PROG_WAIT_TIMEOUT_SEC: float = 2.0
_PAIRING_KEY_ACK_WAIT_TIMEOUT_SEC: float = 5.0
_PAIR_UNLOCK_ATTEMPTS_AGGRESSIVE: int = 10
_PAIR_UNLOCK_ATTEMPTS_DEFAULT: int = 5

PAIRING_KEY = bytearray.fromhex("deadbeaf12341234deadbeaf12341234")


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


class OmronDeviceSession(MemoryProtocolMixin):
    """A connected BLE session to one Omron device.

    Owns the connection lifecycle (use as an ``async with`` context manager, or
    ``connect()`` / ``aclose()``), pairing and unlock. The notify channels,
    the command/reply exchange and the memory session come from
    ``MemoryProtocolMixin``. Supports single-channel (OS-bonding) and
    multi-channel (classic pairing) profiles.
    """

    def __init__(
        self,
        ble_device: BLEDevice,
        device_config: DeviceConfig,
        *,
        pairing_session: bool = False,
        credential: bytes | None = None,
    ) -> None:
        self._ble_device = ble_device
        self._config = device_config
        # Application-layer credential for SECURE_SESSION profiles: None on the
        # session that establishes one, the stored value on every later poll.
        self._credential = credential
        # Only a session that exists to create a bond sends the connect-time
        # pair request on profiles that opt out of it for polls.
        self._pairing_session = pairing_session
        self._init_session_state(client=None, owns_connection=True)

    def _init_session_state(
        self, *, client: BleakClient | None, owns_connection: bool
    ) -> None:
        self._client = client
        # Set only when this session established a credential worth storing.
        self._new_credential: bytes | None = None
        # Pairing agent held for the lifetime of the session, for profiles
        # where the device drives security itself.
        self._pairing_agent: AsyncExitStack | None = None
        self._owns_connection = owns_connection
        # Stage timings and the failure point, for the diagnostic sensors. A
        # poll that adopts this session swaps in its own so the breakdown it
        # publishes matches its own duration; the link facts stay here.
        self.trace = SessionTrace()
        self.link_info: dict[str, Any] = {}
        self._init_memory_protocol_state()
        self._unlocked = False
        self._secure_session = None
        # Swappable handler for the unlock characteristic notifications. The
        # CCCD is subscribed once (via a fixed dispatcher) so the token
        # handshake and the ECDH secure handshake can run back-to-back on the
        # same subscription — re-subscribing mid-flow either resets the device
        # session or trips the backend's "already enabled" guard.
        self._unlock_notify_handler: Any = None

    # -- connection lifecycle -------------------------------------------------

    @classmethod
    def adopt(
        cls,
        client: BleakClient,
        device_config: DeviceConfig,
        *,
        pairing_session: bool = False,
        credential: bytes | None = None,
    ) -> "OmronDeviceSession":
        """Wrap an already-open client to run ops over a connection owned elsewhere.

        ``aclose()`` will not disconnect an adopted client.
        """
        session = cls.__new__(cls)
        session._ble_device = getattr(client, "_device", None)
        session._config = device_config
        # __init__ is bypassed, so this has to be set by hand.
        session._pairing_session = pairing_session
        session._credential = credential
        session._init_session_state(client=client, owns_connection=False)
        return session

    @property
    def client(self) -> BleakClient:
        """Return the live Bleak client, raising if the session is not connected."""
        if self._client is None:
            raise ConnectionError("OmronDeviceSession is not connected")
        return self._client

    @property
    def new_credential(self) -> bytes | None:
        """A credential this session established that the caller should store.

        ``None`` unless a SECURE_SESSION profile completed an initialization the
        device accepted. Callers persist it against this entry; losing it costs
        the user another pass through the cuff's pairing mode.
        """
        return self._new_credential

    @property
    def config(self) -> DeviceConfig:
        """Return the device profile this session was opened for."""
        return self._config

    @property
    def address(self) -> str:
        """Return the device BLE address (best effort)."""
        if self._ble_device is not None:
            addr = getattr(self._ble_device, "address", None)
            if addr:
                return addr
        if self._client is not None:
            return getattr(self._client, "address", "") or ""
        return ""

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def connect(self) -> "OmronDeviceSession":
        """Open the BLE link and let bonding/encryption settle before first use."""
        if self._client is not None and self._client.is_connected:
            return self
        if self._ble_device is None:
            raise ConnectionError(
                "OmronDeviceSession.adopt() sessions cannot connect; open the client first"
            )
        if (
            self._config.register_pairing_agent
            and self._pairing_agent is None
            and is_local_adapter(self._ble_device)
        ):
            # For the whole session, not just the connect: on these profiles the
            # device raises its Security Request on its own schedule, and the
            # hardware-verified sequence had an agent registered throughout.
            # Released in aclose().
            stack = AsyncExitStack()
            try:
                await stack.enter_async_context(_bluez_pairing_agent())
            except Exception as exc:
                await stack.aclose()
                _LOGGER.debug(
                    "Could not hold a pairing agent for %s: %s",
                    self._config.model,
                    exc,
                )
            else:
                self._pairing_agent = stack
        with self.trace.timed("connect"):
            self._client = await establish_connection_with_bond_settle(
                self._ble_device,
                self.address,
                model=self._config.model,
                max_attempts=self._config.connect_settle_attempts,
                # Only the connection that creates the bond; a reconnect that sends
                # a pair request is what cost the bond on a proxy (#142).
                pair_on_connect=self._pairing_session and self._config.pair_on_connect,
                hold_pairing_agent=self._config.register_pairing_agent,
            )
        self.link_info = dict(getattr(self._client, "_omron_link_info", None) or {})
        return self

    async def refresh_services(self) -> None:
        """Re-run GATT discovery so characteristics appear after connection."""
        await _bleak_refresh_services(self.client)

    @traced("services")
    async def verify_parent_service(self) -> bool:
        """Ensure the parent service is present: check, refresh once, then
        clear_cache + re-discover (the only step that beats a stale cache)."""
        parent_uuid = self._config.parent_service_uuid

        def _present() -> bool:
            try:
                return parent_uuid in [s.uuid for s in self.client.services]
            except Exception as exc:
                _LOGGER.debug("Services not ready for %s: %s", self.address, exc)
                return False

        if _present():
            return True

        # Populate/refresh discovery once (cache may be empty post-connect).
        _LOGGER.debug(
            "Parent service %s not in cached services for %s; "
            "refreshing GATT discovery",
            parent_uuid, self.address,
        )
        await self.refresh_services()
        await asyncio.sleep(0.35)
        if _present():
            _LOGGER.debug(
                "Parent service %s found after discovery refresh for %s",
                parent_uuid, self.address,
            )
            return True

        # Still missing → the cached list is stale. Force a fresh discovery by
        # dropping the backend/proxy GATT cache. Two short attempts; stop early
        # if the backend cannot clear its cache (further retries are pointless).
        _LOGGER.debug(
            "Parent service %s still missing after refresh for %s; "
            "forcing fresh discovery via clear_cache (stale proxy/GATT cache "
            "suspected)",
            parent_uuid, self.address,
        )
        for attempt in range(2):
            if not await _bleak_clear_cache(self.client):
                _LOGGER.debug(
                    "clear_cache unsupported by backend for %s; "
                    "cannot force fresh discovery", self.address,
                )
                break
            _LOGGER.debug(
                "Cleared GATT cache; re-discovering parent service %s "
                "(attempt %d/2) for %s",
                parent_uuid, attempt + 1, self.address,
            )
            await self.refresh_services()
            await asyncio.sleep(0.35)
            if _present():
                _LOGGER.debug(
                    "Parent service %s recovered after cache clear "
                    "(attempt %d/2) for %s",
                    parent_uuid, attempt + 1, self.address,
                )
                return True
        return False

    async def read_model_number(self) -> str | None:
        """Read the standard Model Number string characteristic (if present)."""
        char = self.client.services.get_characteristic(MODEL_NUMBER_UUID)
        if char is None:
            return None
        raw = await self.client.read_gatt_char(char)
        if not raw:
            return None
        return raw.decode("utf-8").strip(" \x00")

    def release_for_handoff(self) -> "OmronDeviceSession":
        """Hand off this session for the first poll; ``aclose()`` will not disconnect."""
        self._owns_connection = False
        return self

    def reclaim_ownership(self) -> None:
        """Take back disconnect responsibility after a setup handoff."""
        self._owns_connection = True

    def release_client(self) -> BleakClient:
        """Hand off the live Bleak client; ``aclose()`` will not disconnect afterward."""
        self.release_for_handoff()
        return self.client

    async def aclose(self) -> None:
        """Close any open memory session and (if owned) drop the BLE link."""
        client = self._client
        if client is None:
            return
        addr = self.address or getattr(client, "address", "")
        disconnected = False
        try:
            if self._memory_session_active:
                try:
                    await self.close_memory_session()
                except Exception as exc:
                    if self._pairing_registration_head_done:
                        # Whether the cuff commits the mirror on the write or
                        # on the close is not established, and this path has
                        # no retry: say so, so a refused reconnect later has a
                        # cause in the log.
                        _LOGGER.warning(
                            "%s: the session that wrote the pairing "
                            "registration did not close cleanly (%s); if the "
                            "next reconnect is refused, pair again",
                            addr,
                            exc,
                        )
            if self._owns_connection and client.is_connected:
                with self.trace.timed("disconnect"):
                    await self._await_peer_close(client, addr)
                    if client.is_connected:
                        await client.disconnect()
                        disconnected = True
        except Exception:
            pass
        finally:
            self._client = None
            if self._pairing_agent is not None:
                agent, self._pairing_agent = self._pairing_agent, None
                try:
                    await agent.aclose()
                except Exception as exc:
                    _LOGGER.debug("Releasing the pairing agent failed: %s", exc)
            if disconnected:
                _LOGGER.debug("BLE link closed for %s", addr)

    async def _await_peer_close(self, client: BleakClient, addr: str) -> None:
        """Stay idle at the end of a session so the cuff can end it itself.

        In the #91 phone capture the app sends nothing after its last read and
        the cuff hangs up about three seconds later. The phone's host issues no
        HCI Disconnect for that link, and the Disconnection Complete carries
        reason 0x13 -- remote terminated. We used to close in the same
        millisecond as the final notification, which is why our sessions ended
        0x16, closed by us.

        No-op unless the profile keeps its notify subscriptions; see
        ``DeviceConfig.peer_closes_session_sec``.
        """
        window = self._config.peer_closes_session_sec
        if window <= 0:
            return
        waited = 0.0
        while waited < window and client.is_connected:
            await asyncio.sleep(_PEER_CLOSE_POLL_STEP_SEC)
            waited += _PEER_CLOSE_POLL_STEP_SEC
        if client.is_connected:
            _LOGGER.debug(
                "%s still up %.1fs after the session ended; closing it here",
                addr, waited,
            )
        else:
            _LOGGER.debug(
                "%s ended the session itself after %.2fs", addr, waited
            )

    async def __aenter__(self) -> "OmronDeviceSession":
        return await self.connect()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def _bluez_target(self) -> "BleakClient | BLEDevice":
        """Whichever of the client and the BLEDevice carries a BlueZ path.

        Both can answer, and neither always does: a proxy link has no path on
        either. Asking only the client left every BlueZ bond check unanswered
        (#92).
        """
        if self._client is not None and _bluez_device_path(self._client):
            return self._client
        return self._ble_device

    def _require_connected(self, context: str) -> None:
        """Raise if the Bleak client is not connected (avoids opaque service-cache errors)."""
        try:
            if not self._client.is_connected:
                raise ConnectionError(
                    f"BLE disconnected ({context}); retry the poll when the device is in range"
                )
        except ConnectionError:
            raise
        except Exception as exc:
            raise ConnectionError(
                f"BLE connection state unavailable ({context}): {exc}"
            ) from exc

    async def _ensure_services_cache(self) -> None:
        """Ensure GATT services are usable (refresh if Bleak has not populated the cache)."""
        self._require_connected("GATT service cache")
        try:
            _ = self._client.services
        except BleakError as exc:
            msg = str(exc).lower()
            if "discovery has not been performed" in msg or "not been performed" in msg:
                await _bleak_refresh_services(self._client)
            else:
                raise

    def _debug_ble_link(self, tag: str) -> None:
        """Hook for BLE link tracing (disabled)."""
        return

    async def reset_session_state(self) -> None:
        """Release any stale BLE notify subscriptions and reset session flags.

        Call this before retrying ``open_memory_session`` when a previous
        attempt failed with a BlueZ ``Notify acquired`` or ``Failed to register
        notify session`` error.  The stop_notify calls are
        best-effort; failures are silently ignored so the caller can proceed with
        the next attempt regardless.
        """
        await self._unsubscribe_notify_channels(force=True)
        # The unlock characteristic is not an RX channel, so the loop above
        # never covered it -- and it is the one BlueZ was still holding (#92).
        try:
            await self._client.stop_notify(UNLOCK_CHARACTERISTIC_UUID)
        except Exception as exc:
            _LOGGER.debug("unlock stop_notify during reset ignored: %s", exc)
        # Same for the secure session's async-notice channel: a retry
        # re-subscribes it, and BlueZ refuses a second subscribe on a CCCD it
        # still holds.
        try:
            await self._client.stop_notify(ASYNC_NOTICE_UUID)
        except Exception as exc:
            _LOGGER.debug("async-notice stop_notify during reset ignored: %s", exc)
        self._unlocked = False
        self._secure_session = None
        self._memory_session_active = False
        # Only the fresh-session retry loop in async_poll resets between
        # attempts; the handed-off pairing session has no retry and its close
        # failure is reported from aclose() instead. Where a retry does happen
        # the registration is written again, since whether the cuff commits
        # the mirror on the write or on the close is not established -- a
        # transfer count stepped twice beats a registration silently lost.
        self._pairing_registration_head_done = False
        self._pairing_registration_clock_done = False
        self._channel_fragments = [None] * 4
        self._expected_reply_packet_type = None
        self._expected_reply_memory_address = None
        self._reply_ready.clear()
        self._debug_ble_link("reset_session_state")

    @asynccontextmanager
    async def memory_session_after_unlock(
        self, *, pair_first: bool = False
    ) -> AsyncIterator[None]:
        """Unlock (optional pair) then hold one memory readout session."""
        if pair_first:
            try:
                await self.pair()
            except Exception as exc:
                self.trace.forgive("pair")
                _LOGGER.debug(
                    "Poll pair step failed (continuing to unlock): %s", exc
                )
        await self.unlock()
        async with self.memory_session():
            yield

    async def _maybe_send_unlock_probe(
        self,
        unlock_event: asyncio.Event,
        response_holder: list[bytes | None],
    ) -> None:
        """Best-effort 0x02 probe used by aggressive classic timing profiles."""
        if not self._config.aggressive_gatt_timing:
            return
        unlock_event.clear()
        response_holder[0] = None
        try:
            await self._client.write_gatt_char(
                UNLOCK_CHARACTERISTIC_UUID, b'\x02' + b'\x00' * 16, response=True
            )
            await asyncio.wait_for(unlock_event.wait(), timeout=_UNLOCK_PROBE_WAIT_TIMEOUT_SEC)
        except Exception:
            pass

    async def _apply_pairing_settle_delay(self, aggressive_timing: bool) -> None:
        """Wait briefly after RX notify before unlock subscribe."""
        if aggressive_timing:
            await asyncio.sleep(_PAIRING_SETTLE_AGGRESSIVE_SEC)
            await _bleak_refresh_services(self._client)
        else:
            await asyncio.sleep(_PAIRING_SETTLE_DEFAULT_SEC)

    async def _secure_unlock(self) -> None:
        """Authenticate the application session, establishing or resuming it.

        A pairing session runs the full initialization and keeps the credential
        the device leaves behind only once it accepts the close; every later
        session replays that credential and writes nothing. Losing the
        credential costs the user another pass through the cuff's -P- window,
        so it is only ever replaced by a completed initialization.
        """
        if not self._pairing_session and self._credential is None:
            raise ConnectionError(
                f"No stored transport credential for {self._config.model}; "
                "re-add the device while it is in pairing mode"
            )
        credential = await establish_secure_session(
            self,
            # Re-pairing establishes a fresh credential rather than resuming
            # one, which is also what the device expects in its -P- window.
            stored_ltk=None if self._pairing_session else self._credential,
            now=dt.datetime.now(),
        )
        if credential != self._credential:
            self._credential = credential
            self._new_credential = credential

    @traced("unlock")
    async def unlock(self, key: bytearray | None = None) -> None:
        """Unlock device with pairing key."""
        if self._config.unlock_mode == UnlockMode.NONE:
            _LOGGER.debug("unlock skipped: unlock not required for model=%s", self._config.model)
            return
        if self._unlocked:
            _LOGGER.debug("unlock skipped: transport already unlocked model=%s", self._config.model)
            return

        self._require_connected("unlock")

        # Encrypted secure handshake path
        if self._config.unlock_mode == UnlockMode.SECURE_SESSION:
            await self._secure_unlock()
            return

        # Stateless token handshake (0x11 / 0x91)
        if self._config.unlock_mode == UnlockMode.TOKEN_KEY:
            await self._token_unlock(
                keep_notify=self._config.keep_notify_subscriptions
            )
            return

        unlock_key = key or PAIRING_KEY
        unlock_event = asyncio.Event()
        response_holder: list[bytes | None] = [None]
        rx_notify_primed = False

        def _unlock_callback(_: Any, rx_bytes: bytearray) -> None:
            response_holder[0] = rx_bytes
            unlock_event.set()

        # Match pairing flow: briefly prime RX notify so stacks that require
        # a security request trigger can establish encrypted notify reliably.
        try:
            await self._client.start_notify(
                self._config.rx_channel_uuids[0], lambda _h, _d: None
            )
            rx_notify_primed = True
            await asyncio.sleep(_NOTIFY_SUBSCRIBE_SETTLE_SEC)
        except Exception as exc:
            _LOGGER.debug("unlock RX pre-notify prime skipped: %s", exc)

        self._debug_ble_link("unlock_before_notify")
        await self._client.start_notify(UNLOCK_CHARACTERISTIC_UUID, _unlock_callback)
        await asyncio.sleep(_NOTIFY_SUBSCRIBE_SETTLE_SEC)
        try:
            # Some classic custom-key models are more stable with a 0x02 probe before auth-key unlock.
            await self._maybe_send_unlock_probe(unlock_event, response_holder)

            unlock_event.clear()
            response_holder[0] = None
            await self._client.write_gatt_char(
                UNLOCK_CHARACTERISTIC_UUID, b'\x01' + unlock_key, response=True
            )
            await asyncio.wait_for(unlock_event.wait(), timeout=_UNLOCK_AUTH_WAIT_TIMEOUT_SEC)

            response = response_holder[0]
            if not _is_unlock_auth_key_ack(response):
                _LOGGER.debug(
                    "Unlock failed (pairing key mismatch): notify len=%s hex=%s",
                    len(response) if response is not None else None,
                    _hex(response) if response else "None",
                )
                raise ConnectionError("Unlock failed: pairing key mismatch")
            
            self._unlocked = True
        except asyncio.TimeoutError:
            self._debug_ble_link("unlock_notify_timeout")
            raise ConnectionError("Unlock failed: notify timeout") from None
        finally:
            await self._client.stop_notify(UNLOCK_CHARACTERISTIC_UUID)
            if rx_notify_primed:
                try:
                    await self._client.stop_notify(self._config.rx_channel_uuids[0])
                except Exception as exc:
                    _LOGGER.debug("unlock RX pre-notify stop skipped: %s", exc)
            self._debug_ble_link("unlock_after_stop_notify")

    async def _token_unlock(self, *, keep_notify: bool = False) -> None:
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
        unlock_event = asyncio.Event()
        response_holder: list[bytes | None] = [None]
        rx_notify_primed = False

        def _token_callback(_: Any, rx_bytes: bytearray) -> None:
            data = bytes(rx_bytes)
            if not _is_token_unlock_ack(data, token):
                return
            response_holder[0] = data
            unlock_event.set()

        # Subscribe the unlock CCCD through a fixed dispatcher that forwards to
        # whatever handler is currently installed. This lets _secure_unlock swap
        # in its own handler afterward without a second start_notify (which the
        # device / backend reject on an already-enabled CCCD).
        self._unlock_notify_handler = _token_callback

        def _unlock_dispatch(char: Any, rx_bytes: bytearray) -> None:
            handler = self._unlock_notify_handler
            if handler is not None:
                handler(char, rx_bytes)

        await self._ensure_services_cache()

        # Official app: RX notify CCCD (h=33) before unlock CCCD (h=28).
        #
        # When the subscription is kept for the life of the link, prime it with
        # the real handler rather than a dead callback: the memory session then
        # inherits it instead of calling start_notify on an already-enabled
        # CCCD, which the backends reject and recover from by writing the CCCD
        # back to 0x0000 first — the very churn keep_notify exists to avoid.
        try:
            self._rebuild_notify_handle_index_map()
            await self._start_notify_with_recovery(
                self._config.rx_channel_uuids[0],
                self._on_notify_channel_data if keep_notify else (lambda _h, _d: None),
            )
            rx_notify_primed = True
            if keep_notify:
                self._notify_subscribed = True
            await asyncio.sleep(_NOTIFY_SUBSCRIBE_SETTLE_SEC)
        except Exception as exc:
            _LOGGER.debug("token unlock RX pre-notify prime skipped: %s", exc)

        self._debug_ble_link("token_unlock_before_notify")
        await self._start_notify_with_recovery(
            UNLOCK_CHARACTERISTIC_UUID, _unlock_dispatch
        )
        await asyncio.sleep(_NOTIFY_SUBSCRIBE_SETTLE_SEC)
        try:
            unlock_event.clear()
            response_holder[0] = None
            # Prefer write-without-response (ATT Write Command, per btsnoop); fall
            # back to write-with-response when stacks/proxies drop command writes.
            for use_response in (False, True):
                _LOGGER.debug(
                    "Token unlock write nonce=%s response=%s",
                    token.hex(),
                    use_response,
                )
                await self._client.write_gatt_char(
                    UNLOCK_CHARACTERISTIC_UUID, packet, response=use_response
                )
                try:
                    await asyncio.wait_for(
                        unlock_event.wait(), timeout=_UNLOCK_AUTH_WAIT_TIMEOUT_SEC
                    )
                    break
                except asyncio.TimeoutError:
                    if use_response:
                        self._debug_ble_link("token_unlock_notify_timeout")
                        raise ConnectionError(
                            "Token unlock failed: notify timeout"
                        ) from None
                    unlock_event.clear()
                    response_holder[0] = None
                    _LOGGER.debug(
                        "Token unlock notify timeout with response=False; "
                        "retrying write with response=True"
                    )

            response = response_holder[0]
            if not _is_token_unlock_ack(response, token):
                _LOGGER.debug(
                    "Token unlock failed: sent=%s notify len=%s hex=%s",
                    token.hex(),
                    len(response) if response is not None else None,
                    _hex(response) if response else "None",
                )
                raise ConnectionError("Token unlock failed: missing/invalid 0x91 ack")

            self._unlocked = True
            _LOGGER.debug("Token unlock OK (nonce=%s)", token.hex())
        finally:
            if keep_notify:
                self._debug_ble_link("token_unlock_keep_notify")
            else:
                try:
                    await self._client.stop_notify(UNLOCK_CHARACTERISTIC_UUID)
                except Exception as exc:
                    _LOGGER.debug("token unlock stop_notify skipped: %s", exc)
                if rx_notify_primed:
                    try:
                        await self._client.stop_notify(self._config.rx_channel_uuids[0])
                    except Exception as exc:
                        _LOGGER.debug("token unlock RX pre-notify stop skipped: %s", exc)
                self._debug_ble_link("token_unlock_after_stop_notify")

    async def _pair_os_bonding(self) -> None:
        """Best-effort OS-level BLE bond establishment for modern profiles."""
        _LOGGER.debug("Performing OS-level BLE bonding")
        max_attempts = 2
        last_exc: BaseException | None = None
        stale_bond_cleared = False

        async def _post_bond_refresh() -> None:
            try:
                await asyncio.sleep(_OS_BOND_REFRESH_DELAY_SEC)
                await _bleak_refresh_services(self._client)
            except Exception as refresh_exc:
                _LOGGER.debug("Post-bond service refresh failed (continuing): %s", refresh_exc)

        for attempt in range(1, max_attempts + 1):
            try:
                if attempt > 1:
                    await asyncio.sleep(_OS_BOND_RETRY_DELAY_SEC)
                    await _bleak_refresh_services(self._client)
                agent_paired = await _bluez_agent_pair(self._bluez_target())
                if not agent_paired:
                    try:
                        await self._client.pair()
                    except TypeError:
                        await self._client.pair(protection_level=2)
                _LOGGER.debug("OS-level BLE bonding completed")
                await _post_bond_refresh()
                return
            except Exception as exc:
                last_exc = exc
                # Stale bond → AuthenticationFailed. Remove it and raise; the
                # next connection re-pairs from a clean slate.
                if _is_stale_bond_auth_error(exc) and not stale_bond_cleared:
                    # Only a bond that exists can be stale. On a first pairing
                    # there is none, and the same AuthenticationFailed just
                    # means the cuff refused -- removing nothing and abandoning
                    # the attempt spends the pairing window the user opened by
                    # pressing the button, and the "retry the poll" this used to
                    # raise lands after that window has closed (#92).
                    had_bond = await _bluez_is_paired(self._bluez_target())
                    if had_bond is not False:
                        stale_bond_cleared = True
                        _LOGGER.warning(
                            "OS-level bonding rejected (%s) for %s — removing stale "
                            "bond so the next connection can re-pair cleanly",
                            type(exc).__name__,
                            self._config.model,
                        )
                        if not await _bluez_remove_device(self._bluez_target()):
                            await self.unpair()
                        raise ConnectionError(
                            "Stale BLE bond removed after AuthenticationFailed; "
                            "retry the poll to re-pair"
                        ) from exc
                    _LOGGER.warning(
                        "OS-level bonding rejected (%s) for %s and no bond exists "
                        "to be stale — the peer refused. Attempt %d/%d",
                        type(exc).__name__,
                        self._config.model,
                        attempt,
                        max_attempts,
                    )
                    if attempt < max_attempts:
                        # Retry on this link, while the cuff is still in its
                        # pairing window. Falling through would hit the
                        # non-fatal branch below, which treats
                        # AuthenticationFailed as "no bond needed" and returns
                        # on the first refusal.
                        continue
                    # Out of attempts: leave the existing non-fatal handling to
                    # decide, so profiles that genuinely do not need a bond are
                    # unaffected.
                if _is_non_fatal_os_pairing_error(exc):
                    _LOGGER.warning(
                        "OS-level bonding returned non-fatal error on attempt %d/%d: %s (%r)",
                        attempt,
                        max_attempts,
                        type(exc).__name__,
                        exc,
                    )
                    await _post_bond_refresh()
                    return
                _LOGGER.debug(
                    "OS-level bonding attempt %d/%d failed: %s (%r)",
                    attempt,
                    max_attempts,
                    type(exc).__name__,
                    exc,
                )
        if last_exc is not None:
            _log_pairing_failure_detail(
                f"OS-level BLE bonding failed after {max_attempts} attempts",
                last_exc,
            )
            raise last_exc

    async def _pair_custom_key(self, pair_key: bytearray) -> None:
        """Program a new custom pairing key on classic profiles."""
        if len(pair_key) != 16:
            raise ValueError(f"Pairing key must be 16 bytes, got {len(pair_key)}")

        aggressive_timing = self._config.aggressive_gatt_timing
        if aggressive_timing:
            await _bleak_refresh_services(self._client)
            unlock_attempts, unlock_retry_delay = _PAIR_UNLOCK_ATTEMPTS_AGGRESSIVE, _OS_BOND_RETRY_DELAY_SEC
            key_max_retries = 5
        else:
            unlock_attempts, unlock_retry_delay = _PAIR_UNLOCK_ATTEMPTS_DEFAULT, _PAIRING_SETTLE_DEFAULT_SEC
            key_max_retries = 5

        # This subscribe triggers SMP; its failure is the only record of why
        # the link dies a moment later (#2).
        _LOGGER.debug("Enabling RX notification to trigger BLE pairing")
        rx_notify_error: str | None = None
        try:
            await self._client.start_notify(
                self._config.rx_channel_uuids[0], lambda h, d: None
            )
        except Exception as exc:
            rx_notify_error = f"{type(exc).__name__}: {exc}"
            _LOGGER.debug("Ignored error starting RX notify: %s", exc)

        await self._apply_pairing_settle_delay(aggressive_timing)

        if not getattr(self._client, "is_connected", True):
            # Dead already: no point retrying the unlock subscribe ten times.
            raise ConnectionError(
                "The cuff dropped the link right after the pairing request"
                + (f" ({rx_notify_error})" if rx_notify_error else "")
                + ". Make sure it shows the blinking -P- symbol and that no "
                "phone is connected to it."
            )

        prog_event = asyncio.Event()
        response_holder: list[bytes | None] = [None]

        def _pair_callback(_: Any, rx_bytes: bytearray) -> None:
            response_holder[0] = rx_bytes
            prog_event.set()

        unlock_subscribed = False
        for attempt in range(unlock_attempts):
            try:
                await self._client.start_notify(UNLOCK_CHARACTERISTIC_UUID, _pair_callback)
                unlock_subscribed = True
                break
            except Exception as exc:
                _LOGGER.debug(
                    "Unlock characteristic not ready (%s/%s): %s",
                    attempt + 1,
                    unlock_attempts,
                    exc,
                )
                if not getattr(self._client, "is_connected", True):
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
                    await _bleak_refresh_services(self._client)
                await asyncio.sleep(unlock_retry_delay)
        if not unlock_subscribed:
            raise ConnectionError(
                f"Characteristic {UNLOCK_CHARACTERISTIC_UUID} was not found! "
                "Try clearing Bluetooth cache, or remove the device from OS Bluetooth and retry in -P- mode."
            )

        max_retries = key_max_retries
        entered_programming = False
        last_notify: bytes | None = None
        notify_samples: list[str] = []
        write_failures = 0
        for attempt in range(max_retries):
            resp = response_holder[0]
            if _is_unlock_key_programming_ready(resp):
                _LOGGER.debug("Entered key programming mode after %d attempt(s)", attempt)
                entered_programming = True
                break

            prog_event.clear()
            response_holder[0] = None
            try:
                await self._client.write_gatt_char(
                    UNLOCK_CHARACTERISTIC_UUID, b'\x02' + b'\x00' * 16, response=True
                )
            except Exception as exc:
                write_failures += 1
                _LOGGER.debug("Key programming write attempt %d failed: %s", attempt + 1, exc)

            try:
                await asyncio.wait_for(prog_event.wait(), timeout=_PAIRING_PROG_WAIT_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                pass

            resp = response_holder[0]
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
            try:
                await self._client.stop_notify(UNLOCK_CHARACTERISTIC_UUID)
                await self._client.stop_notify(self._config.rx_channel_uuids[0])
            except Exception:
                pass
            _LOGGER.error(
                "Key programming mode not reached: model=%s aggressive_gatt_timing=%s "
                "unlock_uuid=%s attempts=%s write_failures=%s "
                "expected_notify_first_byte=0x82 last_notify_hex=%s samples=%s",
                self._config.model,
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

        prog_event.clear()
        response_holder[0] = None
        try:
            await self._client.write_gatt_char(
                UNLOCK_CHARACTERISTIC_UUID, b'\x00' + pair_key, response=True
            )
        except Exception as exc:
            _LOGGER.error("Failed to write new key: %s", exc)

        try:
            await asyncio.wait_for(prog_event.wait(), timeout=_PAIRING_KEY_ACK_WAIT_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            pass

        resp = response_holder[0]
        try:
            await self._client.stop_notify(UNLOCK_CHARACTERISTIC_UUID)
            await self._client.stop_notify(self._config.rx_channel_uuids[0])
        except Exception:
            pass

        if not _is_unlock_pairing_key_ack(resp):
            raise ConnectionError(f"Failed to program pairing key. Response: {resp.hex() if resp else 'None'}")

        _LOGGER.debug("Device paired successfully with new key")
        await asyncio.sleep(_PAIRING_SETTLE_DEFAULT_SEC)

    async def subscribe_service_changed(self) -> bool:
        """Subscribe to Service Changed, as the app does when pairing.

        The #67 capture writes 0x0002 to handle 0x000B in both pairing sessions
        and in neither reconnect. It is a CCCD like the vendor ones, and the
        spec has a peripheral keep that configuration per bonded client.

        Best effort: a device without the characteristic, or a backend that
        refuses the subscribe, must not fail the pairing.
        """
        def _on_service_changed(_handle: int, data: bytearray) -> None:
            _LOGGER.debug(
                "Service Changed indication from %s: %s", self.address, _hex(data)
            )

        try:
            await self._client.start_notify(
                SERVICE_CHANGED_UUID, _on_service_changed
            )
        except Exception as exc:
            _LOGGER.debug(
                "Could not subscribe to Service Changed on %s (continuing): %s",
                self.address, exc,
            )
            return False
        _LOGGER.debug("Subscribed to Service Changed on %s", self.address)
        return True

    @traced("pair")
    async def pair(self, key: bytearray | None = None) -> None:
        """Program pairing credentials according to ``host_pairing_mode``."""
        pair_key = key or PAIRING_KEY
        if self._config.host_pairing_mode == HostPairingMode.OS_BONDING:
            if getattr(self._client, "_omron_bonded_at_connect", False):
                # Keyed on what the connect actually did rather than on the
                # profile flag, so that a connect-time pair which fell back
                # still gets its bond here, while one that succeeded is not
                # bonded a second time on the same link -- which would only
                # rotate the keys it just made.
                _LOGGER.debug(
                    "Skipping explicit OS bonding for %s: already bonded during "
                    "connect",
                    self._config.model,
                )
                return
            await self._pair_os_bonding()
            return
        if self._config.host_pairing_mode == HostPairingMode.NONE:
            # Nothing for us to program. The cuff raises its own Security
            # Request and the agent held across the connect answers it; any
            # application-layer credential is established by unlock() instead.
            # A no-op rather than an error so the ordinary setup and retry
            # paths need no special case for these profiles.
            _LOGGER.debug(
                "Skipping host pairing for %s: the device drives security itself",
                self._config.model,
            )
            return
        if self._config.host_pairing_mode != HostPairingMode.CUSTOM_KEY:
            raise ConnectionError("Pairing is not supported for this device")
        if _bluez_device_path(self._bluez_target()) is None:
            # Proxy link: there is no local SMP confirmation to answer, and the
            # agent registers as the system *default*, so putting one up here
            # would take over pairing confirmation for unrelated local devices.
            await self._pair_custom_key(pair_key)
            return
        # Nothing has put an agent up for this link: connect-time bonding and
        # _pair_os_bonding both belong to OS_BONDING profiles, and this is the
        # custom-key path. Cuffs that raise an SMP security request when RX
        # notifications are enabled (HEM-7155T) then drop the link, because
        # BlueZ leaves the Just Works confirmation unanswered without one.
        async with _bluez_pairing_agent():
            await self._pair_custom_key(pair_key)

    async def unpair(self) -> None:
        """Remove the OS-level bond for this device (best-effort).

        Not all backends implement ``BleakClient.unpair``; unsupported ones
        raise ``NotImplementedError``. All failures are swallowed so this
        never breaks the surrounding teardown.
        """
        try:
            await self._client.unpair()
            _LOGGER.debug("Removed OS bond for %s after session", self._config.model)
        except NotImplementedError:
            _LOGGER.debug(
                "unpair() not supported by this BLE backend for %s; "
                "bond (if any) left in place",
                self._config.model,
            )
        except Exception as exc:
            _LOGGER.debug("unpair() failed for %s (ignored): %s", self._config.model, exc)
