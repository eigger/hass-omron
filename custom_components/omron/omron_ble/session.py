"""One connected BLE session to an Omron device: connection lifecycle, pairing and unlock (the memory protocol is owned, from memory_protocol)."""
from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, AsyncIterator

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from blesession import DISCONNECT_TIMEOUT_S, probe_link
from blesession.link import LinkInfo

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
    _NOTIFY_SUBSCRIBE_SETTLE_SEC,
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
from .memory_protocol import MemoryProtocol
from .secure_flow import ASYNC_NOTICE_UUID
from .session_trace import SessionTrace, traced
from .unlock import (
    _UNLOCK_AUTH_WAIT_TIMEOUT_SEC,
    _is_unlock_auth_key_ack,
    _maybe_send_unlock_probe,
    _pair_custom_key,
    _secure_unlock,
    _token_unlock,
)
from .util import _hex

_LOGGER = logging.getLogger(__name__)

# Polling step while waiting for the device to end a session itself.
_PEER_CLOSE_POLL_STEP_SEC: float = 0.25
_OS_BOND_REFRESH_DELAY_SEC: float = 0.3
_OS_BOND_RETRY_DELAY_SEC: float = 0.5

PAIRING_KEY = bytearray.fromhex("deadbeaf12341234deadbeaf12341234")


def _link_scanner_source(raw: Any) -> str | None:
    """A habluetooth scanner id for ``LinkInfo.source``, or None if unknown."""
    if not isinstance(raw, str) or raw == "unknown":
        return None
    return raw




_PROTOCOL_STATE = frozenset({
    "_notify_subscribed",
    "_last_reply_packet_type",
    "_last_reply_memory_address",
    "_last_reply_payload",
    "_last_reply_result_code",
    "_expected_reply_packet_type",
    "_expected_reply_memory_address",
    "_reply_ready",
    "_channel_fragments",
    "_notify_handle_to_channel",
    "_memory_session_active",
    "_pairing_registration_head_done",
    "_pairing_registration_clock_done",
})


class OmronDeviceSession:
    """A connected BLE session to one Omron device.

    Owns the connection lifecycle (use as an ``async with`` context manager, or
    ``connect()`` / ``aclose()``), pairing and unlock. The notify channels,
    the command/reply exchange and the memory session live on ``self.memory``.
    Supports single-channel (OS-bonding) and multi-channel (classic pairing)
    profiles.
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
        self.memory = MemoryProtocol(self)
        self._unlocked = False
        self._secure_session = None
        # Swappable handler for the unlock characteristic notifications. The
        # CCCD is subscribed once (via a fixed dispatcher) so the token
        # handshake and the ECDH secure handshake can run back-to-back on the
        # same subscription — re-subscribing mid-flow either resets the device
        # session or trips the backend's "already enabled" guard.
        self._unlock_notify_handler: Any = None

    def __getattr__(self, name: str) -> Any:
        memory = self.__dict__.get("memory")
        if memory is not None and (
            name in _PROTOCOL_STATE or hasattr(type(memory), name)
        ):
            return getattr(memory, name)
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _PROTOCOL_STATE and "memory" in self.__dict__:
            setattr(self.__dict__["memory"], name, value)
            return
        object.__setattr__(self, name, value)

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
            self.publish_link_to(self.trace)
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
        # Filled in by the connect as it goes, so a connect that fails on
        # every attempt still tells the trace which radio it tried.
        self.link_info = {}
        try:
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
                    link_info=self.link_info,
                )
        finally:
            self.publish_link_to(self.trace)
        return self

    def publish_link_to(self, trace: SessionTrace) -> None:
        """Copy what the connect learned onto ``trace``.

        Safe to call on an already-connected session (early return from
        ``connect``) or when a poll adopts the link and needs the same facts
        on its own trace. Does not overwrite link or facts ``trace`` already
        has.
        """
        if self.trace.link is not None and trace.link is None:
            trace.link = self.trace.link
        info = self.link_info
        source = _link_scanner_source(info.get("source"))
        if trace.link is None and self._client is not None and self._ble_device is not None:
            link = probe_link(self._client, self._ble_device)
            if link.source is None and source is not None:
                link = LinkInfo(via=link.via, source=source, proxy=link.proxy)
            trace.link = link
        if trace.link is None and (info.get("via") or source is not None):
            trace.link = LinkInfo(via=info.get("via"), source=source)
        for key in ("connect_attempts", "bonded_at_connect"):
            value = self.trace.facts.get(key, info.get(key))
            if value is not None and key not in trace.facts:
                trace.note(**{key: value})

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
                        # Bounded: the close runs after the poll deadline has
                        # already fired, so nothing else bounds it, and a proxy
                        # that stopped answering would hang here holding the
                        # per-entry session lock. A timeout is swallowed below
                        # like any other close failure -- the link is dropped
                        # anyway once the proxy comes back.
                        async with asyncio.timeout(DISCONNECT_TIMEOUT_S):
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
        await self.memory._unsubscribe_notify_channels(force=True)
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
        # Only the fresh-session retry loop in async_poll resets between
        # attempts; the handed-off pairing session has no retry and its close
        # failure is reported from aclose() instead. Where a retry does happen
        # the registration is written again, since whether the cuff commits
        # the mirror on the write or on the close is not established -- a
        # transfer count stepped twice beats a registration silently lost.
        self.memory.reset()
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
            await _secure_unlock(self)
            return

        # Stateless token handshake (0x11 / 0x91)
        if self._config.unlock_mode == UnlockMode.TOKEN_KEY:
            await _token_unlock(
                self, keep_notify=self._config.keep_notify_subscriptions
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
            await _maybe_send_unlock_probe(self, unlock_event, response_holder)

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
            await _pair_custom_key(self, pair_key)
            return
        # Nothing has put an agent up for this link: connect-time bonding and
        # _pair_os_bonding both belong to OS_BONDING profiles, and this is the
        # custom-key path. Cuffs that raise an SMP security request when RX
        # notifications are enabled (HEM-7155T) then drop the link, because
        # BlueZ leaves the Just Works confirmation unanswered without one.
        async with _bluez_pairing_agent():
            await _pair_custom_key(self, pair_key)

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
