from __future__ import annotations

import asyncio
import datetime as dt
import logging
import secrets
import traceback
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from .const import MODEL_NUMBER_UUID
from .devices import DeviceConfig, HostPairingMode, UnlockMode

_LOGGER = logging.getLogger(__name__)

# BLE memory-protocol pacing (extra margin for weak RF / busy stacks).
_MEMORY_PROTOCOL_REPLY_TIMEOUT_SEC: float = 3.5
_MEMORY_PROTOCOL_TX_MAX_RETRIES: int = 4
_MEMORY_PROTOCOL_RETRY_BACKOFF_SEC: float = 0.25
_NOTIFY_SUBSCRIBE_SETTLE_SEC: float = 0.75
_NOTIFY_SUBSCRIBE_MAX_RETRIES: int = 3

# Pause inserted between establish_connection() returning and the first GATT
# operation.  On OS-bonding Omron devices (and through the ESP32
# bluetooth_proxy in particular) the L2CAP link comes up before the BLE
# stack finishes:
#   * LL_ENC_REQ / LL_ENC_RSP encryption negotiation using a previously
#     stored LTK,
#   * any service-changed indication processing, and
#   * encryption-required characteristic visibility refresh.
# Issuing GATT operations immediately races this settling and produces
# sporadic "Characteristic not found" or silently-dropped CCCD writes.  An
# explicit short wait gives the stack a stable starting point before the
# follow-up service-cache refresh below.
_POST_CONNECT_BOND_SETTLE_SEC: float = 1.5
_UNLOCK_PROBE_WAIT_TIMEOUT_SEC: float = 2.0
_UNLOCK_AUTH_WAIT_TIMEOUT_SEC: float = 5.0
_PAIRING_SETTLE_AGGRESSIVE_SEC: float = 0.25
_PAIRING_SETTLE_DEFAULT_SEC: float = 1.0
_PAIRING_PROG_WAIT_TIMEOUT_SEC: float = 2.0
_PAIRING_KEY_ACK_WAIT_TIMEOUT_SEC: float = 5.0
_SECURE_HANDSHAKE_WAIT_TIMEOUT_SEC: float = 5.0
_OS_BOND_REFRESH_DELAY_SEC: float = 0.3
_OS_BOND_RETRY_DELAY_SEC: float = 0.5
_PAIR_UNLOCK_ATTEMPTS_AGGRESSIVE: int = 10
_PAIR_UNLOCK_ATTEMPTS_DEFAULT: int = 5

PAIRING_KEY = bytearray.fromhex("deadbeaf12341234deadbeaf12341234")


async def _bleak_refresh_services(client: BleakClient) -> None:
    """Re-run GATT discovery so characteristics appear after connection."""
    gs = getattr(client, "get_services", None)
    if not callable(gs):
        return
    try:
        await gs()
    except Exception as exc:
        _LOGGER.debug("get_services refresh: %s", exc)


async def _bleak_clear_cache(client: BleakClient) -> bool:
    """Drop the backend/proxy GATT service cache so the next discovery is fresh.

    A plain ``get_services()`` refresh can keep returning a stale cached
    service list — notably over an ESP32 (bleak_esphome) proxy — which hides
    the modern-stack ``fe4a`` service even though the device exposes it. This
    forces the cache to be discarded; unsupported backends (no ``clear_cache``)
    are a safe no-op.
    """
    cc = getattr(client, "clear_cache", None)
    if not callable(cc):
        return False
    try:
        await cc()
        return True
    except Exception as exc:
        _LOGGER.debug("clear_cache failed (ignored): %s", exc)
        return False


async def establish_connection_with_bond_settle(
    ble_device: BLEDevice, name: str
) -> BleakClient:
    """Connect, wait for bonding/encryption to settle, then refresh GATT cache.

    Wraps ``bleak_retry_connector.establish_connection`` with a fixed
    :data:`_POST_CONNECT_BOND_SETTLE_SEC` pause plus a service-cache refresh.
    All Omron integration code paths should use this helper instead of
    calling ``establish_connection`` directly so the bond-settle behaviour
    is consistent.
    """
    client = await establish_connection(BleakClient, ble_device, name)
    _LOGGER.debug(
        "BLE link established to %s; settling %.1fs for bonding/encryption "
        "before first GATT op",
        name,
        _POST_CONNECT_BOND_SETTLE_SEC,
    )
    await asyncio.sleep(_POST_CONNECT_BOND_SETTLE_SEC)
    await _bleak_refresh_services(client)
    return client


def _hex(data: bytes | bytearray) -> str:
    """Convert byte array to hex string."""
    return bytes(data).hex()


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


async def _bluez_agent_pair(client: BleakClient) -> bool:
    """Pair via BlueZ DBus with a registered KeyboardDisplay agent.

    On BlueZ 5.72+ (Linux), calling ``Device1.Pair()`` without a registered
    agent causes the Just Works confirmation to go unanswered, producing
    ``AuthenticationFailed`` even though the device supports the pairing
    method.  Registering a ``KeyboardDisplay`` agent that auto-confirms
    passkeys resolves this for OS-bonding-only devices like the HEM-716BT2.

    Uses ``dbus-fast`` (already a bleak dependency) so no extra packages
    are needed.  Silently returns False on non-Linux platforms or when the
    BlueZ DBus path is unavailable.
    """
    try:
        from dbus_fast.aio.message_bus import MessageBus
        from dbus_fast.constants import BusType, MessageType
        from dbus_fast.message import Message
        from dbus_fast.service import ServiceInterface, method as dbus_method
    except ImportError:
        return False

    # BlueZ device path is in the BLEDevice details on Linux/BlueZ backends.
    details = getattr(getattr(client, "_device", None), "details", None) or {}
    device_path: str | None = details.get("path")
    if not device_path:
        # Try bleak's internal _backend attribute for older versions.
        backend = getattr(client, "_backend", None)
        device_path = getattr(getattr(backend, "_device", None), "path", None)
    if not device_path:
        return False

    _AGENT_PATH = "/omron/ble/pairagent"

    class _AutoConfirmAgent(ServiceInterface):
        """Minimal BlueZ pairing agent that auto-accepts Just Works."""

        def __init__(self) -> None:
            super().__init__("org.bluez.Agent1")

        @dbus_method()
        def Release(self) -> None:  # type: ignore[override]
            pass

        @dbus_method()
        def RequestConfirmation(self, device: "o", passkey: "u") -> None:  # type: ignore[override]
            _LOGGER.debug("BlueZ agent: auto-confirming passkey %06d", passkey)

        @dbus_method()
        def RequestPasskey(self, device: "o") -> "u":  # type: ignore[override]
            return 0

        @dbus_method()
        def RequestAuthorization(self, device: "o") -> None:  # type: ignore[override]
            pass

        @dbus_method()
        def Cancel(self) -> None:  # type: ignore[override]
            pass

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as exc:
        _LOGGER.debug("BlueZ agent pair: cannot connect to system bus: %s", exc)
        return False

    agent = _AutoConfirmAgent()
    try:
        bus.export(_AGENT_PATH, agent)

        await bus.call(
            Message(
                destination="org.bluez",
                path="/org/bluez",
                interface="org.bluez.AgentManager1",
                member="RegisterAgent",
                signature="os",
                body=[_AGENT_PATH, "KeyboardDisplay"],
            )
        )
        await bus.call(
            Message(
                destination="org.bluez",
                path="/org/bluez",
                interface="org.bluez.AgentManager1",
                member="RequestDefaultAgent",
                signature="o",
                body=[_AGENT_PATH],
            )
        )
        _LOGGER.debug("BlueZ KeyboardDisplay agent registered; calling Pair()")

        reply = await bus.call(
            Message(
                destination="org.bluez",
                path=device_path,
                interface="org.bluez.Device1",
                member="Pair",
            )
        )
        success = reply.message_type == MessageType.METHOD_RETURN
        if success:
            _LOGGER.debug("BlueZ agent pair succeeded for %s", device_path)
        else:
            _LOGGER.debug(
                "BlueZ agent pair returned non-success for %s: %s",
                device_path,
                reply,
            )
        return success
    except Exception as exc:
        _LOGGER.debug("BlueZ agent pair failed for %s: %s", device_path, exc)
        return False
    finally:
        try:
            await bus.call(
                Message(
                    destination="org.bluez",
                    path="/org/bluez",
                    interface="org.bluez.AgentManager1",
                    member="UnregisterAgent",
                    signature="o",
                    body=[_AGENT_PATH],
                )
            )
        except Exception:
            pass
        bus.disconnect()


