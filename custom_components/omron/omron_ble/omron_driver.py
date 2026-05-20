from __future__ import annotations

import asyncio
import datetime as dt
import logging
import traceback
from typing import Any

from bleak import BleakClient
from bleak.exc import BleakError

from .devices import DeviceConfig

_LOGGER = logging.getLogger(__name__)

# BLE memory-protocol pacing (extra margin for weak RF / busy stacks).
_MEMORY_PROTOCOL_REPLY_TIMEOUT_SEC: float = 3.5
_MEMORY_PROTOCOL_TX_MAX_RETRIES: int = 4
_MEMORY_PROTOCOL_RETRY_BACKOFF_SEC: float = 0.25
_NOTIFY_SUBSCRIBE_SETTLE_SEC: float = 0.75
_NOTIFY_SUBSCRIBE_MAX_RETRIES: int = 3

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


class GattTransport:
    """BLE GATT read/write and notify handling for Omron measurement memory access.

    Supports single-channel (OS-bonding) and multi-channel (classic pairing) profiles.
    """

    def __init__(self, client: BleakClient, device_config: DeviceConfig) -> None:
        self._client = client
        self._config = device_config
        self._notify_subscribed = False
        self._last_reply_packet_type: bytes | None = None
        self._last_reply_memory_address: bytes | None = None
        self._last_reply_payload: bytes | None = None
        self._reply_ready = asyncio.Event()
        self._channel_fragments: list[bytes | None] = [None] * 4
        self._notify_handle_to_channel: dict[int, int] = {}
        self._memory_session_depth = 0
        self._unlocked = False

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
        """Enable notifications on all RX channels (and any ctrl channels if configured)."""
        if self._notify_subscribed:
            _LOGGER.debug(
                "RX notify subscribe skipped (already flagged) model=%s",
                self._config.model,
            )
            return

        self._debug_ble_link("before_rx_subscribe")
        await self._ensure_services_cache()
        self._rebuild_notify_handle_index_map()

        # Subscribe ctrl channels first (device may require CCCD before accepting commands).
        for uuid in self._config.ctrl_notify_uuids:
            try:
                await self._client.start_notify(uuid, lambda _h, _d: None)
            except Exception as exc:
                _LOGGER.debug("ctrl_notify subscribe skipped for %s: %s", uuid, exc)

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
        """Disable notifications on all RX and ctrl channels."""
        for uuid in self._config.rx_channel_uuids:
            try:
                await self._client.stop_notify(uuid)
            except Exception as exc:
                _LOGGER.debug("stop_notify for %s ignored: %s", uuid, exc)
        for uuid in self._config.ctrl_notify_uuids:
            try:
                await self._client.stop_notify(uuid)
            except Exception as exc:
                _LOGGER.debug("ctrl stop_notify for %s ignored: %s", uuid, exc)
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
        self._memory_session_depth = 0
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

    async def open_memory_session(self) -> None:
        """Start a data readout session."""
        self._memory_session_depth += 1
        if self._memory_session_depth > 1:
            _LOGGER.debug("Memory session already open, increasing depth to %d", self._memory_session_depth)
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
            self._debug_ble_link("open_memory_session_ok")
        except BaseException:
            if self._memory_session_depth > 0:
                self._memory_session_depth -= 1
            self._unlocked = False
            self._debug_ble_link("open_memory_session_fail_cleanup")
            await self._unsubscribe_notify_channels()
            raise

    async def close_memory_session(self) -> None:
        """End a data readout session."""
        if self._memory_session_depth <= 0:
            return
        self._memory_session_depth -= 1
        if self._memory_session_depth > 0:
            _LOGGER.debug("Decreasing memory session depth to %d", self._memory_session_depth)
            return

        stop_cmd = bytearray.fromhex("080f000000000007")
        await self._write_command_and_wait_reply(stop_cmd)
        if self._last_reply_packet_type != bytearray.fromhex("8f00"):
            _LOGGER.warning("Invalid response to data readout end")
        elif self._last_reply_payload and self._last_reply_payload[0]:
            _LOGGER.warning(
                "Device reported error code %d during session close", self._last_reply_payload[0]
            )
        await self._unsubscribe_notify_channels()

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

    async def unlock(self, key: bytearray | None = None) -> None:
        """Unlock device with pairing key."""
        if not self._config.requires_unlock:
            _LOGGER.debug("unlock skipped: requires_unlock=False model=%s", self._config.model)
            return
        if self._unlocked:
            _LOGGER.debug("unlock skipped: transport already unlocked model=%s", self._config.model)
            return

        self._require_connected("unlock")

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
            # Legacy classic-stack devices are often more stable if we send a
            # short "confirm encryption" probe before auth-key unlock.
            if self._config.legacy_pairing_workarounds:
                unlock_event.clear()
                response_holder[0] = None
                try:
                    await self._client.write_gatt_char(
                        self._config.unlock_uuid, b'\x02' + b'\x00' * 16, response=True
                    )
                    await asyncio.wait_for(unlock_event.wait(), timeout=2.0)
                except Exception:
                    pass

            unlock_event.clear()
            response_holder[0] = None
            await self._client.write_gatt_char(
                self._config.unlock_uuid, b'\x01' + unlock_key, response=True
            )
            await asyncio.wait_for(unlock_event.wait(), timeout=5.0)

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

    async def pair(
        self,
        key: bytearray | None = None,
        *,
        high_protection: bool = True,
    ) -> None:
        """Program a new pairing key into the device.

        The device must be in pairing mode (hold bluetooth button until -P- blinks).
        For OS-bonding-only devices (e.g. HEM-7380T1), performs standard BLE pairing.

        ``high_protection`` (default ``True``) requests
        ``protection_level=4`` (Authenticated + Encrypted, MITM-protected) on
        the first try.  This is the strongest pairing protection the backend
        supports and matches the security level the official Omron Connect
        app obtains via Android's ``BluetoothDevice.createBond()``.

        If ``protection_level=4`` is rejected by the backend (``TypeError``
        for backends that don't accept the kwarg, e.g. BlueZ) or refused by
        the device (e.g. ``BleakError``/``ConnectionError`` because the
        device only supports Just Works), the same attempt falls back to
        ``pair()`` with no arguments so a weaker-but-still-encrypted bond
        can be established.

        Pass ``high_protection=False`` to skip the level-4 request entirely
        and call ``pair()`` directly — useful for non-user-initiated flows
        that just want best-effort bond confirmation.
        """
        pair_key = key or PAIRING_KEY

        # OS-level bonding only (HEM-7380T1 etc.)
        if self._config.supports_os_bonding_only:
            _LOGGER.debug(
                "Performing OS-level BLE bonding (high_protection=%s)",
                high_protection,
            )
            # Some stacks fail pair() even when already bonded/encrypted sessions work.
            # Keep this as best-effort and allow caller to proceed to GATT operations.
            max_attempts = 2
            last_exc: BaseException | None = None

            async def _post_bond_refresh() -> None:
                # After OS bonding completes, the peer may newly expose
                # encryption-required characteristics (e.g. TX channel
                # ``db5b55e0-…``) that were absent from the pre-bond GATT
                # cache.  Subsequent ``write_gatt_char`` lookups against
                # those UUIDs would otherwise fail with "Characteristic …
                # was not found".  Refresh the service cache so the next
                # GATT operation sees the post-bond GATT database.
                try:
                    await asyncio.sleep(0.3)
                    await _bleak_refresh_services(self._client)
                except Exception as refresh_exc:
                    _LOGGER.debug(
                        "Post-bond service refresh failed (continuing): %s",
                        refresh_exc,
                    )

            async def _do_pair_with_optional_high_protection() -> None:
                """Try level=4 first, fall back to default in the same attempt."""
                if high_protection:
                    try:
                        await self._client.pair(protection_level=4)
                        _LOGGER.debug(
                            "OS-level BLE bonding completed (protection_level=4)"
                        )
                        return
                    except TypeError as type_exc:
                        # Backend doesn't accept the kwarg (BlueZ etc.) —
                        # fall through to plain pair() in this same attempt.
                        _LOGGER.debug(
                            "Backend rejected protection_level=4 (%s); "
                            "falling back to default pair() in same attempt",
                            type_exc,
                        )
                    except Exception as level_exc:
                        # Device or backend refused level=4 (e.g. Just Works
                        # only).  Still in the pairing-mode window — try the
                        # default pair() immediately so the user's button
                        # press isn't wasted.
                        _LOGGER.debug(
                            "pair(protection_level=4) failed (%s); "
                            "falling back to default pair() in same attempt",
                            level_exc,
                        )
                await self._client.pair()
                _LOGGER.debug("OS-level BLE bonding completed (default)")

            for attempt in range(1, max_attempts + 1):
                try:
                    if attempt > 1:
                        await asyncio.sleep(0.5)
                        await _bleak_refresh_services(self._client)
                    await _do_pair_with_optional_high_protection()
                    await _post_bond_refresh()
                    return
                except Exception as exc:
                    last_exc = exc
                    if _is_non_fatal_os_pairing_error(exc):
                        _LOGGER.warning(
                            "OS-level bonding returned non-fatal error on attempt %d/%d: %s (%r)",
                            attempt,
                            max_attempts,
                            type(exc).__name__,
                            exc,
                        )
                        # "already bonded" / "in progress" still imply the
                        # bond exists — refresh the GATT cache anyway.
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
            return

        # Custom pairing key (most classic-stack devices)
        if not self._config.supports_pairing:
            raise ConnectionError("Pairing is not supported for this device")

        if len(pair_key) != 16:
            raise ValueError(f"Pairing key must be 16 bytes, got {len(pair_key)}")

        legacy = self._config.legacy_pairing_workarounds
        if legacy:
            await _bleak_refresh_services(self._client)
            unlock_attempts, unlock_retry_delay = 10, 0.5
            key_max_retries = 5
        else:
            unlock_attempts, unlock_retry_delay = 5, 1.0
            key_max_retries = 5

        # Step 1: Enable RX channel notification to trigger SMP Security Request (RX notify first, then unlock)
        _LOGGER.debug("Enabling RX notification to trigger BLE pairing")
        try:
            await self._client.start_notify(
                self._config.rx_channel_uuids[0], lambda h, d: None
            )
        except Exception as exc:
            _LOGGER.debug("Ignored error starting RX notify: %s", exc)

        if legacy:
            await asyncio.sleep(0.25)
            await _bleak_refresh_services(self._client)
        else:
            await asyncio.sleep(1.0)

        # Step 2: Subscribe on unlock UUID
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
                if legacy:
                    await _bleak_refresh_services(self._client)
                await asyncio.sleep(unlock_retry_delay)
        if not unlock_subscribed:
            raise ConnectionError(
                f"Characteristic {self._config.unlock_uuid} was not found! "
                "Try clearing Bluetooth cache, or remove the device from OS Bluetooth and retry in -P- mode."
            )

        # Step 3: Enter key programming mode (0x02 prefix writes)
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
                await asyncio.wait_for(prog_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

            resp = response_holder[0]
            if resp:
                last_notify = bytes(resp)
                if len(notify_samples) < 10:
                    notify_samples.append(
                        f"#{attempt + 1}:{_hex(resp)}"
                    )
            if _is_unlock_key_programming_ready(resp):
                _LOGGER.debug("Entered key programming mode after %d attempt(s)", attempt + 1)
                entered_programming = True
                break

            _LOGGER.debug(
                "Key programming attempt %d/%d got: %s",
                attempt + 1, max_retries,
                resp[:2].hex() if resp else "None",
            )
            await asyncio.sleep(1)

        if not entered_programming:
            try:
                await self._client.stop_notify(self._config.unlock_uuid)
                await self._client.stop_notify(self._config.rx_channel_uuids[0])
            except Exception:
                pass
            _LOGGER.error(
                "Key programming mode not reached: model=%s legacy_workarounds=%s "
                "unlock_uuid=%s attempts=%s write_failures=%s "
                "expected_notify_first_byte=0x82 last_notify_hex=%s samples=%s",
                self._config.model,
                legacy,
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

        # Step 4: Program the new key
        prog_event.clear()
        response_holder[0] = None
        try:
            await self._client.write_gatt_char(
                self._config.unlock_uuid, b'\x00' + pair_key, response=True
            )
        except Exception as exc:
            _LOGGER.error("Failed to write new key: %s", exc)

        try:
            await asyncio.wait_for(prog_event.wait(), timeout=5.0)
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
        await asyncio.sleep(1.0)


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
        self, transport: GattTransport, now: dt.datetime | None = None
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
        await transport.open_memory_session()
        try:
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
        finally:
            try:
                await transport.close_memory_session()
            except Exception:
                pass

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
        self, transport: GattTransport
    ) -> list[list[dict[str, Any]]]:
        """Read all records from all users.

        Returns a list of lists: [[user1_records], [user2_records], ...]
        """
        await transport.unlock()
        await transport.open_memory_session()

        try:
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
        finally:
            try:
                await transport.close_memory_session()
            except Exception:
                pass

        return all_user_records

    async def get_latest_record(
        self, transport: GattTransport
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
        self, transport: GattTransport
    ) -> dict[int, dict[str, Any]]:
        """Return latest valid record per configured user index (1-based).

        Tries the index-based fast path first.  If the index covers all expected
        users the result is returned immediately.  When only a subset of users has
        a valid index entry the partial result is kept and a full-scan fallback
        supplies the missing users, avoiding a second round-trip for the users
        already found via the index.
        """
        latest_by_user: dict[int, dict[str, Any]] = {}
        expected_user_count = len(self._config.per_user_records_count)

        indexed_candidates = await self._get_latest_via_index(
            transport, return_all_users=True
        )

        if indexed_candidates:
            if len(indexed_candidates) >= expected_user_count:
                # All users covered — return without a full scan.
                return indexed_candidates
            # Partial: keep what the index found; fall back for the rest.
            latest_by_user.update(indexed_candidates)
            _LOGGER.debug(
                "Index path returned %d/%d user(s) for model=%s; "
                "falling back to full scan for missing user(s)",
                len(indexed_candidates),
                expected_user_count,
                self._config.model,
            )

        missing_users = set(range(1, expected_user_count + 1)) - set(latest_by_user.keys())
        if not missing_users:
            return latest_by_user

        # Full-scan fallback — only processes users absent from latest_by_user.
        all_user_records = await self.get_all_records(transport)
        for user_idx, user_records in enumerate(all_user_records):
            user = user_idx + 1
            if user not in missing_users:
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
        self, transport: GattTransport
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
        self, transport: GattTransport, *, return_all_users: bool = False
    ) -> Any | None:
        """Read index block and fetch only the latest slot per configured user."""
        layout = self._config.index_pointer_layout
        if (
            layout is None
            or self._config.settings_read_address is None
            or self._config.record_byte_size <= 0
        ):
            return None if not return_all_users else {}

        index_region_byte_size = int(layout.get("index_region_byte_size", 0))
        user_layouts = layout.get("users", [])
        if index_region_byte_size <= 0 or not isinstance(user_layouts, list) or not user_layouts:
            return None if not return_all_users else {}

        record_addresses = layout.get("record_addresses") or self._config.user_start_addresses
        record_byte_size = int(layout.get("record_byte_size", self._config.record_byte_size))
        record_step = int(layout.get("record_step", record_byte_size))
        backtrack_slots = int(layout.get("backtrack_slots", 0))
        collect_all_valid = bool(layout.get("collect_all_valid_in_index_window", False))
        ptr_endian = str(layout.get("endianness", self._config.endianness))

        candidates: list[tuple[int, dict[str, Any]]] = []
        max_probe: int = 0  # initialised here so the finally-block log never hits NameError
        await transport.unlock()
        await transport.open_memory_session()
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
        except Exception as exc:
            if self._config.supports_os_bonding_only:
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
            return None
        finally:
            try:
                await transport.close_memory_session()
            except Exception:
                pass

        if not candidates:
            _LOGGER.debug(
                "Index read [%s]: no valid candidate found (checked %d configured user layout(s))",
                self._config.model, len(user_layouts),
            )
            return None if not return_all_users else {}

        if return_all_users:
            result_per_user: dict[int, dict[str, Any]] = {}
            for user_idx in range(len(user_layouts)):
                user = user_idx + 1
                user_candidates = [c for c in candidates if c[0] == user]
                selected = self._select_latest_candidate(user_candidates)
                if selected:
                    result_per_user[user] = self._finalize_public_latest_record(selected[1], user)
            return result_per_user

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
        self, transport: GattTransport
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
