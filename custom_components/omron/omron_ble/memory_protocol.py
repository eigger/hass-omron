"""The OMRON memory protocol: notify channels, command/reply, and the memory session.

Mixed into ``OmronDeviceSession``. It owns the reply state the notify callback
fills in and ``_write_command_and_wait_reply`` consumes, the RX-channel
subscriptions, and the memory session with its reads, writes and the pairing
registration written on the way out. The host provides the link
(``_client``, ``_config``, ``address``), the secure session used to wrap
frames, and ``_require_connected`` / ``_ensure_services_cache`` /
``_debug_ble_link``.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator

from bleak.exc import BleakError

from .connection import _bleak_refresh_services
from .devices import UnlockMode
from .settings_mirror import SettingsMirrorLayout, clock_block, slot_checksum
from .util import _hex

if TYPE_CHECKING:
    from bleak import BleakClient

    from .devices import DeviceConfig, PairingRegistration
    from .secure_session import SecureSession

_LOGGER = logging.getLogger(__name__)

# BLE memory-protocol pacing (extra margin for weak RF / busy stacks).
_MEMORY_PROTOCOL_REPLY_TIMEOUT_SEC: float = 5.0
_MEMORY_PROTOCOL_TX_MAX_RETRIES: int = 4
_MEMORY_PROTOCOL_RETRY_BACKOFF_SEC: float = 0.25
_NOTIFY_SUBSCRIBE_SETTLE_SEC: float = 0.75
_NOTIFY_SUBSCRIBE_MAX_RETRIES: int = 3

# The per-transfer slot that follows the index region in the settings mirror
# (#175 BP5465 capture; same shape in the #67 HEM-7155T-MW3 capture).
_REGISTRATION_SLOT_COUNT_OFFSET: int = 4      # u32 LE, steps once per transfer

# The device answers 0x8f00 for the session-close ack and for a rejected
# command alike, so it is accepted whatever was asked for.
END_OF_TRANSMISSION_PACKET_TYPE = bytes([0x8F, 0x00])


class MemoryProtocolMixin:
    """Notify channels, command/reply exchange and memory session over a Bleak client."""

    if TYPE_CHECKING:
        # Provided by the host session.
        _client: BleakClient | None
        _config: DeviceConfig
        _secure_session: SecureSession | None
        _unlocked: bool
        _pairing_session: bool
        address: str

        def _require_connected(self, context: str) -> None: ...
        async def _ensure_services_cache(self) -> None: ...
        def _debug_ble_link(self, tag: str) -> None: ...

    def _init_memory_protocol_state(self) -> None:
        self._notify_subscribed = False
        self._last_reply_packet_type: bytes | None = None
        self._last_reply_memory_address: bytes | None = None
        self._last_reply_payload: bytes | None = None
        self._expected_reply_packet_type: bytes | None = None
        self._expected_reply_memory_address: bytes | None = None
        self._reply_ready = asyncio.Event()
        self._channel_fragments: list[bytes | None] = [None] * 4
        self._notify_handle_to_channel: dict[int, int] = {}
        self._memory_session_active = False
        # A pairing session commits its registration once per link. The two
        # writes are tracked apart: the head write steps the cuff's transfer
        # count and must never repeat, the clock write can be redone.
        self._pairing_registration_head_done = False
        self._pairing_registration_clock_done = False

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

    async def _start_notify_with_recovery(
        self, uuid: str, callback: Any | None = None
    ) -> None:
        """Start notify with recovery for transient BlueZ/stack races."""
        handler = callback if callback is not None else self._on_notify_channel_data
        last_exc: BaseException | None = None
        for attempt in range(_NOTIFY_SUBSCRIBE_MAX_RETRIES):
            try:
                await self._client.start_notify(uuid, handler)
                return
            except BleakError as exc:
                last_exc = exc
                msg = str(exc).lower()
                # BlueZ can keep CCCD/notify acquired briefly after reconnect;
                # the ESPHome proxy backend reports the same state as
                # "notifications are already enabled". Either way, release the
                # stale subscription and re-subscribe.
                if (
                    "notify acquired" in msg
                    or "notpermitted" in msg
                    or "already enabled" in msg
                    # BlueZ's wording when it still holds the session from the
                    # previous connection, which keep_notify never released (#92).
                    or "register notify session" in msg
                ):
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

    async def _unsubscribe_notify_channels(self, *, force: bool = False) -> None:
        """Disable notifications on all RX channels.

        ``force`` overrides ``keep_notify_subscriptions`` for the paths that
        have to release a subscription to make progress — a failed session
        teardown and ``reset_session_state``. The normal session close does
        not, so profiles that ask leave the CCCD enabled the way the app does.
        """
        if self._config.keep_notify_subscriptions and not force:
            _LOGGER.debug(
                "RX notify left enabled for %s (profile keeps its subscriptions)",
                self._config.model,
            )
            return
        for uuid in self._config.rx_channel_uuids:
            try:
                await self._client.stop_notify(uuid)
            except Exception as exc:
                _LOGGER.debug("stop_notify for %s ignored: %s", uuid, exc)
        self._notify_subscribed = False
        self._debug_ble_link("after_rx_unsubscribe")

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

        if channel_index == 0:
            # Devices send notification channels sequentially (ch0, ch1, ...);
            # receiving ch0 signals the start of a new frame, so discard stale fragments.
            self._channel_fragments = [None] * 4
        self._channel_fragments[channel_index] = rx_bytes

        # Check if we can assemble a complete packet
        if not self._channel_fragments[0]:
            return

        if self._config.is_single_channel:
            frame_bytes = bytearray(self._channel_fragments[0])
            self._channel_fragments = [None] * 4
            # C0 is an encrypted envelope marker, not a 192-byte length.
            secure_envelope = (
                self._config.unlock_mode == UnlockMode.SECURE_SESSION
                and self._secure_session is not None
            )
            declared = frame_bytes[0] if frame_bytes and not secure_envelope else 0
            if declared and len(frame_bytes) < declared:
                _LOGGER.warning(
                    "Truncated BLE frame: declared %d bytes, received %d: %s",
                    declared,
                    len(frame_bytes),
                    _hex(frame_bytes),
                )
                return
            if declared:
                frame_bytes = frame_bytes[:declared]
        else:
            packet_size = self._channel_fragments[0][0]
            if packet_size == 0:
                _LOGGER.warning("Received zero-length BLE frame packet_size")
                self._channel_fragments = [None] * 4
                return
            if packet_size > 64:
                _LOGGER.warning(
                    "BLE frame packet_size %d exceeds 4-channel capacity (max 64 bytes)",
                    packet_size,
                )
                self._channel_fragments = [None] * 4
                return
            required_channels = range((packet_size + 15) // 16)
            # Check all required channels are received
            for ch in required_channels:
                if ch >= len(self._channel_fragments) or self._channel_fragments[ch] is None:
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
            if not frame_bytes or frame_bytes[0] != len(frame_bytes):
                _LOGGER.error("Invalid decrypted memory-frame length")
                return

        # The inner memory protocol retains its XOR checksum under CCM.
        xor_crc = 0
        for byte in frame_bytes:
            xor_crc ^= byte
        if xor_crc:
            _LOGGER.error(
                "CRC error in rx data: crc=%d, buffer=%s", xor_crc, _hex(frame_bytes)
            )
            return

        # Check minimum valid frame length (len(1) + type(2) + addr(2) + datalen(1) + rescode(1) + crc(1) = 8)
        if len(frame_bytes) < 8:
            _LOGGER.warning(
                "Received malformed or undersized BLE frame (%d bytes): %s",
                len(frame_bytes),
                _hex(frame_bytes),
            )
            return

        # Extract packet fields
        packet_type = bytes(frame_bytes[1:3])
        memory_address = bytes(frame_bytes[3:5])
        expected_data_len = frame_bytes[5]

        # Match on type and address, never the declared length (#148): every
        # memory read answers 0x8100 and only the address tells them apart.
        # 0x8f00 always passes -- it is both the close ack and the rejection.
        if packet_type != END_OF_TRANSMISSION_PACKET_TYPE:
            if (
                self._expected_reply_packet_type is not None
                and packet_type != self._expected_reply_packet_type
            ):
                _LOGGER.debug(
                    "Ignoring unexpected or late reply packet type %s (expected %s)",
                    _hex(packet_type),
                    _hex(self._expected_reply_packet_type),
                )
                return
            if (
                self._expected_reply_memory_address is not None
                and memory_address != self._expected_reply_memory_address
            ):
                _LOGGER.debug(
                    "Ignoring a late reply meant for another address: got %s, "
                    "waiting for %s",
                    _hex(memory_address),
                    _hex(self._expected_reply_memory_address),
                )
                return

        self._last_reply_packet_type = packet_type
        self._last_reply_memory_address = memory_address
        if packet_type == b"\x81\x00":
            # Memory block read: payload length in byte 5, payload at bytes 6..6+data_len
            if len(frame_bytes) < expected_data_len + 8:
                _LOGGER.warning(
                    "Truncated BLE read frame received (expected %d bytes payload, available %d): %s",
                    expected_data_len,
                    max(0, len(frame_bytes) - 8),
                    _hex(frame_bytes),
                )
                return
            self._last_reply_payload = bytes(frame_bytes[6:6 + expected_data_len])
        elif packet_type == b"\x8f\x00":
            # End-of-transmission packet: error code is in byte 6
            self._last_reply_payload = bytes(frame_bytes[6:7])
        else:
            # Control frame (e.g. 0x8000 session open, 0x81c0 write response): response code in byte 6
            self._last_reply_payload = bytes(frame_bytes[6:7])

        self._reply_ready.set()

    async def _write_command_and_wait_reply(
        self,
        command: bytearray,
        timeout: float = _MEMORY_PROTOCOL_REPLY_TIMEOUT_SEC,
    ) -> None:
        """Send a command and wait for response with retry logic."""
        # Derive expected reply packet type from plaintext command before optional encryption
        if len(command) >= 3:
            self._expected_reply_packet_type = bytes([command[1] | 0x80, command[2]])
        else:
            self._expected_reply_packet_type = None
        # The address a reply must echo back from the command asking for it.
        if len(command) >= 5:
            self._expected_reply_memory_address = bytes(command[3:5])
        else:
            self._expected_reply_memory_address = None

        plaintext = bytearray(command)
        max_retries = _MEMORY_PROTOCOL_TX_MAX_RETRIES
        try:
            for retry in range(max_retries):
                self._reply_ready.clear()

                if (
                    self._config.unlock_mode == UnlockMode.SECURE_SESSION
                    and self._secure_session is not None
                ):
                    # Encrypt per transmission: every CCM frame carries a
                    # counter the device will not accept twice, so a retry that
                    # replayed the first attempt's ciphertext would be refused
                    # and burn the whole retry budget without ever reaching the
                    # device's command handler.
                    try:
                        command = bytearray(
                            self._secure_session.encrypt(bytes(plaintext))
                        )
                    except Exception as exc:
                        _LOGGER.error(
                            "Secure session encryption failed for command: %s", exc
                        )
                        raise
                else:
                    command = plaintext

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
                    if (
                        self._expected_reply_packet_type is not None
                        and self._last_reply_packet_type == b"\x8f\x00"
                        and self._expected_reply_packet_type != b"\x8f\x00"
                    ):
                        code = (
                            self._last_reply_payload[0]
                            if self._last_reply_payload
                            else -1
                        )
                        raise ConnectionError(
                            f"Device rejected command {_hex(command[:3])} "
                            f"(error frame 0x8f00, code 0x{code:02x})"
                        )
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
        finally:
            self._expected_reply_packet_type = None
            self._expected_reply_memory_address = None

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
            if self._last_reply_payload and self._last_reply_payload[0]:
                raise ConnectionError(
                    f"Device rejected memory session open (error code 0x{self._last_reply_payload[0]:02x})"
                )
            self._memory_session_active = True
            self._debug_ble_link("open_memory_session_ok")
            _LOGGER.debug("Memory session opened for %s", self.address)
        except BaseException:
            self._memory_session_active = False
            self._unlocked = False
            self._debug_ble_link("open_memory_session_fail_cleanup")
            await self._unsubscribe_notify_channels(force=True)
            raise

    async def commit_pairing_registration(self) -> bool:
        """Write the settings mirror the app writes at the end of a pairing.

        A WLD3.0 cuff on the token-key transport accepts the bond it just made
        and then refuses to resume it on the next connection -- HCI 0x06, PIN
        or Key Missing -- unless the pairing session also wrote its mirror:
        the index region with every unread counter reset, the user's profile
        slot with its transfer count stepped and checksum redone, and the
        clock record stamped with the current time. The official app does all
        of it before its 080f close; hardware-verified on a BP5465 over local
        BlueZ, through a power cycle (#175). Which bytes are counters, where
        the slot sits and where the clock lives all come off the profile.

        Pairing sessions only, once per link, and after the records have been
        read: the unread counters are reset here. Returns whether it wrote.
        Never raises past a layout problem -- the close that follows matters
        more than this write, and a retained-bond session that ends without it
        is still a working session. The caller logs.
        """
        cfg = self._config
        registration = cfg.pairing_registration
        if (
            registration is None
            or not self._pairing_session
            or (
                self._pairing_registration_head_done
                and self._pairing_registration_clock_done
            )
        ):
            return False
        self._require_connected("commit_pairing_registration")
        if not self._memory_session_active:
            raise ConnectionError(
                "Pairing registration needs an open memory session"
            )
        layout = SettingsMirrorLayout(cfg)
        head_size = registration.slot_offset + registration.slot_size
        if layout.head_read_size < head_size:
            raise ConnectionError(
                f"Settings region of {cfg.model} is {layout.head_read_size} bytes; "
                f"the registration block needs {head_size}"
            )

        written: list[str] = []
        if not self._pairing_registration_head_done:
            await self._write_registration_head(layout, registration)
            written.append(f"settings mirror 0x{layout.head_write_address:04X}")
        if not self._pairing_registration_clock_done:
            await self._write_registration_clock(layout)
            written.append(f"clock 0x{layout.clock_write_address:04X}")
        _LOGGER.info(
            "%s: pairing registration written (%s)", cfg.model, ", ".join(written)
        )
        return True

    async def _write_registration_head(
        self, layout: SettingsMirrorLayout, registration: PairingRegistration
    ) -> None:
        """Index region with every unread counter reset, plus the stepped slot."""
        cfg = self._config
        slot = registration.slot_offset
        slot_size = registration.slot_size
        head_size = slot + slot_size
        head = await self.read_memory_range(
            layout.head_read_address, layout.head_read_size, cfg.transmission_block_size
        )
        if len(head) != layout.head_read_size:
            raise ConnectionError(
                f"Short settings read for registration: {len(head)} of "
                f"{layout.head_read_size}"
            )
        block = bytearray(head[:head_size])

        # Every stream's unread counter back to its idle marker: the records
        # have been read. Widths and bounds were validated when the profile
        # was built.
        for offset, width, idle in registration.unread_clears:
            block[offset : offset + width] = idle.to_bytes(width, "little")

        if block[slot : slot + 2] == b"\xff\xff":
            # Never written: the cuff treats a slot that starts 0xFFFF as empty
            # and skips its checksum. Stepping it would only make it look
            # populated without being one, so it goes back as it came.
            _LOGGER.warning(
                "%s: the user profile slot at +0x%02X is empty; the index is "
                "reset but the slot is left untouched, and the cuff may not "
                "resume this bond",
                cfg.model,
                slot,
            )
        else:
            count_at = slot + _REGISTRATION_SLOT_COUNT_OFFSET
            count = int.from_bytes(block[count_at : count_at + 4], "little")
            block[count_at : count_at + 4] = ((count + 1) & 0xFFFFFFFF).to_bytes(
                4, "little"
            )
            block[slot + slot_size - 2] = slot_checksum(
                block[slot : slot + slot_size], slot_size
            )
        await self.write_memory_range(
            layout.head_write_address, block, block_size=len(block)
        )
        # Armed as soon as the write is out: this is the half that steps the
        # cuff's transfer count and must not run twice on this link.
        self._pairing_registration_head_done = True

    async def _write_registration_clock(self, layout: SettingsMirrorLayout) -> None:
        """The clock record stamped with the current time, flag bit set.

        Tracked apart from the head so a failure here is retried on its own:
        a pairing session skips the measurement-completion mirror, so nothing
        else on this link sets that flag bit -- the EEPROM time sync keeps the
        record's leading bytes as read and only runs on drift.
        """
        cfg = self._config
        tail = await self.read_memory_range(
            layout.clock_read_address, layout.clock_read_size, cfg.transmission_block_size
        )
        if len(tail) < layout.clock_write_size:
            raise ConnectionError(
                f"Short clock read for registration: {len(tail)} of "
                f"{layout.clock_write_size}"
            )
        stamped = clock_block(
            tail, dt.datetime.now().astimezone(), layout.clock_write_size
        )
        await self.write_memory_range(
            layout.clock_write_address, bytearray(stamped), block_size=len(stamped)
        )
        self._pairing_registration_clock_done = True

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