async def _bluez_remove_device(client: BleakClient) -> bool:
    """Remove the BlueZ device and its bond via ``Adapter1.RemoveDevice``.

    BlueZ persists bonds under ``/var/lib/bluetooth`` across reboots, so a
    stale/rotated bond cannot be cleared by reconnecting or rebooting — it
    must be explicitly removed.  ``BleakClient.unpair()`` is not implemented
    on every backend (notably the HA/habluetooth BlueZ wrapper), so go
    straight to BlueZ over DBus (same transport used by the pairing agent).

    Returns True if RemoveDevice succeeded; False on any failure or when the
    DBus path is unavailable (caller should fall back to ``unpair()``).
    """
    try:
        from dbus_fast.aio.message_bus import MessageBus
        from dbus_fast.constants import BusType, MessageType
        from dbus_fast.message import Message
    except ImportError:
        return False

    details = getattr(getattr(client, "_device", None), "details", None) or {}
    device_path: str | None = details.get("path")
    if not device_path:
        backend = getattr(client, "_backend", None)
        device_path = getattr(getattr(backend, "_device", None), "path", None)
    if not device_path or "/dev_" not in device_path:
        return False
    # Adapter path is the device path minus the trailing dev_XX_.. segment.
    adapter_path = device_path.rsplit("/", 1)[0]

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as exc:
        _LOGGER.debug("BlueZ RemoveDevice: cannot connect to system bus: %s", exc)
        return False
    try:
        reply = await bus.call(
            Message(
                destination="org.bluez",
                path=adapter_path,
                interface="org.bluez.Adapter1",
                member="RemoveDevice",
                signature="o",
                body=[device_path],
            )
        )
        ok = reply.message_type == MessageType.METHOD_RETURN
        _LOGGER.debug(
            "BlueZ RemoveDevice(%s) -> %s", device_path, "ok" if ok else reply
        )
        return ok
    except Exception as exc:
        _LOGGER.debug("BlueZ RemoveDevice failed for %s: %s", device_path, exc)
        return False
    finally:
        bus.disconnect()


def _is_non_fatal_os_pairing_error(exc: BaseException) -> bool:
    """Whether an OS-level BLE pairing exception can be safely ignored.

    Modern-stack Omron devices (pairing=false in ubpm) do not require an
    explicit pair() call; the BLE stack negotiates security automatically
    when GATT operations are performed.  Therefore most pair() errors on
    these devices are non-fatal and should not block the config flow.
    """
    msg = str(exc).lower()
    non_fatal_markers = (
        "alreadyexists",
        "already exists",
        "already paired",
        "already bonded",
        "authentication canceled",
        "authenticationcanceled",
        "authentication cancelled",
        "authenticationcancelled",
        "authenticationfailed",
        "authentication failed",
        "authenticationrejected",
        "authentication rejected",
        "notready",
        "not ready",
        "in progress",
    )
    return any(marker in msg for marker in non_fatal_markers)


def _is_stale_bond_auth_error(exc: BaseException) -> bool:
    """Whether a bonding failure looks like a stale/rotated bond.

    Some Omron AFib devices (notably the HEM-7380T1 family) do not retain
    their pairing bond across disconnects — they rotate or discard the LTK
    after each session.  The host's stored bond then no longer matches, so
    BlueZ rejects re-encryption/re-pairing with ``AuthenticationFailed`` (the
    link never gets encrypted and later GATT ops fail with ``NotConnected``).
    This is the signature of "works once right after a fresh bond, then fails
    on every subsequent connection"; removing the stale bond and re-pairing
    from a clean slate recovers the device.
    """
    msg = str(exc).lower()
    return any(
        marker in msg
        for marker in (
            "authenticationfailed",
            "authentication failed",
            "authenticationrejected",
            "authentication rejected",
        )
    )


def _log_pairing_failure_detail(prefix: str, exc: BaseException) -> None:
    """Emit structured detail for BLE bonding/pairing failures."""
    lines = [
        prefix,
        f"  type: {type(exc).__module__}.{type(exc).__name__}",
        f"  str: {exc!s}",
        f"  repr: {exc!r}",
    ]
    dbus_error = getattr(exc, "dbus_error", None)
    if dbus_error is not None:
        lines.append(f"  dbus_error: {dbus_error!s}")
    for attr in ("dbus_path", "name", "details", "reply", "error_name", "error_message"):
        val = getattr(exc, attr, None)
        if val is not None:
            lines.append(f"  {attr}: {val!r}")

    cause = exc.__cause__
    depth = 0
    while cause is not None and depth < 8:
        lines.append(
            f"  __cause__[{depth}]: "
            f"{type(cause).__module__}.{type(cause).__name__}: {cause!s}"
        )
        cause = cause.__cause__
        depth += 1

    _LOGGER.error("\n".join(lines))
    tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    _LOGGER.debug("%s (full traceback)\n%s", prefix, "".join(tb_lines))


