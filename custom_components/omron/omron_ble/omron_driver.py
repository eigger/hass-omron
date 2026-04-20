from __future__ import annotations

import asyncio
import datetime as dt
import logging
import traceback
from typing import Any

from bleak import BleakClient

from .devices import DeviceConfig, bytearray_bits_to_int

_LOGGER = logging.getLogger(__name__)

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
    """Unlock notify: key programming mode ready (e.g. HEM-7150T-Z uses 8208, others 8200)."""
    if resp is None or len(resp) < 2:
        return False
    return resp[0] == 0x82 and resp[1] in (0x00, 0x08)


def _is_unlock_pairing_key_ack(resp: bytes | bytearray | None) -> bool:
    """Unlock notify: new pairing key accepted (e.g. HEM-7150T-Z uses 8004, omblepy 8000)."""
    if resp is None or len(resp) < 2:
        return False
    return resp[0] == 0x80 and resp[1] in (0x00, 0x04)


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

    def _rebuild_notify_handle_index_map(self) -> None:
        """Build mapping from GATT characteristic handles to notify channel indices."""
        self._notify_handle_to_channel = {}
        for idx, uuid in enumerate(self._config.rx_channel_uuids):
            char = self._client.services.get_characteristic(uuid)
            if char is not None:
                self._notify_handle_to_channel[char.handle] = idx

    async def _subscribe_notify_channels(self) -> None:
        """Enable notifications on all RX channels with retry and graceful error handling.

        A failed descriptor write is caught,
        retried up to _NOTIFY_RETRIES times with a short delay, and if still failing the
        channel is skipped with a warning rather than raising immediately.  This avoids
        crashing the entire session for a transient GATT error (e.g. error 259 via
        ESPHome BLE proxy) on one of the notify channels.
        """
        _NOTIFY_RETRIES = 3
        _NOTIFY_RETRY_DELAY = 1.0  # seconds between retries

        if self._notify_subscribed:
            return

        self._rebuild_notify_handle_index_map()
        failed_uuids: list[str] = []

        for uuid in self._config.rx_channel_uuids:
            last_exc: Exception | None = None
            for attempt in range(1, _NOTIFY_RETRIES + 1):
                try:
                    await self._client.start_notify(uuid, self._on_notify_channel_data)
                    _LOGGER.debug("Subscribed notify channel %s", uuid)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    _LOGGER.debug(
                        "start_notify failed for %s (attempt %d/%d): %s",
                        uuid, attempt, _NOTIFY_RETRIES, exc,
                    )
                    if attempt < _NOTIFY_RETRIES:
                        await asyncio.sleep(_NOTIFY_RETRY_DELAY)

            if last_exc is not None:
                failed_uuids.append(uuid)
                _LOGGER.warning(
                    "Could not subscribe notify channel %s after %d attempts: %s - "
                    "channel will be skipped (data may be incomplete)",
                    uuid, _NOTIFY_RETRIES, last_exc,
                )

        # If ALL channels failed, the session cannot proceed - raise the last error.
        if failed_uuids and len(failed_uuids) == len(self._config.rx_channel_uuids):
            raise ConnectionError(
                f"Failed to enable notifications on any RX channel: {failed_uuids}"
            )

        self._notify_subscribed = True

    async def _unsubscribe_notify_channels(self) -> None:
        """Disable notifications on all RX channels."""
        if self._notify_subscribed:
            for uuid in self._config.rx_channel_uuids:
                try:
                    await self._client.stop_notify(uuid)
                except Exception as exc:
                    _LOGGER.debug("stop_notify for %s ignored: %s", uuid, exc)
            self._notify_subscribed = False

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
        _LOGGER.debug("rx ch%d < %s", channel_index, _hex(rx_bytes))

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
        self, command: bytearray, timeout: float = 2.0
    ) -> None:
        """Send a command and wait for response with retry logic."""
        for retry in range(5):
            self._reply_ready.clear()

            # Split command across TX channels
            remaining_cmd = command
            channel_width = 16
            if self._config.is_single_channel:
                channel_width = max(channel_width, len(command))

            num_tx_channels = (len(command) + channel_width - 1) // channel_width
            for ch_idx in range(num_tx_channels):
                tx_segment = remaining_cmd[:channel_width]
                _LOGGER.debug("tx ch%d > %s", ch_idx, _hex(tx_segment))
                if self._config.is_single_channel:
                    await self._client.write_gatt_char(
                        self._config.tx_channel_uuids[ch_idx], tx_segment, response=False
                    )
                else:
                    await self._client.write_gatt_char(
                        self._config.tx_channel_uuids[ch_idx], tx_segment
                    )
                remaining_cmd = remaining_cmd[channel_width:]

            # Wait for response
            try:
                await asyncio.wait_for(self._reply_ready.wait(), timeout=timeout)
                return  # Success
            except asyncio.TimeoutError:
                _LOGGER.warning("TX timeout, retry %d/5", retry + 1)

        raise ConnectionError("Failed to receive response after 5 retries")

    async def open_memory_session(self) -> None:
        """Start a data readout session."""
        await self._subscribe_notify_channels()
        # Build init command dynamically: [len, cmd_hi, cmd_lo, 0, 0, block_size, pad, crc]
        block_size = self._config.transmission_block_size
        cmd = bytearray([0x08, 0x00, 0x00, 0x00, 0x00, block_size])
        xor_crc = 0
        for byte in cmd:
            xor_crc ^= byte
        cmd += b'\x00'
        cmd.append(xor_crc)
        await self._write_command_and_wait_reply(cmd)
        if self._last_reply_packet_type != bytearray.fromhex("8000"):
            raise ConnectionError("Invalid response to data readout start")

    async def close_memory_session(self) -> None:
        """End a data readout session."""
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
            _LOGGER.debug("read %#06x size %#04x", start_address, chunk_size)
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
            _LOGGER.debug("write %#06x size %#04x", start_address, chunk_size)
            await self.write_memory_block(start_address, data[:chunk_size])
            data = data[chunk_size:]
            start_address += chunk_size

    async def unlock(self, key: bytearray | None = None) -> None:
        """Unlock device with pairing key."""
        if not self._config.requires_unlock:
            return

        unlock_key = key or PAIRING_KEY
        unlock_event = asyncio.Event()
        response_holder: list[bytes | None] = [None]

        def _unlock_callback(_: Any, rx_bytes: bytearray) -> None:
            response_holder[0] = rx_bytes
            unlock_event.set()

        await self._client.start_notify(self._config.unlock_uuid, _unlock_callback)
        try:
            unlock_event.clear()
            await self._client.write_gatt_char(
                self._config.unlock_uuid, b'\x01' + unlock_key, response=True
            )
            await asyncio.wait_for(unlock_event.wait(), timeout=5.0)

            response = response_holder[0]
            if response is None or response[:2] != bytearray.fromhex("8100"):
                raise ConnectionError("Unlock failed: pairing key mismatch")
        finally:
            await self._client.stop_notify(self._config.unlock_uuid)

    async def pair(self, key: bytearray | None = None) -> None:
        """Program a new pairing key into the device.

        The device must be in pairing mode (hold bluetooth button until -P- blinks).
        For OS-bonding-only devices (e.g. HEM-7380T1), performs standard BLE pairing.
        """
        pair_key = key or PAIRING_KEY

        # OS-level bonding only (HEM-7380T1 etc.)
        if self._config.supports_os_bonding_only:
            _LOGGER.info("Performing OS-level BLE bonding")
            # Some stacks fail pair() even when already bonded/encrypted sessions work.
            # Keep this as best-effort and allow caller to proceed to GATT operations.
            max_attempts = 2
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    if attempt > 1:
                        await asyncio.sleep(0.5)
                        await _bleak_refresh_services(self._client)
                    try:
                        await self._client.pair()
                    except TypeError:
                        await self._client.pair(protection_level=2)
                    _LOGGER.info("OS-level BLE bonding completed")
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
            unlock_attempts, unlock_retry_delay = 30, 0.5
            key_max_retries = 10
        else:
            unlock_attempts, unlock_retry_delay = 15, 1.0
            key_max_retries = 15

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
                "expected_notify_prefix=8200_or_8208 last_notify_hex=%s samples=%s",
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

        await self._client.stop_notify(self._config.unlock_uuid)
        await self._client.stop_notify(self._config.rx_channel_uuids[0])
        _LOGGER.info("Device paired successfully with new key")

        # Step 4: Initial handshake (required after first pairing)
        try:
            await self.open_memory_session()
            await self.close_memory_session()
        except Exception as exc:
            _LOGGER.warning("Post-pairing handshake failed (may be normal): %s", exc)


class OmronDeviceDriver:
    """High-level driver for reading records from Omron blood pressure monitors.

    Uses DeviceConfig for device-specific behavior.
    """

    def __init__(self, config: DeviceConfig) -> None:
        self._config = config
        self._cached_settings: bytearray | None = None
        self._now_func = dt.datetime.now
        self._counter_probe_logged = False

    async def _probe_7142_counter_candidates(
        self, transport: GattTransport
    ) -> None:
        """Log potential unread-counter regions for HEM-7142T2 analysis."""
        if not self._config.use_layout_fallback_scan or self._counter_probe_logged:
            return
        # Only run when debug logging is enabled to avoid noisy logs.
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return

        probe_regions = [
            (0x0010, 0x10, "b-format settings block"),
            (0x0054, 0x10, "b-format settings write block"),
            (0x0260, 0x10, "a-format settings block"),
            (0x0286, 0x10, "a-format settings write block"),
        ]
        try:
            for addr, size, label in probe_regions:
                raw = await transport.read_memory_range(addr, size, size)
                hex_raw = bytes(raw).hex()
                _LOGGER.debug(
                    "7142 counter-probe %s addr=%#06x size=%#04x raw=%s",
                    label,
                    addr,
                    size,
                    hex_raw,
                )
                if len(raw) >= 8:
                    # Try index-based interpretation:
                    # [0:2],[2:4] -> lastWrittenSlot user1/2
                    # [4:6],[6:8] -> unread user1/2
                    lw_u1_le = int.from_bytes(raw[0:2], "little")
                    lw_u2_le = int.from_bytes(raw[2:4], "little")
                    ur_u1_le = int.from_bytes(raw[4:6], "little")
                    ur_u2_le = int.from_bytes(raw[6:8], "little")
                    lw_u1_be = int.from_bytes(raw[0:2], "big")
                    lw_u2_be = int.from_bytes(raw[2:4], "big")
                    ur_u1_be = int.from_bytes(raw[4:6], "big")
                    ur_u2_be = int.from_bytes(raw[6:8], "big")
                    _LOGGER.debug(
                        "7142 counter-probe decoded addr=%#06x "
                        "LE(last_slot_u1=%d,u2=%d unread_u1=%d,u2=%d) "
                        "BE(last_slot_u1=%d,u2=%d unread_u1=%d,u2=%d)",
                        addr,
                        lw_u1_le,
                        lw_u2_le,
                        ur_u1_le,
                        ur_u2_le,
                        lw_u1_be,
                        lw_u2_be,
                        ur_u1_be,
                        ur_u2_be,
                    )
            self._counter_probe_logged = True
        except Exception as exc:
            _LOGGER.debug("7142 counter-probe skipped: %s", exc)

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

            await transport.close_memory_session()
        except Exception:
            # Try to cleanly end if possible
            try:
                await transport.close_memory_session()
            except Exception:
                pass
            raise

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
            _LOGGER.info(
                "Index path returned no valid candidate for model=%s; skipping full scan fallback",
                self._config.model,
            )
            return None
        if self._config.enable_index_debug_logs:
            _LOGGER.info(
                "%s index path did not yield a valid latest record; falling back to full scan",
                self._config.model,
            )
        _LOGGER.debug(
            "Falling back to full scan latest selection for model=%s",
            self._config.model,
        )
        return await self._get_latest_via_full_scan(transport)

    async def get_latest_records_per_user(
        self, transport: GattTransport
    ) -> dict[int, dict[str, Any]]:
        """Return latest valid record per configured user index (1-based)."""
        if self._config.use_layout_fallback_scan:
            all_user_records = await self._get_all_records_with_format_c_fallback(transport)
        else:
            all_user_records = await self.get_all_records(transport)

        latest_by_user: dict[int, dict[str, Any]] = {}
        for user_idx, user_records in enumerate(all_user_records):
            user = user_idx + 1
            if not user_records:
                continue
            selected = self._select_latest_candidate([(user, rec) for rec in user_records])
            if selected is None:
                continue
            _, record = selected
            result = dict(record)
            result["user"] = user
            result.pop("_slot_index", None)
            result.pop("_offset", None)
            latest_by_user[user] = result
        return latest_by_user

    async def _get_latest_via_full_scan(
        self, transport: GattTransport
    ) -> dict[str, Any] | None:
        """Existing full EEPROM scan path."""
        if self._config.use_layout_fallback_scan:
            all_user_records = await self._get_all_records_with_format_c_fallback(transport)
        else:
            all_user_records = await self.get_all_records(transport)
        candidates: list[tuple[int, dict[str, Any]]] = []
        for user_idx, user_records in enumerate(all_user_records):
            for record in user_records:
                candidates.append((user_idx + 1, record))

        if self._config.enable_index_debug_logs:
            self._log_top_candidates(candidates)

        selected = self._select_latest_candidate(candidates)
        if selected is None:
            return None
        user, record = selected
        result = dict(record)
        result["user"] = user
        result.pop("_slot_index", None)
        result.pop("_offset", None)
        return result

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
        self, transport: GattTransport
    ) -> dict[str, Any] | None:
        """Read index block and fetch only the latest slot per configured user."""
        layout = self._config.index_pointer_layout
        if (
            layout is None
            or self._config.settings_read_address is None
            or self._config.record_byte_size <= 0
        ):
            return None

        index_region_byte_size = int(layout.get("index_region_byte_size", 0))
        user_layouts = layout.get("users", [])
        if index_region_byte_size <= 0 or not isinstance(user_layouts, list) or not user_layouts:
            return None

        record_addresses = layout.get("record_addresses") or self._config.user_start_addresses
        record_byte_size = int(layout.get("record_byte_size", self._config.record_byte_size))
        record_step = int(layout.get("record_step", record_byte_size))
        backtrack_slots = int(layout.get("backtrack_slots", 0))
        collect_all_valid = bool(layout.get("collect_all_valid_in_index_window", False))
        ptr_endian = str(layout.get("endianness", self._config.endianness))

        candidates: list[tuple[int, dict[str, Any]]] = []
        if self._config.enable_index_debug_logs:
            _LOGGER.info(
                "%s index path start: read_addr=%#06x size=%d record_size=%d record_step=%d",
                self._config.model,
                self._config.settings_read_address,
                index_region_byte_size,
                record_byte_size,
                record_step,
            )
        await transport.unlock()
        await transport.open_memory_session()
        try:
            index_bytes = await transport.read_memory_range(
                self._config.settings_read_address,
                index_region_byte_size,
                self._config.transmission_block_size,
            )
            if self._config.enable_index_debug_logs:
                _LOGGER.info("%s index raw=%s", self._config.model, bytes(index_bytes).hex())
            for idx, user_cfg in enumerate(user_layouts):
                if idx >= len(record_addresses) or idx >= len(self._config.per_user_records_count):
                    continue
                write_cursor_offset = int(user_cfg.get("write_cursor_offset", -1))
                if write_cursor_offset < 0 or write_cursor_offset + 2 > len(index_bytes):
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
                    if self._config.enable_index_debug_logs:
                        _LOGGER.info(
                            "%s index out_of_range: user=%d raw=%d masked=%d corrected=%d range=[%d,%d]",
                            self._config.model,
                            idx + 1,
                            raw_pointer,
                            pointer_masked,
                            pointer_corrected,
                            pointer_min,
                            pointer_max,
                        )
                    continue
                record_count = (pointer_max - pointer_min) + 1
                if record_count <= 0:
                    continue
                latest_slot = pointer_wrapped
                max_probe = min(max(backtrack_slots, 0), max(record_count - 1, 0))
                parsed = None
                found_valid_for_user = False
                base_addr = int(record_addresses[idx])
                logged_first_raw_fail = False
                empty_slot_count = 0
                parse_error_count = 0
                plausibility_reject_count = 0
                valid_candidate_count = 0
                for back in range(max_probe + 1):
                    probe_slot = latest_slot - back
                    while probe_slot < pointer_min:
                        probe_slot += record_count
                    logical_slot = probe_slot - pointer_min
                    probe_addr = base_addr + (logical_slot * record_step)
                    if self._config.enable_index_debug_logs and back == 0:
                        _LOGGER.info(
                            "%s index mapped: user=%d raw=%d masked=%d corrected=%d wrapped=%d slot=%d addr=%#06x",
                            self._config.model,
                            idx + 1,
                            raw_pointer,
                            pointer_masked,
                            pointer_corrected,
                            pointer_wrapped,
                            probe_slot,
                            probe_addr,
                        )
                    raw_record = await transport.read_memory_range(
                        probe_addr,
                        record_byte_size,
                        self._config.transmission_block_size,
                    )
                    try:
                        parsed = self._config.parse_record(bytes(raw_record))
                    except Exception as exc:
                        _LOGGER.debug(
                            "Index-targeted parse failed model=%s user=%d slot=%d addr=%#06x: %s",
                            self._config.model,
                            idx + 1,
                            probe_slot,
                            probe_addr,
                            exc,
                        )
                        if self._config.enable_index_debug_logs:
                            reason = "empty_slot" if "empty" in str(exc).lower() else "parse_error"
                            if reason == "empty_slot":
                                empty_slot_count += 1
                            else:
                                parse_error_count += 1
                            if not logged_first_raw_fail:
                                _LOGGER.info(
                                    "%s index %s: user=%d slot=%d addr=%#06x raw=%s err=%s",
                                    self._config.model,
                                    reason,
                                    idx + 1,
                                    probe_slot,
                                    probe_addr,
                                    bytes(raw_record).hex(),
                                    exc,
                                )
                                logged_first_raw_fail = True
                        parsed = None
                        continue
                    parsed["_slot_index"] = probe_slot
                    if not self._is_record_plausible(parsed):
                        plausibility_reject_count += 1
                        if self._config.enable_index_debug_logs:
                            _LOGGER.debug(
                                "%s index plausibility_reject user=%d slot=%d parsed=%s",
                                self._config.model,
                                idx + 1,
                                probe_slot,
                                {
                                    "datetime": parsed.get("datetime"),
                                    "sys": parsed.get("sys"),
                                    "dia": parsed.get("dia"),
                                    "bpm": parsed.get("bpm"),
                                },
                            )
                        parsed = None
                        continue
                    candidates.append((idx + 1, parsed))
                    found_valid_for_user = True
                    valid_candidate_count += 1
                    if not collect_all_valid:
                        break
                    parsed = None
                if (
                    found_valid_for_user
                    and self._config.enable_index_debug_logs
                    and valid_candidate_count > 0
                ):
                    _LOGGER.info(
                        "%s index probe summary: user=%d valid_candidates=%d",
                        self._config.model,
                        idx + 1,
                        valid_candidate_count,
                    )
                if (
                    not found_valid_for_user
                    and self._config.enable_index_debug_logs
                    and (empty_slot_count or parse_error_count or plausibility_reject_count)
                ):
                    _LOGGER.info(
                        "%s index probe summary: user=%d empty_slot=%d parse_error=%d plausibility_reject=%d",
                        self._config.model,
                        idx + 1,
                        empty_slot_count,
                        parse_error_count,
                        plausibility_reject_count,
                    )
        except Exception as exc:
            _LOGGER.debug(
                "Index-based latest read failed for model=%s: %s",
                self._config.model,
                exc,
            )
            if self._config.enable_index_debug_logs:
                _LOGGER.info("%s index path exception: %s", self._config.model, exc)
            return None
        finally:
            try:
                await transport.close_memory_session()
            except Exception:
                pass

        selected = self._select_latest_candidate(candidates)
        if selected is None:
            if self._config.enable_index_debug_logs:
                _LOGGER.info("%s index path produced no valid candidate (index_empty)", self._config.model)
            return None
        user, record = selected
        result = dict(record)
        result["user"] = user
        result.pop("_slot_index", None)
        result.pop("_offset", None)
        _LOGGER.debug(
            "Index-based latest selected for model=%s user=%d datetime=%s sys=%s dia=%s bpm=%s",
            self._config.model,
            user,
            result.get("datetime"),
            result.get("sys"),
            result.get("dia"),
            result.get("bpm"),
        )
        return result

    def _log_top_candidates(
        self, candidates: list[tuple[int, dict[str, Any]]], limit: int = 8
    ) -> None:
        """Log top candidate records for troubleshooting latest selection."""
        if not candidates:
            _LOGGER.debug("Top candidates: none")
            return
        ranked = sorted(
            candidates,
            key=lambda item: (
                item[1].get("_record_id", -1),
                item[1].get("_slot_index", -1),
                item[1].get("datetime", dt.datetime.min),
            ),
            reverse=True,
        )[:limit]
        _LOGGER.debug(
            "Top %d candidates (user, slot, record_id, datetime, sys, dia, bpm): %s",
            len(ranked),
            [
                (
                    user,
                    rec.get("_slot_index"),
                    rec.get("_record_id"),
                    rec.get("datetime"),
                    rec.get("sys"),
                    rec.get("dia"),
                    rec.get("bpm"),
                )
                for user, rec in ranked
            ],
        )

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
                    _LOGGER.debug(
                        "Skipping implausible record for user%d at offset %d: %s",
                        user_idx + 1,
                        offset,
                        {
                            "datetime": record.get("datetime"),
                            "sys": record.get("sys"),
                            "dia": record.get("dia"),
                            "bpm": record.get("bpm"),
                        },
                    )
                    continue
                records.append(record)
            except ValueError as exc:
                # Many devices leave partially initialized slots (not always all 0xFF).
                # Treat parse-level ValueError as an empty/invalid slot and skip quietly.
                _LOGGER.debug(
                    "Skipping invalid/empty record for user%d at offset %d (data: %s): %s",
                    user_idx + 1,
                    offset,
                    _hex(single),
                    exc,
                )
            except Exception as exc:
                _LOGGER.warning(
                    "Error parsing record for user%d at offset %d (data: %s): %s",
                    user_idx + 1, offset, _hex(single), exc,
                )
        return records

    def _parse_user_records_best_alignment(
        self,
        raw_data: bytearray,
        user_idx: int,
        record_byte_size: int,
    ) -> list[dict[str, Any]]:
        """Parse records and pick best byte alignment for 7142 format-c dumps."""
        if not self._config.use_layout_fallback_scan or record_byte_size != 0x10:
            return self._parse_user_records(raw_data, user_idx, record_byte_size=record_byte_size)

        best_records: list[dict[str, Any]] = []
        best_shift = 0
        best_score: tuple[int, dt.datetime, int, int] | None = None
        for shift in range(record_byte_size):
            shifted = raw_data[shift:]
            usable_len = len(shifted) - (len(shifted) % record_byte_size)
            if usable_len <= 0:
                continue
            records = self._parse_user_records(
                bytearray(shifted[:usable_len]),
                user_idx,
                record_byte_size=record_byte_size,
            )
            if not records:
                continue
            latest_dt = max(
                (
                    rec.get("datetime")
                    for rec in records
                    if isinstance(rec.get("datetime"), dt.datetime)
                ),
                default=dt.datetime.min,
            )
            best_record_id = max(int(rec.get("_record_id", -1)) for rec in records)
            score = (
                1 if latest_dt != dt.datetime.min else 0,
                latest_dt,
                best_record_id,
                len(records),
            )
            if best_score is None or score > best_score:
                best_score = score
                best_shift = shift
                best_records = records

        if best_score is not None and best_shift > 0:
            # Rebase offsets/slots to original raw_data coordinates.
            for rec in best_records:
                rec["_offset"] = int(rec.get("_offset", 0)) + best_shift
                rec["_slot_index"] = int(rec["_offset"]) // record_byte_size
            _LOGGER.info(
                "HEM-7142T2 alignment selected shift=%d for user%d (valid=%d latest=%s)",
                best_shift,
                user_idx + 1,
                len(best_records),
                max(
                    (
                        rec.get("datetime")
                        for rec in best_records
                        if isinstance(rec.get("datetime"), dt.datetime)
                    ),
                    default=None,
                ),
            )
        # Supplemental scan for 7142:
        # Some dumps appear slightly misaligned in a way constant chunking misses.
        # Scan every byte window and collect additional plausible records.
        if len(best_records) < 6:
            found_by_sig: set[tuple[Any, ...]] = set()
            merged: list[dict[str, Any]] = []
            for rec in best_records:
                sig = (
                    rec.get("_record_id"),
                    rec.get("datetime"),
                    rec.get("sys"),
                    rec.get("dia"),
                    rec.get("bpm"),
                )
                found_by_sig.add(sig)
                merged.append(rec)

            for offset in range(0, max(0, len(raw_data) - record_byte_size + 1)):
                window = raw_data[offset:offset + record_byte_size]
                if window == (b"\xff" * record_byte_size):
                    continue
                try:
                    rec = self._config.parse_record(window)
                except Exception:
                    continue
                rec["_offset"] = offset
                rec["_slot_index"] = offset // record_byte_size
                if not self._is_record_plausible(rec):
                    continue
                sig = (
                    rec.get("_record_id"),
                    rec.get("datetime"),
                    rec.get("sys"),
                    rec.get("dia"),
                    rec.get("bpm"),
                )
                if sig in found_by_sig:
                    continue
                found_by_sig.add(sig)
                merged.append(rec)

            if len(merged) > len(best_records):
                _LOGGER.info(
                    "HEM-7142T2 supplemental scan added %d plausible records for user%d",
                    len(merged) - len(best_records),
                    user_idx + 1,
                )
                return merged
        return best_records

    def _select_latest_candidate(
        self, candidates: list[tuple[int, dict[str, Any]]]
    ) -> tuple[int, dict[str, Any]] | None:
        """Choose the latest record across users using model-specific strategy."""
        if not candidates:
            return None

        strategy = self._config.latest_selection_strategy
        if strategy == "record_id_slot_datetime":
            _LOGGER.debug("Selecting latest record using record_id_slot_datetime strategy")
            # Some devices prefer recent datetime candidate over raw record_id order.
            if self._config.use_layout_fallback_scan:
                now = self._now_func()
                recent_dt_candidates = [
                    item
                    for item in candidates
                    if isinstance(item[1].get("datetime"), dt.datetime)
                    and (now - dt.timedelta(days=30)) <= item[1]["datetime"] <= (now + dt.timedelta(days=2))
                ]
                if recent_dt_candidates:
                    selected_by_datetime = max(
                        recent_dt_candidates,
                        key=lambda item: (
                            item[1].get("datetime", dt.datetime.min),
                            item[1].get("_slot_index", -1),
                            item[1].get("_record_id", -1),
                        ),
                    )
                    _LOGGER.info(
                        "%s latest selector: using recent datetime candidate "
                        "(dt=%s sys=%s dia=%s bpm=%s record_id=%s slot=%s)",
                        self._config.model,
                        selected_by_datetime[1].get("datetime"),
                        selected_by_datetime[1].get("sys"),
                        selected_by_datetime[1].get("dia"),
                        selected_by_datetime[1].get("bpm"),
                        selected_by_datetime[1].get("_record_id"),
                        selected_by_datetime[1].get("_slot_index"),
                    )
                    return selected_by_datetime
            return max(
                candidates,
                key=lambda item: (
                    item[1].get("_record_id", -1),
                    item[1].get("_slot_index", -1),
                    item[1].get("datetime", dt.datetime.min),
                ),
            )
        if strategy == "slot_desc_datetime":
            _LOGGER.debug("Selecting latest record using slot_desc_datetime strategy")
            return max(
                candidates,
                key=lambda item: (
                    item[1].get("_slot_index", -1),
                    item[1].get("datetime", dt.datetime.min),
                ),
            )

        _LOGGER.debug("Selecting latest record using datetime strategy")
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
        allow_missing_datetime = (
            self._config.use_layout_fallback_scan
            and self._config.record_parser == "format_c_7142"
        )
        if not isinstance(date_value, dt.datetime):
            if not allow_missing_datetime:
                return False

        if isinstance(date_value, dt.datetime):
            now = self._now_func()
            if date_value < dt.datetime(2010, 1, 1):
                return False
            if date_value > (now + dt.timedelta(days=2)):
                return False

        sys = record.get("sys")
        dia = record.get("dia")
        bpm = record.get("bpm")
        if not isinstance(sys, int) or not isinstance(dia, int) or not isinstance(bpm, int):
            return False
        if not (60 <= sys <= 280):
            return False
        if not (30 <= dia <= 180):
            return False
        if not (30 <= bpm <= 240):
            return False
        if dia >= sys:
            return False
        return True

    async def _get_all_records_with_format_c_fallback(
        self, transport: GattTransport
    ) -> list[list[dict[str, Any]]]:
        """Try EEPROM-aligned layout candidates with early-stop to limit retries."""
        default_size = self._config.record_byte_size
        alt_size = 0x0E if default_size == 0x10 else 0x10
        # Keep attempts intentionally short: configured layout first, then minimal fallbacks.
        attempts = [
            (self._config.user_start_addresses, self._config.per_user_records_count, default_size),
            ([0x01C4], [100], default_size),
            ([0x0804], [100], default_size),
            (self._config.user_start_addresses, self._config.per_user_records_count, alt_size),
        ]
        if self._config.use_layout_fallback_scan:
            # Keep extended-range probes only as low-priority fallbacks.
            # Latest records were not found there during testing.
            attempts.extend(
                [
                    ([0x01C4 + (100 * default_size)], [40], default_size),
                    ([0x0804 + (100 * default_size)], [40], default_size),
                ]
            )

        await transport.unlock()
        await transport.open_memory_session()
        try:
            await self._probe_7142_counter_candidates(transport)
            best_records: list[list[dict[str, Any]]] | None = None
            best_latest: dt.datetime | None = None
            best_layout: tuple[list[int], list[int], int] | None = None
            best_valid_count = -1
            best_score: tuple[int, int, dt.datetime, int] | None = None

            for attempt_idx, (starts, counts, record_size) in enumerate(attempts, start=1):
                all_user_records: list[list[dict[str, Any]]] = []
                for user_idx, (start_addr, count) in enumerate(zip(starts, counts)):
                    total_bytes = count * record_size
                    raw_data = await transport.read_memory_range(
                        start_addr, total_bytes, self._config.transmission_block_size
                    )
                    records = self._parse_user_records_best_alignment(
                        raw_data, user_idx, record_byte_size=record_size
                    )
                    all_user_records.append(records)

                if not any(all_user_records):
                    continue

                candidates: list[tuple[int, dict[str, Any]]] = []
                for user_idx, user_records in enumerate(all_user_records):
                    for record in user_records:
                        candidates.append((user_idx + 1, record))
                selected = self._select_latest_candidate(candidates)
                if selected is None:
                    continue

                _, selected_record = selected
                selected_dt = selected_record.get("datetime")
                slot_index = int(selected_record.get("_slot_index", -1))
                record_id = int(selected_record.get("_record_id", -1))
                selected_dt_safe = (
                    selected_dt if isinstance(selected_dt, dt.datetime) else dt.datetime.min
                )
                valid_count = len(candidates)
                if self._config.use_layout_fallback_scan:
                    # Prefer layout whose selected record has the newest plausible datetime.
                    # record_id remains a secondary tie-breaker.
                    score = (
                        1 if isinstance(selected_dt, dt.datetime) else 0,
                        selected_dt_safe,
                        record_id,
                        slot_index,
                        valid_count,
                    )
                else:
                    score = (
                        valid_count,
                        1 if isinstance(selected_dt, dt.datetime) else 0,
                        selected_dt_safe,
                        slot_index,
                    )
                _LOGGER.debug(
                    "Layout candidate (attempt %d/%d) starts=%s counts=%s record_size=0x%02x valid=%d latest=%s slot=%s record_id=%s values=%s/%s/%s",
                    attempt_idx,
                    len(attempts),
                    [f"{addr:#06x}" for addr in starts],
                    counts,
                    record_size,
                    valid_count,
                    selected_dt if isinstance(selected_dt, dt.datetime) else "None",
                    slot_index,
                    record_id,
                    selected_record.get("sys"),
                    selected_record.get("dia"),
                    selected_record.get("bpm"),
                )
                if best_score is None or score > best_score:
                    best_score = score
                    best_valid_count = valid_count
                    best_latest = (
                        selected_dt if isinstance(selected_dt, dt.datetime) else None
                    )
                    best_records = all_user_records
                    best_layout = (starts, counts, record_size)

                # Early-stop: once we got a strong latest signal, avoid excessive scans.
                has_recent_datetime = isinstance(selected_dt, dt.datetime) and (
                    selected_dt >= (self._now_func() - dt.timedelta(days=30))
                )
                # Avoid premature lock-in when only a tiny number of valid rows were found.
                # This keeps retries low but still gives one more chance to alternate mapping.
                if (
                    not self._config.use_layout_fallback_scan
                    and record_id > 0
                    and has_recent_datetime
                    and valid_count >= 5
                ):
                    _LOGGER.info(
                        "Early-stop fallback scan for %s at attempt %d/%d (record_id=%d latest=%s)",
                        self._config.model,
                        attempt_idx,
                        len(attempts),
                        record_id,
                        selected_dt,
                    )
                    break

            await transport.close_memory_session()
            if best_records is not None and best_layout is not None:
                starts, counts, record_size = best_layout
                _LOGGER.info(
                    "Recovered best records for %s using layout starts=%s counts=%s record_size=0x%02x valid=%d latest=%s",
                    self._config.model,
                    [f"{addr:#06x}" for addr in starts],
                    counts,
                    record_size,
                    best_valid_count,
                    best_latest,
                )
                return best_records
            return [[]]
        except Exception:
            try:
                await transport.close_memory_session()
            except Exception:
                pass
            raise