class OmronDeviceSession:
    """A connected BLE session to one Omron device.

    Owns the connection lifecycle (use as an ``async with`` context manager, or
    ``connect()`` / ``aclose()``) together with all GATT read/write, notify,
    pairing, unlock and memory-session operations. Supports single-channel
    (OS-bonding) and multi-channel (classic pairing) profiles.
    """

    def __init__(self, ble_device: BLEDevice, device_config: DeviceConfig) -> None:
        self._ble_device = ble_device
        self._config = device_config
        self._init_session_state(client=None, owns_connection=True)

    def _init_session_state(
        self, *, client: BleakClient | None, owns_connection: bool
    ) -> None:
        self._client = client
        self._owns_connection = owns_connection
        self._notify_subscribed = False
        self._last_reply_packet_type: bytes | None = None
        self._last_reply_memory_address: bytes | None = None
        self._last_reply_payload: bytes | None = None
        self._reply_ready = asyncio.Event()
        self._channel_fragments: list[bytes | None] = [None] * 4
        self._notify_handle_to_channel: dict[int, int] = {}
        self._memory_session_active = False
        self._unlocked = False
        self._secure_session = None

    # -- connection lifecycle -------------------------------------------------

    @classmethod
    def adopt(
        cls, client: BleakClient, device_config: DeviceConfig
    ) -> "OmronDeviceSession":
        """Wrap an already-open client to run ops over a connection owned elsewhere.

        ``aclose()`` will not disconnect an adopted client.
        """
        session = cls.__new__(cls)
        session._ble_device = getattr(client, "_device", None)
        session._config = device_config
        session._init_session_state(client=client, owns_connection=False)
        return session

    @property
    def client(self) -> BleakClient:
        """Return the live Bleak client, raising if the session is not connected."""
        if self._client is None:
            raise ConnectionError("OmronDeviceSession is not connected")
        return self._client

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
        self._client = await establish_connection_with_bond_settle(
            self._ble_device, self.address
        )
        return self

    async def refresh_services(self) -> None:
        """Re-run GATT discovery so characteristics appear after connection."""
        await _bleak_refresh_services(self.client)

    async def verify_parent_service(self) -> bool:
        """Ensure the profile's parent service is present, refreshing if needed.

        Repeating a plain ``get_services()`` is pointless once it has returned a
        list without the parent service — it just re-reads the same cached list.
        So the sequence keeps only the *meaningful* steps:

        1. check the current services,
        2. refresh once (the cache may simply be unpopulated right after connect),
        3. if still missing, force a fresh discovery via ``clear_cache()`` (the
           only move different from step 2 — it drops a stale backend/proxy cache
           that can hide ``fe4a``). Bail out immediately if the backend cannot
           clear its cache, rather than looping uselessly.
        """
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
                except Exception:
                    pass
            if self._owns_connection and client.is_connected:
                await client.disconnect()
                disconnected = True
        except Exception:
            pass
        finally:
            self._client = None
            if disconnected:
                _LOGGER.debug("BLE link closed for %s", addr)

    async def __aenter__(self) -> "OmronDeviceSession":
        return await self.connect()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

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

    def _rebuild_notify_handle_index_map(self) -> None:
        """Build mapping from GATT characteristic handles to notify channel indices."""
        self._notify_handle_to_channel = {}
        for idx, uuid in enumerate(self._config.rx_channel_uuids):
            char = self._client.services.get_characteristic(uuid)
            if char is not None:
                self._notify_handle_to_channel[char.handle] = idx

    async def _subscribe_notify_channels(self) -> None:
        """Enable notifications on all RX channels."""
        if self._notify_subscribed:
            _LOGGER.debug(
                "RX notify subscribe skipped (already flagged) model=%s",
                self._config.model,
            )
            return

        self._debug_ble_link("before_rx_subscribe")
        await self._ensure_services_cache()
        self._rebuild_notify_handle_index_map()

        for uuid in self._config.rx_channel_uuids:
            await self._start_notify_with_recovery(uuid)
        await asyncio.sleep(_NOTIFY_SUBSCRIBE_SETTLE_SEC)
        self._notify_subscribed = True
        self._debug_ble_link("after_rx_subscribe")

    async def _start_notify_with_recovery(self, uuid: str) -> None:
        """Start notify with recovery for transient BlueZ/stack races."""
        last_exc: BaseException | None = None
        for attempt in range(_NOTIFY_SUBSCRIBE_MAX_RETRIES):
            try:
                await self._client.start_notify(uuid, self._on_notify_channel_data)
                return
            except BleakError as exc:
                last_exc = exc
                msg = str(exc).lower()
                # BlueZ can keep CCCD/notify acquired briefly after reconnect.
                # Try to release stale state and re-subscribe.
                if "notify acquired" in msg or "notpermitted" in msg:
                    _LOGGER.debug(
                        "start_notify recovery (%d/%d) for %s on %s: %s",
                        attempt + 1,
                        _NOTIFY_SUBSCRIBE_MAX_RETRIES,
                        uuid,
                        self._config.model,
                        exc,
                    )
                    try:
                        await self._client.stop_notify(uuid)
                    except Exception:
                        pass
                    await _bleak_refresh_services(self._client)
                    if attempt + 1 < _NOTIFY_SUBSCRIBE_MAX_RETRIES:
                        await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                if "service discovery has not been performed" in msg or "not been performed" in msg:
                    await _bleak_refresh_services(self._client)
                    if attempt + 1 < _NOTIFY_SUBSCRIBE_MAX_RETRIES:
                        await asyncio.sleep(0.2)
                    continue
                raise
            except Exception as exc:
                last_exc = exc
                if attempt + 1 < _NOTIFY_SUBSCRIBE_MAX_RETRIES:
                    await asyncio.sleep(0.2)
                    continue
                raise
        if last_exc is not None:
            raise last_exc

    async def _unsubscribe_notify_channels(self) -> None:
        """Disable notifications on all RX channels."""
        for uuid in self._config.rx_channel_uuids:
            try:
                await self._client.stop_notify(uuid)
            except Exception as exc:
                _LOGGER.debug("stop_notify for %s ignored: %s", uuid, exc)
        self._notify_subscribed = False
        self._debug_ble_link("after_rx_unsubscribe")

    async def reset_session_state(self) -> None:
        """Release any stale BLE notify subscriptions and reset session flags.

        Call this before retrying ``open_memory_session`` when a previous attempt
        failed with a BlueZ ``Notify acquired`` error.  The stop_notify calls are
        best-effort; failures are silently ignored so the caller can proceed with
        the next attempt regardless.
        """
        await self._unsubscribe_notify_channels()
        self._unlocked = False
        self._secure_session = None
        self._memory_session_active = False
        self._channel_fragments = [None] * 4
        self._reply_ready.clear()
        self._debug_ble_link("reset_session_state")

    def _on_notify_channel_data(self, char: Any, rx_bytes: bytearray) -> None:
        """Callback for received BLE notifications. Reassembles multi-channel packets."""
        # Determine which channel this notification came from
        if self._config.is_single_channel:
            channel_index = 0
        elif isinstance(char, int):
            channel_index = self._notify_handle_to_channel.get(char, -1)
        else:
            # Try UUID-based mapping first, then handle-based
            if char.uuid in self._config.rx_channel_uuids:
                channel_index = self._config.rx_channel_uuids.index(char.uuid)
            else:
                channel_index = self._notify_handle_to_channel.get(char.handle, -1)

        if channel_index < 0:
            _LOGGER.warning("Received data on unknown handle/uuid: %s", char)
            return

        self._channel_fragments[channel_index] = rx_bytes

        # Check if we can assemble a complete packet
        if not self._channel_fragments[0]:
            return

        if self._config.is_single_channel:
            frame_bytes = bytearray(self._channel_fragments[0])
            self._channel_fragments = [None] * 4
        else:
            packet_size = self._channel_fragments[0][0]
            required_channels = range((packet_size + 15) // 16)
            # Check all required channels are received
            for ch in required_channels:
                if self._channel_fragments[ch] is None:
                    return
            # Combine channels
            frame_bytes = bytearray()
            for ch in required_channels:
                frame_bytes += self._channel_fragments[ch]
            frame_bytes = frame_bytes[:packet_size]
            self._channel_fragments = [None] * 4

        # Decrypt if secure-session encryption is active
        if self._config.unlock_mode == UnlockMode.SECURE_SESSION and self._secure_session is not None:
            try:
                frame_bytes = bytearray(self._secure_session.decrypt(bytes(frame_bytes)))
            except Exception as exc:
                _LOGGER.error("Secure session decryption failed: %s", exc)
                return
        else:
            # Verify XOR CRC
            xor_crc = 0
            for byte in frame_bytes:
                xor_crc ^= byte
            if xor_crc:
                _LOGGER.error(
                    "CRC error in rx data: crc=%d, buffer=%s", xor_crc, _hex(frame_bytes)
                )
                return

        # Extract packet fields
        self._last_reply_packet_type = frame_bytes[1:3]
        self._last_reply_memory_address = frame_bytes[3:5]
        expected_data_len = frame_bytes[5]
        if expected_data_len > (len(frame_bytes) - 8):
            self._last_reply_payload = bytes(b'\xff') * expected_data_len
        else:
            if self._last_reply_packet_type == bytearray.fromhex("8f00"):
                # End-of-transmission packet: error code is in byte 6
                self._last_reply_payload = frame_bytes[6:7]
            else:
                self._last_reply_payload = frame_bytes[6:6 + expected_data_len]

        self._reply_ready.set()

    async def _write_command_and_wait_reply(
        self,
        command: bytearray,
        timeout: float = _MEMORY_PROTOCOL_REPLY_TIMEOUT_SEC,
    ) -> None:
        """Send a command and wait for response with retry logic."""
        if self._config.unlock_mode == UnlockMode.SECURE_SESSION and self._secure_session is not None:
            try:
                command = bytearray(self._secure_session.encrypt(bytes(command)))
            except Exception as exc:
                _LOGGER.error("Secure session encryption failed for command: %s", exc)
                raise

        max_retries = _MEMORY_PROTOCOL_TX_MAX_RETRIES
        for retry in range(max_retries):
            self._reply_ready.clear()

            # Split command across TX channels
            remaining_cmd = command
            channel_width = 16
            if self._config.is_single_channel:
                channel_width = max(channel_width, len(command))

            num_tx_channels = (len(command) + channel_width - 1) // channel_width
            try:
                for ch_idx in range(num_tx_channels):
                    tx_segment = remaining_cmd[:channel_width]
                    if self._config.is_single_channel:
                        await self._client.write_gatt_char(
                            self._config.tx_channel_uuids[ch_idx], tx_segment, response=False
                        )
                    else:
                        await self._client.write_gatt_char(
                            self._config.tx_channel_uuids[ch_idx], tx_segment
                        )
                    remaining_cmd = remaining_cmd[channel_width:]
            except BleakError as exc:
                msg = str(exc).lower()
                # Refresh the GATT cache when either:
                #   1. Bleak reports services were never discovered, or
                #   2. The TX characteristic UUID is not in the local cache
                #      (typical right after OS bonding completes — the peer
                #      newly exposes encryption-required characteristics that
                #      weren't visible in the pre-bond enumeration).
                stale_cache = (
                    "service discovery has not been performed" in msg
                    or "was not found" in msg
                )
                if stale_cache:
                    _LOGGER.debug(
                        "GATT cache stale during write (retry %d/%d), refreshing: %s",
                        retry + 1,
                        max_retries,
                        exc,
                    )
                    try:
                        await asyncio.sleep(_MEMORY_PROTOCOL_REPLY_TIMEOUT_SEC)
                        await _bleak_refresh_services(self._client)
                    except Exception as refresh_exc:
                        _LOGGER.debug(
                            "Service refresh during write retry failed (continuing): %s",
                            refresh_exc,
                        )
                else:
                    _LOGGER.warning(
                        "BLE error during write (retry %d/%d): %s",
                        retry + 1,
                        max_retries,
                        exc,
                    )
                if retry + 1 >= max_retries:
                    raise
                continue

            # Wait for response
            try:
                self._debug_ble_link(
                    f"await_reply attempt={retry + 1} cmd_head={_hex(command[:8])}"
                )
                await asyncio.wait_for(self._reply_ready.wait(), timeout=timeout)
                return  # Success
            except asyncio.TimeoutError:
                _LOGGER.warning("TX timeout, retry %d/%d", retry + 1, max_retries)
                self._debug_ble_link(
                    f"reply_timeout attempt={retry + 1} cmd_head={_hex(command[:8])}"
                )
                try:
                    if not self._client.is_connected:
                        raise ConnectionError(
                            "BLE disconnected while waiting for a memory-protocol reply "
                            "(no assembled RX within timeout); retry when the link is stable"
                        )
                except ConnectionError:
                    raise
                except Exception:
                    pass
                if retry + 1 < max_retries:
                    await asyncio.sleep(_MEMORY_PROTOCOL_RETRY_BACKOFF_SEC)

        raise ConnectionError(
            f"Failed to receive response after {max_retries} retries"
        )

    @property
    def memory_session_active(self) -> bool:
        """True while the EEPROM readout GATT session is open."""
        return self._memory_session_active

    @asynccontextmanager
    async def memory_session(self) -> AsyncIterator[None]:
        """Hold one EEPROM readout session (idempotent if already open)."""
        await self.open_memory_session()
        try:
            yield
        finally:
            await self.close_memory_session()

    @asynccontextmanager
    async def memory_session_after_unlock(
        self, *, pair_first: bool = False
    ) -> AsyncIterator[None]:
        """Unlock (optional pair) then hold one memory readout session."""
        if pair_first:
            try:
                await self.pair()
            except Exception as exc:
                _LOGGER.debug(
                    "Poll pair step failed (continuing to unlock): %s", exc
                )
        await self.unlock()
        async with self.memory_session():
            yield

    async def open_memory_session(self) -> None:
        """Start a data readout session (no-op if already open)."""
        if self._memory_session_active:
            return

        try:
            self._require_connected("open_memory_session")
            self._debug_ble_link("open_memory_session_enter")
            await self._subscribe_notify_channels()
            # Universal init command (ubpm cmd_init): byte[5]=0x10 for all devices.
            start_cmd = bytearray.fromhex("0800000000100018")
            await self._write_command_and_wait_reply(start_cmd)
            if self._last_reply_packet_type != bytearray.fromhex("8000"):
                raise ConnectionError("Invalid response to data readout start")
            self._memory_session_active = True
            self._debug_ble_link("open_memory_session_ok")
            _LOGGER.debug("Memory session opened for %s", self.address)
        except BaseException:
            self._memory_session_active = False
            self._unlocked = False
            self._debug_ble_link("open_memory_session_fail_cleanup")
            await self._unsubscribe_notify_channels()
            raise

    async def close_memory_session(self) -> None:
        """End a data readout session (no-op if not open)."""
        if not self._memory_session_active:
            return

        try:
            stop_cmd = bytearray.fromhex("080f000000000007")
            await self._write_command_and_wait_reply(stop_cmd)
            if self._last_reply_packet_type != bytearray.fromhex("8f00"):
                _LOGGER.warning("Invalid response to data readout end")
            elif self._last_reply_payload and self._last_reply_payload[0]:
                _LOGGER.warning(
                    "Device reported error code %d during session close",
                    self._last_reply_payload[0],
                )
        finally:
            self._memory_session_active = False
            await self._unsubscribe_notify_channels()
            _LOGGER.debug("Memory session closed for %s", self.address)

    async def read_memory_block(self, address: int, blocksize: int) -> bytes:
        """Read a block of data from device EEPROM."""
        cmd = bytearray.fromhex("080100")
        cmd += address.to_bytes(2, "big")
        cmd += blocksize.to_bytes(1, "big")
        # Calculate XOR CRC
        xor_crc = 0
        for byte in cmd:
            xor_crc ^= byte
        cmd += b'\x00'
        cmd.append(xor_crc)

        await self._write_command_and_wait_reply(cmd)
        if self._last_reply_memory_address != address.to_bytes(2, "big"):
            raise ConnectionError(
                f"Address mismatch: got {self._last_reply_memory_address}, expected {address:#06x}"
            )
        if self._last_reply_packet_type != bytearray.fromhex("8100"):
            raise ConnectionError("Invalid packet type in EEPROM read")
        return self._last_reply_payload

    async def write_memory_block(self, address: int, data: bytearray) -> None:
        """Write a block of data to device EEPROM."""
        cmd = bytearray()
        cmd += (len(data) + 8).to_bytes(1, "big")
        cmd += bytearray.fromhex("01c0")
        cmd += address.to_bytes(2, "big")
        cmd += len(data).to_bytes(1, "big")
        cmd += data
        # Calculate XOR CRC
        xor_crc = 0
        for byte in cmd:
            xor_crc ^= byte
        cmd += b'\x00'
        cmd.append(xor_crc)

        await self._write_command_and_wait_reply(cmd)
        if self._last_reply_memory_address != address.to_bytes(2, "big"):
            raise ConnectionError(
                f"Address mismatch in write: got {self._last_reply_memory_address}, expected {address:#06x}"
            )
        if self._last_reply_packet_type != bytearray.fromhex("81c0"):
            raise ConnectionError("Invalid packet type in EEPROM write")

    async def read_memory_range(
        self, start_address: int, bytes_to_read: int, block_size: int = 0x10
    ) -> bytearray:
        """Read a continuous range from EEPROM in blocks."""
        result = bytearray()
        while bytes_to_read > 0:
            chunk_size = min(bytes_to_read, block_size)
            result += await self.read_memory_block(start_address, chunk_size)
            start_address += chunk_size
            bytes_to_read -= chunk_size
        return result

    async def write_memory_range(
        self, start_address: int, data: bytearray, block_size: int = 0x08
    ) -> None:
        """Write continuous data to EEPROM in blocks."""
        while len(data) > 0:
            chunk_size = min(len(data), block_size)
            await self.write_memory_block(start_address, data[:chunk_size])
            data = data[chunk_size:]
            start_address += chunk_size

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
                self._config.unlock_uuid, b'\x02' + b'\x00' * 16, response=True
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
            await self._token_unlock()
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
        await self._client.start_notify(self._config.unlock_uuid, _unlock_callback)
        await asyncio.sleep(_NOTIFY_SUBSCRIBE_SETTLE_SEC)
        try:
            # Some classic custom-key models are more stable with a 0x02 probe before auth-key unlock.
            await self._maybe_send_unlock_probe(unlock_event, response_holder)

            unlock_event.clear()
            response_holder[0] = None
            await self._client.write_gatt_char(
                self._config.unlock_uuid, b'\x01' + unlock_key, response=True
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
            await self._client.stop_notify(self._config.unlock_uuid)
            if rx_notify_primed:
                try:
                    await self._client.stop_notify(self._config.rx_channel_uuids[0])
                except Exception as exc:
                    _LOGGER.debug("unlock RX pre-notify stop skipped: %s", exc)
            self._debug_ble_link("unlock_after_stop_notify")

    async def _token_unlock(self) -> None:
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

        await self._ensure_services_cache()

        # Official app: RX notify CCCD (h=33) before unlock CCCD (h=28).
        try:
            await self._client.start_notify(
                self._config.rx_channel_uuids[0], lambda _h, _d: None
            )
            rx_notify_primed = True
            await asyncio.sleep(_NOTIFY_SUBSCRIBE_SETTLE_SEC)
        except Exception as exc:
            _LOGGER.debug("token unlock RX pre-notify prime skipped: %s", exc)

        self._debug_ble_link("token_unlock_before_notify")
        await self._client.start_notify(self._config.unlock_uuid, _token_callback)
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
                    self._config.unlock_uuid, packet, response=use_response
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
            try:
                await self._client.stop_notify(self._config.unlock_uuid)
            except Exception as exc:
                _LOGGER.debug("token unlock stop_notify skipped: %s", exc)
            if rx_notify_primed:
                try:
                    await self._client.stop_notify(self._config.rx_channel_uuids[0])
                except Exception as exc:
                    _LOGGER.debug("token unlock RX pre-notify stop skipped: %s", exc)
            self._debug_ble_link("token_unlock_after_stop_notify")

    async def _secure_unlock(self) -> None:
        """Perform encrypted secure handshake to unlock the device."""
        _LOGGER.debug("Starting secure handshake unlock for model=%s", self._config.model)
        from .secure_session import SecureSession

        self._secure_session = SecureSession()
        unlock_event = asyncio.Event()
        response_holder: list[bytes | None] = [None]

        def _secure_callback(_: Any, rx_bytes: bytearray) -> None:
            response_holder[0] = bytes(rx_bytes)
            unlock_event.set()

        # Subscribe to unlock characteristic notifications
        await self._client.start_notify(self._config.unlock_uuid, _secure_callback)
        await asyncio.sleep(_NOTIFY_SUBSCRIBE_SETTLE_SEC)

        try:
            # Step 1: Send Pairing Request
            pair_req = self._secure_session.build_pair_req()
            _LOGGER.debug("Sending Pairing Request (len=%d): %s", len(pair_req), pair_req.hex())
            unlock_event.clear()
            response_holder[0] = None
            await self._client.write_gatt_char(self._config.unlock_uuid, pair_req, response=True)
            
            # Wait for Pairing Response
            await asyncio.wait_for(unlock_event.wait(), timeout=_SECURE_HANDSHAKE_WAIT_TIMEOUT_SEC)
            pair_resp = response_holder[0]
            _LOGGER.debug("Received Pairing Response (len=%d): %s", len(pair_resp) if pair_resp else 0, pair_resp.hex() if pair_resp else "None")
            if not pair_resp or len(pair_resp) < 2:
                raise ConnectionError("Invalid or empty pairing response")
            
            # Process Pairing Response and derive LTK
            self._secure_session.process_pair_resp(pair_resp)
            _LOGGER.debug("Key exchange complete")

            # Step 2: Send Encryption Start Request
            start_enc_req = self._secure_session.build_start_enc_req()
            _LOGGER.debug("Sending Encryption Start Request (len=%d): %s", len(start_enc_req), start_enc_req.hex())
            unlock_event.clear()
            response_holder[0] = None
            await self._client.write_gatt_char(self._config.unlock_uuid, start_enc_req, response=True)

            # Wait for Encryption Response
            await asyncio.wait_for(unlock_event.wait(), timeout=_SECURE_HANDSHAKE_WAIT_TIMEOUT_SEC)
            enc_resp = response_holder[0]
            _LOGGER.debug("Received Encryption Response (len=%d): %s", len(enc_resp) if enc_resp else 0, enc_resp.hex() if enc_resp else "None")
            if not enc_resp or len(enc_resp) < 2:
                raise ConnectionError("Invalid or empty encryption response")

            # Step 3: Challenge-Response mutual authentication
            challenge_req = self._secure_session.build_challenge_req(enc_resp)
            _LOGGER.debug("Sending Challenge Request (len=%d): %s", len(challenge_req), challenge_req.hex())
            unlock_event.clear()
            response_holder[0] = None
            await self._client.write_gatt_char(self._config.unlock_uuid, challenge_req, response=True)

            # Wait for Challenge Response
            await asyncio.wait_for(unlock_event.wait(), timeout=_SECURE_HANDSHAKE_WAIT_TIMEOUT_SEC)
            challenge_resp = response_holder[0]
            _LOGGER.debug("Received Challenge Response (len=%d): %s", len(challenge_resp) if challenge_resp else 0, challenge_resp.hex() if challenge_resp else "None")
            if not challenge_resp or len(challenge_resp) < 2:
                raise ConnectionError("Invalid or empty challenge response")

            # Finalize: verify peer's challenge response
            self._secure_session.process_challenge_resp(challenge_resp)
            _LOGGER.info("Secure handshake succeeded. Session unlocked.")
            self._unlocked = True

        except asyncio.TimeoutError as exc:
            _LOGGER.error("Secure handshake timed out during negotiation")
            raise ConnectionError("Secure unlock timeout") from exc
        except Exception as exc:
            _LOGGER.error("Secure handshake failed: %s", exc)
            raise ConnectionError(f"Secure unlock failed: {exc}") from exc
        finally:
            await self._client.stop_notify(self._config.unlock_uuid)

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
                agent_paired = await _bluez_agent_pair(self._client)
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
                # Stale/rotated bond (HEM-7380T1 AFib family): the device
                # discarded its LTK, so the stored bond no longer matches and
                # BlueZ raises AuthenticationFailed.  Treating this as
                # "non-fatal" and continuing leaves the link unencrypted, so the
                # session then dies with NotConnected.  Instead remove the stale
                # bond once; RemoveDevice also drops the link, so re-pairing
                # in-place is impossible — raise and let the next connection
                # re-pair from a clean slate (self-heals on the following poll).
                if _is_stale_bond_auth_error(exc) and not stale_bond_cleared:
                    stale_bond_cleared = True
                    _LOGGER.warning(
                        "OS-level bonding rejected (%s) for %s — removing stale "
                        "bond so the next connection can re-pair cleanly",
                        type(exc).__name__,
                        self._config.model,
                    )
                    # Prefer BlueZ RemoveDevice over DBus (authoritative, works
                    # even when the backend's unpair() is a no-op); fall back to
                    # BleakClient.unpair() for non-BlueZ backends.
                    if not await _bluez_remove_device(self._client):
                        await self.unpair()
                    raise ConnectionError(
                        "Stale BLE bond removed after AuthenticationFailed; "
                        "retry the poll to re-pair"
                    ) from exc
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

        _LOGGER.debug("Enabling RX notification to trigger BLE pairing")
        try:
            await self._client.start_notify(
                self._config.rx_channel_uuids[0], lambda h, d: None
            )
        except Exception as exc:
            _LOGGER.debug("Ignored error starting RX notify: %s", exc)

        await self._apply_pairing_settle_delay(aggressive_timing)

        prog_event = asyncio.Event()
        response_holder: list[bytes | None] = [None]

        def _pair_callback(_: Any, rx_bytes: bytearray) -> None:
            response_holder[0] = rx_bytes
            prog_event.set()

        unlock_subscribed = False
        for attempt in range(unlock_attempts):
            try:
                await self._client.start_notify(self._config.unlock_uuid, _pair_callback)
                unlock_subscribed = True
                break
            except Exception as exc:
                _LOGGER.debug(
                    "Unlock characteristic not ready (%s/%s): %s",
                    attempt + 1,
                    unlock_attempts,
                    exc,
                )
                if aggressive_timing:
                    await _bleak_refresh_services(self._client)
                await asyncio.sleep(unlock_retry_delay)
        if not unlock_subscribed:
            raise ConnectionError(
                f"Characteristic {self._config.unlock_uuid} was not found! "
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
                    self._config.unlock_uuid, b'\x02' + b'\x00' * 16, response=True
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
                await self._client.stop_notify(self._config.unlock_uuid)
                await self._client.stop_notify(self._config.rx_channel_uuids[0])
            except Exception:
                pass
            _LOGGER.error(
                "Key programming mode not reached: model=%s aggressive_gatt_timing=%s "
                "unlock_uuid=%s attempts=%s write_failures=%s "
                "expected_notify_first_byte=0x82 last_notify_hex=%s samples=%s",
                self._config.model,
                aggressive_timing,
                self._config.unlock_uuid,
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
                self._config.unlock_uuid, b'\x00' + pair_key, response=True
            )
        except Exception as exc:
            _LOGGER.error("Failed to write new key: %s", exc)

        try:
            await asyncio.wait_for(prog_event.wait(), timeout=_PAIRING_KEY_ACK_WAIT_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            pass

        resp = response_holder[0]
        try:
            await self._client.stop_notify(self._config.unlock_uuid)
            await self._client.stop_notify(self._config.rx_channel_uuids[0])
        except Exception:
            pass

        if not _is_unlock_pairing_key_ack(resp):
            raise ConnectionError(f"Failed to program pairing key. Response: {resp.hex() if resp else 'None'}")

        _LOGGER.debug("Device paired successfully with new key")
        await asyncio.sleep(_PAIRING_SETTLE_DEFAULT_SEC)

    async def pair(self, key: bytearray | None = None) -> None:
        """Program pairing credentials according to ``host_pairing_mode``."""
        pair_key = key or PAIRING_KEY
        if self._config.host_pairing_mode == HostPairingMode.OS_BONDING:
            await self._pair_os_bonding()
            return
        if self._config.host_pairing_mode == HostPairingMode.NONE:
            raise ConnectionError("Pairing is disabled for this device profile")
        if self._config.host_pairing_mode != HostPairingMode.CUSTOM_KEY:
            raise ConnectionError("Pairing is not supported for this device")
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


def _decode_eeprom_time_payload(layout: str, cached: bytearray) -> dt.datetime:
    """Decode wall time from an EEPROM time-sync section (naive datetime)."""
    if layout == "eeprom_time_modern_offset8":
        year_off, month, day, hour, minute, second = (int(b) for b in cached[8:14])
        return dt.datetime(
            year_off + 2000, month, day, hour, minute, min(second, 59)
        )
    if layout == "eeprom_time_classic_offset8":
        month, year_off, hour, day, second, minute = (int(b) for b in cached[8:14])
        return dt.datetime(
            year_off + 2000, month, day, hour, minute, min(second, 59)
        )
    if layout == "eeprom_time_hem6401_prefix":
        year_off, month, day, hour, minute, second = (int(b) for b in cached[0:6])
        return dt.datetime(
            year_off + 2000, month, day, hour, minute, min(second, 59)
        )
    if layout == "eeprom_time_linear_10":
        year_off, month, day, hour, minute, second = (int(b) for b in cached[2:8])
        return dt.datetime(
            year_off + 2000, month, day, hour, minute, min(second, 59)
        )
    # Default: eeprom_time_classic_mixed
    month, year_off, hour, day, second, minute = (int(b) for b in cached[2:8])
    return dt.datetime(
        year_off + 2000, month, day, hour, minute, min(second, 59)
    )


def _encode_eeprom_time_payload(
    layout: str, cached: bytearray, now: dt.datetime
) -> bytearray:
    """Build EEPROM time-sync bytes for writing (includes checksum/padding per layout)."""
    if layout == "eeprom_time_modern_offset8":
        result = bytearray(cached[0:8])
        result += bytes(
            [
                now.year - 2000,
                now.month,
                now.day,
                now.hour,
                now.minute,
                now.second,
            ]
        )
        result.append(sum(result) & 0xFF)
        result += bytes([0x00])
        return result
    if layout == "eeprom_time_classic_offset8":
        result = bytearray(cached[0:8])
        result += bytes(
            [
                now.month,
                now.year - 2000,
                now.hour,
                now.day,
                now.second,
                now.minute,
            ]
        )
        result.append(sum(result) & 0xFF)
        result += bytes([0x00])
        return result
    if layout == "eeprom_time_hem6401_prefix":
        result = bytearray(cached)
        if len(result) < 16:
            result.extend([0x00] * (16 - len(result)))
        result[0:6] = bytes(
            [
                now.year - 2000,
                now.month,
                now.day,
                now.hour,
                now.minute,
                now.second,
            ]
        )
        return result
    if layout == "eeprom_time_linear_10":
        result = bytearray(cached[0:2])
        result += bytes(
            [
                now.year - 2000,
                now.month,
                now.day,
                now.hour,
                now.minute,
                now.second,
            ]
        )
        result += bytes([0x00])
        result.append(sum(result) & 0xFF)
        return result
    # Default: eeprom_time_classic_mixed
    result = bytearray(cached[0:2])
    result += bytes(
        [
            now.month,
            now.year - 2000,
            now.hour,
            now.day,
            now.second,
            now.minute,
        ]
    )
    result += bytes([0x00])
    result.append(sum(result) & 0xFF)
    return result


class OmronDeviceDriver:
    """High-level driver for reading records from Omron blood pressure monitors.

    Uses DeviceConfig for device-specific behavior.
    """

    def __init__(self, config: DeviceConfig) -> None:
        self._config = config
        self._cached_settings: bytearray | None = None
        self._now_func = dt.datetime.now
        self._counter_probe_logged = False

    async def sync_eeprom_time(
        self, transport: OmronDeviceSession, now: dt.datetime | None = None
    ) -> bool:
        """Synchronize time to legacy devices via EEPROM settings write.

        Legacy Omron devices (classic-stack with custom key pairing) do not use
        the standard BLE CTS characteristic for time synchronization.  Instead,
        the time is stored in a dedicated region of the EEPROM settings block.

        Layout keys (``DeviceConfig.time_sync_layout`` / ``resolved_time_sync_layout``):

        eeprom_time_classic_mixed (default for [0x14, 0x1E] classic block)
            Time bytes [2:8] = [month, year-2000, hour, day, second, minute]
            Checksum [9] = sum(bytes[0:9]) & 0xFF

        eeprom_time_linear_10 (same 10-byte window, chronological field order)
            Time bytes [2:8] = [year-2000, month, day, hour, minute, second]
            Checksum [9] = sum(bytes[0:9]) & 0xFF

        eeprom_time_modern_offset8 ([0x2C, 0x3C] 16-byte block)
            Time bytes [8:14] = [year-2000, month, day, hour, minute, second]
            Checksum [14] = sum(bytes[0:14]) & 0xFF

        eeprom_time_hem6401_prefix (HEM-6401 family 16-byte settings slice)
            Time bytes [0:6] = [year-2000, month, day, hour, minute, second]
            Full 16-byte section write without the classic 10-byte checksum tail.

        Returns True on success, False if the device does not support EEPROM time sync.
        """
        if not self._config.supports_eeprom_time_sync:
            return False

        time_sync_range = self._config.settings_time_sync_bytes
        read_addr = self._config.settings_read_address
        write_addr = self._config.settings_write_address
        if time_sync_range is None or read_addr is None or write_addr is None:
            return False

        section_start, section_end = time_sync_range
        section_size = section_end - section_start

        if now is None:
            now = self._now_func()
        # Normalize to local timezone-aware datetime so comparisons with parsed
        # EEPROM timestamps never fail on naive/aware mismatch.
        if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
            now = now.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)

        await transport.unlock()

        # Read current time sync settings from EEPROM
        cached = await transport.read_memory_range(
            read_addr + section_start,
            section_size,
            min(section_size, self._config.transmission_block_size),
        )
        cached = bytearray(cached)
        _LOGGER.debug(
            "EEPROM time raw for %s (layout=%s addr=0x%04X+0x%02X size=%d): %s",
            self._config.model,
            self._config.resolved_time_sync_layout(),
            read_addr,
            section_start,
            section_size,
            bytes(cached).hex(),
        )

        # Parse current device time and only write if difference is > 60 seconds
        device_dt = self._parse_eeprom_device_time(cached)
        if device_dt is not None:
            diff = abs((device_dt - now).total_seconds())
            if diff <= 60:
                _LOGGER.debug(
                    "Device %s time is already in sync (%s), skipping EEPROM write",
                    self._config.model,
                    device_dt.strftime("%Y-%m-%d %H:%M:%S"),
                )
                return True

        # Write new time into the cached settings
        cached = self._build_eeprom_time_data(cached, now)

        # Write the modified settings back to EEPROM
        await transport.write_memory_range(
            write_addr + section_start,
            cached,
            block_size=len(cached),
        )
        # Allow the device to commit the EEPROM write internally.
        # Without this settle time, subsequent read commands may time out
        # because the device is still processing the write operation.
        await asyncio.sleep(1.0)
        _LOGGER.debug(
            "Synced time via EEPROM for %s: %s",
            self._config.model,
            now.strftime("%Y-%m-%d %H:%M:%S"),
        )

        return True

    def _parse_eeprom_device_time(self, cached: bytearray) -> dt.datetime | None:
        """Parse and return the current time stored on the device (best-effort)."""
        try:
            layout = self._config.resolved_time_sync_layout()
            device_dt = _decode_eeprom_time_payload(layout, cached)
            # Use local timezone to match the `now` timezone we compare against
            device_dt = device_dt.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
            return device_dt
        except Exception:
            _LOGGER.warning(
                "Device %s has invalid EEPROM time data: %s",
                self._config.model,
                bytes(cached).hex(),
            )
            return None

    def _build_eeprom_time_data(
        self, cached: bytearray, now: dt.datetime
    ) -> bytearray:
        """Build the EEPROM time sync payload with updated time and checksum."""
        layout = self._config.resolved_time_sync_layout()
        return _encode_eeprom_time_payload(layout, cached, now)

    def _finalize_public_latest_record(
        self, record: dict[str, Any], user: int
    ) -> dict[str, Any]:
        """Copy a parsed record for API consumers and strip internal EEPROM offsets."""
        result = dict(record)
        result["user"] = user
        result.pop("_slot_index", None)
        result.pop("_offset", None)
        return result

    async def get_all_records(
        self, transport: OmronDeviceSession
    ) -> list[list[dict[str, Any]]]:
        """Read all records from all users.

        Returns a list of lists: [[user1_records], [user2_records], ...]
        """
        await transport.unlock()

        all_user_records = []
        for user_idx in range(self._config.num_users):
            start_addr = self._config.user_start_addresses[user_idx]
            total_bytes = (
                self._config.per_user_records_count[user_idx]
                * self._config.record_byte_size
            )

            raw_data = await transport.read_memory_range(
                start_addr, total_bytes, self._config.transmission_block_size
            )

            records = self._parse_user_records(raw_data, user_idx)
            all_user_records.append(records)

        return all_user_records

    async def get_latest_record(
        self, transport: OmronDeviceSession
    ) -> dict[str, Any] | None:
        """Read latest record using index first, then fallback to full scan."""
        layout = self._config.index_pointer_layout or {}
        indexed = await self._get_latest_via_index(transport)
        if indexed is not None:
            return indexed
        if bool(layout.get("skip_full_scan_fallback_when_index_empty")):
            _LOGGER.debug(
                "Index path returned no valid candidate for model=%s; skipping full scan fallback",
                self._config.model,
            )
            return None
        _LOGGER.debug(
            "%s index path did not yield a valid latest record; falling back to full scan",
            self._config.model,
        )
        return await self._get_latest_via_full_scan(transport)

    async def get_latest_records_per_user(
        self, transport: OmronDeviceSession
    ) -> dict[int, dict[str, Any]]:
        """Return latest valid record per configured user index (1-based).

        Tries the index-based fast path first.  If the index covers all expected
        users the result is returned immediately.  When only a subset of users has
        a valid index entry the partial result is kept and a full-scan fallback
        supplies the missing users, avoiding a second round-trip for the users
        already found via the index.

        Users whose probed index slot(s) were all ``0xFF`` (the device's
        empty-slot marker) are reported by ``_get_latest_via_index`` via the
        ``confirmed_empty_users`` set and are skipped from the full-scan
        fallback — they demonstrably have never recorded a measurement, so
        scanning their memory region wastes a BLE session window (~60 s for
        100-slot users) and tends to produce spurious TX timeouts as the
        device runs out of payload to send back.
        """
        latest_by_user: dict[int, dict[str, Any]] = {}
        expected_user_count = len(self._config.per_user_records_count)

        index_result = await self._get_latest_via_index(
            transport, return_all_users=True
        )
        # New return shape is ``(dict, set)``.  Tolerate the older ``dict | None``
        # shape too in case a sub-class or backport returns it.
        if isinstance(index_result, tuple):
            indexed_candidates, confirmed_empty_users = index_result
        else:
            indexed_candidates = index_result or {}
            confirmed_empty_users = set()

        if indexed_candidates:
            if len(indexed_candidates) >= expected_user_count:
                # All users covered — return without a full scan.
                return indexed_candidates
            # Partial: keep what the index found; fall back for the rest.
            latest_by_user.update(indexed_candidates)

        missing_users = set(range(1, expected_user_count + 1)) - set(latest_by_user.keys())
        if not missing_users:
            return latest_by_user

        # Skip the full-scan fallback for users whose probed slots were
        # confirmed all-0xFF.  If every missing user is empty there is
        # nothing left to scan, so we return immediately.
        scan_required_users = missing_users - confirmed_empty_users
        skipped_empty = missing_users & confirmed_empty_users
        if skipped_empty:
            _LOGGER.debug(
                "Index path confirmed user(s) %s as empty for model=%s; "
                "skipping full-scan fallback for them",
                sorted(skipped_empty),
                self._config.model,
            )
        if not scan_required_users:
            _LOGGER.debug(
                "Index path returned %d/%d user(s) for model=%s; "
                "remaining user(s) are confirmed empty — skipping full scan",
                len(indexed_candidates),
                expected_user_count,
                self._config.model,
            )
            return latest_by_user

        _LOGGER.debug(
            "Index path returned %d/%d user(s) for model=%s; "
            "falling back to full scan for user(s) %s",
            len(indexed_candidates),
            expected_user_count,
            self._config.model,
            sorted(scan_required_users),
        )
        # Full-scan fallback — only processes users absent from latest_by_user
        # *and* not in ``confirmed_empty_users``.
        all_user_records = await self.get_all_records(transport)
        for user_idx, user_records in enumerate(all_user_records):
            user = user_idx + 1
            if user not in scan_required_users:
                continue
            if not user_records:
                continue
            selected = self._select_latest_candidate([(user, rec) for rec in user_records])
            if selected is None:
                continue
            _, record = selected
            latest_by_user[user] = self._finalize_public_latest_record(record, user)
        return latest_by_user

    async def _get_latest_via_full_scan(
        self, transport: OmronDeviceSession
    ) -> dict[str, Any] | None:
        """Existing full EEPROM scan path."""
        all_user_records = await self.get_all_records(transport)
        candidates: list[tuple[int, dict[str, Any]]] = []
        for user_idx, user_records in enumerate(all_user_records):
            for record in user_records:
                candidates.append((user_idx + 1, record))

        selected = self._select_latest_candidate(candidates)
        if selected is None:
            return None
        user, record = selected
        return self._finalize_public_latest_record(record, user)

    @staticmethod
    def _wrap_pointer_to_range(pointer: int, pointer_min: int, pointer_max: int) -> int | None:
        """Wrap pointer into [min, max] range (device index window semantics)."""
        if pointer_max < pointer_min:
            return None
        span = (pointer_max - pointer_min) + 1
        if span <= 0:
            return None
        while pointer < pointer_min:
            pointer += span
        while pointer > pointer_max:
            pointer -= span
        return pointer

    async def _get_latest_via_index(
        self, transport: OmronDeviceSession, *, return_all_users: bool = False
    ) -> Any | None:
        """Read index block and fetch only the latest slot per configured user.

        When ``return_all_users=True`` the function returns a tuple
        ``(per_user_records, confirmed_empty_users)``:

        * ``per_user_records`` — ``dict[int, record]`` keyed by 1-based user
          index, containing the latest valid measurement found via the index
          probe for that user (only users with a valid record are present).
        * ``confirmed_empty_users`` — ``set[int]`` of 1-based user indices
          whose probed slot(s) were *all* ``0xFF`` (the device's empty-slot
          marker).  These users have demonstrably never recorded a
          measurement; the caller can skip the expensive full-scan fallback
          for them.

        When ``return_all_users=False`` the function preserves the original
        single-record return shape (``dict | None``) for backward
        compatibility.
        """
        layout = self._config.index_pointer_layout
        if (
            layout is None
            or self._config.settings_read_address is None
            or self._config.record_byte_size <= 0
        ):
            return None if not return_all_users else ({}, set())

        index_region_byte_size = int(layout.get("index_region_byte_size", 0))
        user_layouts = layout.get("users", [])
        if index_region_byte_size <= 0 or not isinstance(user_layouts, list) or not user_layouts:
            return None if not return_all_users else ({}, set())

        record_addresses = layout.get("record_addresses") or self._config.user_start_addresses
        record_byte_size = int(layout.get("record_byte_size", self._config.record_byte_size))
        record_step = int(layout.get("record_step", record_byte_size))
        backtrack_slots = int(layout.get("backtrack_slots", 0))
        collect_all_valid = bool(layout.get("collect_all_valid_in_index_window", False))
        ptr_endian = str(layout.get("endianness", self._config.endianness))

        candidates: list[tuple[int, dict[str, Any]]] = []
        # Users whose probed slot(s) were all-0xFF — device has never recorded
        # a measurement for them.  Used by the caller to skip the full-scan
        # fallback that would otherwise spend ~60 s scanning a blank region
        # and produce spurious TX timeouts.
        confirmed_empty_users: set[int] = set()
        max_probe: int = 0  # initialised here so the finally-block log never hits NameError
        await transport.unlock()
        try:
            index_bytes = await transport.read_memory_range(
                self._config.settings_read_address,
                index_region_byte_size,
                self._config.transmission_block_size,
            )
            _LOGGER.debug(
                "Index block [%s]: addr=0x%04X size=%d endian=%s raw=%s",
                self._config.model,
                self._config.settings_read_address,
                index_region_byte_size,
                ptr_endian,
                bytes(index_bytes).hex(),
            )
            for idx, user_cfg in enumerate(user_layouts):
                if idx >= len(record_addresses) or idx >= len(self._config.per_user_records_count):
                    continue
                write_cursor_offset = int(user_cfg.get("write_cursor_offset", -1))
                if write_cursor_offset < 0 or write_cursor_offset + 2 > len(index_bytes):
                    _LOGGER.debug(
                        "User%d [%s]: write_cursor_offset=0x%02X invalid (index_bytes len=%d), skipping",
                        idx + 1, self._config.model, write_cursor_offset, len(index_bytes),
                    )
                    continue

                raw_pointer = int.from_bytes(
                    index_bytes[write_cursor_offset:write_cursor_offset + 2],
                    ptr_endian,
                    signed=False,
                )
                pointer_mask = int(user_cfg.get("write_cursor_mask", 0xFF))
                pointer_min = int(user_cfg.get("slot_index_min", 0))
                pointer_max = int(
                    user_cfg.get(
                        "slot_index_max",
                        self._config.per_user_records_count[idx] - 1,
                    )
                )
                correction = int(user_cfg.get("slot_index_bias", -1))
                pointer_masked = raw_pointer & pointer_mask
                pointer_corrected = pointer_masked + correction
                pointer_wrapped = self._wrap_pointer_to_range(
                    pointer_corrected, pointer_min, pointer_max
                )
                if pointer_wrapped is None:
                    _LOGGER.debug(
                        "User%d [%s]: cursor raw=0x%04X masked=0x%02X corrected=%d wrapped=None "
                        "(range [%d,%d]), skipping",
                        idx + 1, self._config.model,
                        raw_pointer, pointer_masked, pointer_corrected,
                        pointer_min, pointer_max,
                    )
                    continue
                record_count = (pointer_max - pointer_min) + 1
                if record_count <= 0:
                    continue
                latest_slot = pointer_wrapped
                _LOGGER.debug(
                    "User%d [%s]: cursor raw=0x%04X masked=0x%02X bias=%+d "
                    "→ slot=%d (range [%d,%d]) base_addr=0x%04X record_step=%d",
                    idx + 1, self._config.model,
                    raw_pointer, pointer_masked, correction,
                    latest_slot, pointer_min, pointer_max,
                    int(record_addresses[idx]), record_step,
                )
                max_probe = min(max(backtrack_slots, 0), max(record_count - 1, 0))
                parsed = None
                base_addr = int(record_addresses[idx])
                # Track whether every probed slot for this user was the
                # device's empty marker (all-0xFF).  If so, the user has
                # never recorded a measurement and the caller can skip the
                # full-scan fallback safely.
                user_had_any_read = False
                user_all_probed_slots_empty = True
                for back in range(max_probe + 1):
                    probe_slot = latest_slot - back
                    while probe_slot < pointer_min:
                        probe_slot += record_count
                    logical_slot = probe_slot - pointer_min
                    probe_addr = base_addr + (logical_slot * record_step)
                    raw_record = await transport.read_memory_range(
                        probe_addr,
                        record_byte_size,
                        self._config.transmission_block_size,
                    )
                    _LOGGER.debug(
                        "User%d [%s] slot=%d addr=0x%04X raw=%s",
                        idx + 1, self._config.model, probe_slot,
                        probe_addr, bytes(raw_record).hex(),
                    )
                    user_had_any_read = True
                    # The device leaves un-written slots as all-0xFF.  A
                    # single byte that differs means *something* was stored
                    # at this slot, even if our parser rejects it.
                    if any(b != 0xFF for b in raw_record):
                        user_all_probed_slots_empty = False
                    try:
                        parsed = self._config.parse_record(bytes(raw_record))
                    except Exception as parse_exc:
                        _LOGGER.debug(
                            "User%d [%s] slot=%d parse error: %s",
                            idx + 1, self._config.model, probe_slot, parse_exc,
                        )
                        parsed = None
                        continue
                    parsed["_slot_index"] = probe_slot
                    _LOGGER.debug(
                        "User%d [%s] slot=%d parsed: sys=%s dia=%s bpm=%s "
                        "dt=%s ihb=%s mov=%s cuff=%s pos=%s",
                        idx + 1, self._config.model, probe_slot,
                        parsed.get("sys"), parsed.get("dia"), parsed.get("bpm"),
                        parsed.get("datetime"), parsed.get("ihb"),
                        parsed.get("mov"), parsed.get("cuff"), parsed.get("pos"),
                    )
                    if not self._is_record_plausible(parsed):
                        parsed = None
                        continue
                    candidates.append((idx + 1, parsed))
                    if not collect_all_valid:
                        break
                    parsed = None
                # After the backtrack window completes: if every read came
                # back all-0xFF, mark this user as definitively empty.
                if user_had_any_read and user_all_probed_slots_empty:
                    confirmed_empty_users.add(idx + 1)
                    _LOGGER.debug(
                        "User%d [%s] confirmed empty: cursor slot and %d "
                        "backtrack slot(s) all 0xFF — full-scan fallback "
                        "will be skipped for this user",
                        idx + 1, self._config.model, max_probe,
                    )
        except Exception as exc:
            if self._config.host_pairing_mode == HostPairingMode.OS_BONDING:
                _LOGGER.warning(
                    "Index-based read failed for OS-bonding model=%s addr may need re-bond: %s. "
                    "If this persists, remove and re-add the device to complete OS-level pairing.",
                    self._config.model,
                    exc,
                )
            else:
                _LOGGER.debug(
                    "Index-based latest read failed for model=%s: %s",
                    self._config.model,
                    exc,
                )
            # Transport exception — we cannot confirm any user as empty, so
            # leave ``confirmed_empty_users`` empty and let the caller fall
            # back to a full scan as it would have without this feature.
            return None if not return_all_users else ({}, set())

        if not candidates:
            _LOGGER.debug(
                "Index read [%s]: no valid candidate found (checked %d configured user layout(s))",
                self._config.model, len(user_layouts),
            )
            return None if not return_all_users else ({}, confirmed_empty_users)

        if return_all_users:
            result_per_user: dict[int, dict[str, Any]] = {}
            for user_idx in range(len(user_layouts)):
                user = user_idx + 1
                user_candidates = [c for c in candidates if c[0] == user]
                
                # Check for TruRead sequence in user_candidates
                # They are ordered newest to oldest if probed sequentially
                truread_avg = None
                if len(user_candidates) >= 3:
                    # Sort candidates by slot index just to be safe, newest last
                    sorted_cands = sorted(user_candidates, key=lambda x: x[1].get('_slot_index', -1))
                    if len(sorted_cands) >= 3:
                        c3, c2, c1 = sorted_cands[-1], sorted_cands[-2], sorted_cands[-3]
                        if c3[1].get('pos') == 3 and c2[1].get('pos') == 2 and c1[1].get('pos') == 1:
                            dt3 = c3[1].get('datetime')
                            dt1 = c1[1].get('datetime')
                            # Check if the whole session fits in 15 minutes
                            import datetime as dt_mod
                            if dt3 and dt1 and (dt3 - dt1) <= dt_mod.timedelta(minutes=15):
                                avg_sys = round((c3[1].get('sys', 0) + c2[1].get('sys', 0) + c1[1].get('sys', 0)) / 3)
                                avg_dia = round((c3[1].get('dia', 0) + c2[1].get('dia', 0) + c1[1].get('dia', 0)) / 3)
                                avg_bpm = round((c3[1].get('bpm', 0) + c2[1].get('bpm', 0) + c1[1].get('bpm', 0)) / 3)
                                
                                # Clone c3 as the base for the virtual average record
                                avg_record = dict(c3[1])
                                avg_record['sys'] = avg_sys
                                avg_record['dia'] = avg_dia
                                avg_record['bpm'] = avg_bpm
                                avg_record['measurement_type'] = 'TruRead Average'
                                # c3 carries pos=3 (sequence index); overwrite with 0
                                # so the aggregate doesn't report improper_position=True,
                                # while still pushing a fresh value to the sensor.
                                avg_record['pos'] = 0
                                
                                # Store individual records for attributes
                                def _clean_rec(r):
                                    return {
                                        'sys': r.get('sys'),
                                        'dia': r.get('dia'),
                                        'bpm': r.get('bpm'),
                                        'time': r.get('datetime').isoformat() if r.get('datetime') else None,
                                        'pos': r.get('pos')
                                    }
                                avg_record['truread_details'] = [
                                    _clean_rec(c1[1]),
                                    _clean_rec(c2[1]),
                                    _clean_rec(c3[1])
                                ]
                                truread_avg = (user, avg_record)

                if truread_avg:
                    selected = truread_avg
                else:
                    selected = self._select_latest_candidate(user_candidates)
                    if selected:
                        selected[1]['measurement_type'] = 'Single'

                if selected:
                    result_per_user[user] = self._finalize_public_latest_record(selected[1], user)
            return result_per_user, confirmed_empty_users

        selected = self._select_latest_candidate(candidates)
        if selected is None:
            return None
        user, record = selected
        _LOGGER.debug(
            "Index selected [%s]: user=%d slot=%d sys=%s dia=%s bpm=%s dt=%s",
            self._config.model, user, record.get("_slot_index", "?"),
            record.get("sys"), record.get("dia"), record.get("bpm"),
            record.get("datetime"),
        )
        return self._finalize_public_latest_record(record, user)

    async def get_all_records_flat(
        self, transport: OmronDeviceSession
    ) -> list[dict[str, Any]]:
        """Read all records, adding user index, and return a flat sorted list."""
        all_user_records = await self.get_all_records(transport)

        flat = []
        for user_idx, user_records in enumerate(all_user_records):
            for record in user_records:
                record["user"] = user_idx + 1
                flat.append(record)

        flat.sort(key=lambda r: r["datetime"])
        return flat

    def _parse_user_records(
        self,
        raw_data: bytearray,
        user_idx: int,
        record_byte_size: int | None = None,
    ) -> list[dict[str, Any]]:
        """Parse raw EEPROM bytes into a list of record dicts."""
        records = []
        size = record_byte_size or self._config.record_byte_size
        empty_record = b'\xff' * size

        for offset in range(0, len(raw_data), size):
            single = raw_data[offset:offset + size]
            if single == empty_record:
                continue
            try:
                record = self._config.parse_record(single)
                record["_slot_index"] = offset // size
                record["_offset"] = offset
                if not self._is_record_plausible(record):
                    continue
                records.append(record)
            except ValueError:
                # Many devices leave partially initialized slots (not always all 0xFF).
                pass
            except Exception as exc:
                _LOGGER.warning(
                    "Error parsing record for user%d at offset %d (data: %s): %s",
                    user_idx + 1, offset, _hex(single), exc,
                )
        return records

    def _select_latest_candidate(
        self, candidates: list[tuple[int, dict[str, Any]]]
    ) -> tuple[int, dict[str, Any]] | None:
        """Choose the latest record across users using model-specific strategy."""
        if not candidates:
            return None

        if self._config.prefer_latest_by_slot_index:
            return max(
                candidates,
                key=lambda item: (
                    item[1].get("_slot_index", -1),
                    item[1].get("datetime", dt.datetime.min),
                ),
            )

        return max(
            candidates,
            key=lambda item: (
                item[1].get("datetime", dt.datetime.min),
                item[1].get("_slot_index", -1),
            ),
        )

    def _is_record_plausible(self, record: dict[str, Any]) -> bool:
        """Sanity-check parsed values to avoid stale/garbage slot selection."""
        date_value = record.get("datetime")
        if not isinstance(date_value, dt.datetime):
            _LOGGER.debug(
                "Record rejected [%s slot=%s]: datetime is %r (not a datetime object)",
                self._config.model, record.get("_slot_index", "?"), date_value,
            )
            return False

        now = self._now_func()
        if date_value < dt.datetime(2010, 1, 1):
            _LOGGER.debug(
                "Record rejected [%s slot=%s]: datetime %s is before 2010 (likely empty/corrupt slot)",
                self._config.model, record.get("_slot_index", "?"), date_value,
            )
            return False
        if date_value > (now + dt.timedelta(days=2)):
            _LOGGER.debug(
                "Record rejected [%s slot=%s]: datetime %s is in the future (clock sync issue?)",
                self._config.model, record.get("_slot_index", "?"), date_value,
            )
            return False

        sys = record.get("sys")
        dia = record.get("dia")
        bpm = record.get("bpm")
        if not isinstance(sys, int) or not isinstance(dia, int) or not isinstance(bpm, int):
            _LOGGER.debug(
                "Record rejected [%s slot=%s]: non-integer vitals sys=%r dia=%r bpm=%r",
                self._config.model, record.get("_slot_index", "?"), sys, dia, bpm,
            )
            return False
        if not (60 <= sys <= 280):
            _LOGGER.debug(
                "Record rejected [%s slot=%s]: sys=%d out of range [60, 280]",
                self._config.model, record.get("_slot_index", "?"), sys,
            )
            return False
        if not (30 <= dia <= 180):
            _LOGGER.debug(
                "Record rejected [%s slot=%s]: dia=%d out of range [30, 180]",
                self._config.model, record.get("_slot_index", "?"), dia,
            )
            return False
        if not (30 <= bpm <= 240):
            _LOGGER.debug(
                "Record rejected [%s slot=%s]: bpm=%d out of range [30, 240]",
                self._config.model, record.get("_slot_index", "?"), bpm,
            )
            return False
        if dia >= sys:
            _LOGGER.debug(
                "Record rejected [%s slot=%s]: dia=%d >= sys=%d (physiologically invalid)",
                self._config.model, record.get("_slot_index", "?"), dia, sys,
            )
            return False
        return True
