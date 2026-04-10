from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from bleak import BleakClient

from .devices import DeviceConfig, bytearray_bits_to_int

_LOGGER = logging.getLogger(__name__)

PAIRING_KEY = bytearray.fromhex("deadbeaf12341234deadbeaf12341234")


def _hex(data: bytes | bytearray) -> str:
    """Convert byte array to hex string."""
    return bytes(data).hex()


class BluetoothTxRxHandler:
    """Handles BLE GATT TX/RX communication with Omron devices.

    Supports both single-channel (HEM-7380T1) and multi-channel (legacy) devices.
    """

    def __init__(self, client: BleakClient, device_config: DeviceConfig) -> None:
        self._client = client
        self._config = device_config
        self._rx_notify_active = False
        self._rx_packet_type: bytes | None = None
        self._rx_eeprom_address: bytes | None = None
        self._rx_data_bytes: bytes | None = None
        self._rx_finished = asyncio.Event()
        self._rx_raw_channel_buffer: list[bytes | None] = [None] * 4
        self._rx_handle_to_channel: dict[int, int] = {}

    def _build_rx_handle_map(self) -> None:
        """Build mapping from GATT characteristic handles to channel indices."""
        self._rx_handle_to_channel = {}
        for idx, uuid in enumerate(self._config.rx_channel_uuids):
            char = self._client.services.get_characteristic(uuid)
            if char is not None:
                self._rx_handle_to_channel[char.handle] = idx

    async def _enable_rx_notify(self) -> None:
        """Enable notifications on all RX channels."""
        if not self._rx_notify_active:
            self._build_rx_handle_map()
            for uuid in self._config.rx_channel_uuids:
                await self._client.start_notify(uuid, self._rx_callback)
            self._rx_notify_active = True

    async def _disable_rx_notify(self) -> None:
        """Disable notifications on all RX channels."""
        if self._rx_notify_active:
            for uuid in self._config.rx_channel_uuids:
                await self._client.stop_notify(uuid)
            self._rx_notify_active = False

    def _rx_callback(self, char: Any, rx_bytes: bytearray) -> None:
        """Callback for received BLE notifications. Reassembles multi-channel packets."""
        # Determine which channel this notification came from
        if self._config.is_single_channel:
            rx_channel_id = 0
        elif isinstance(char, int):
            rx_channel_id = self._rx_handle_to_channel.get(char, -1)
        else:
            # Try UUID-based mapping first, then handle-based
            if char.uuid in self._config.rx_channel_uuids:
                rx_channel_id = self._config.rx_channel_uuids.index(char.uuid)
            else:
                rx_channel_id = self._rx_handle_to_channel.get(char.handle, -1)

        if rx_channel_id < 0:
            _LOGGER.warning("Received data on unknown handle/uuid: %s", char)
            return

        self._rx_raw_channel_buffer[rx_channel_id] = rx_bytes
        _LOGGER.debug("rx ch%d < %s", rx_channel_id, _hex(rx_bytes))

        # Check if we can assemble a complete packet
        if not self._rx_raw_channel_buffer[0]:
            return

        if self._config.is_single_channel:
            combined = bytearray(self._rx_raw_channel_buffer[0])
            self._rx_raw_channel_buffer = [None] * 4
        else:
            packet_size = self._rx_raw_channel_buffer[0][0]
            required_channels = range((packet_size + 15) // 16)
            # Check all required channels are received
            for ch in required_channels:
                if self._rx_raw_channel_buffer[ch] is None:
                    return
            # Combine channels
            combined = bytearray()
            for ch in required_channels:
                combined += self._rx_raw_channel_buffer[ch]
            combined = combined[:packet_size]
            self._rx_raw_channel_buffer = [None] * 4

        # Verify XOR CRC
        xor_crc = 0
        for byte in combined:
            xor_crc ^= byte
        if xor_crc:
            _LOGGER.error(
                "CRC error in rx data: crc=%d, buffer=%s", xor_crc, _hex(combined)
            )
            return

        # Extract packet fields
        self._rx_packet_type = combined[1:3]
        self._rx_eeprom_address = combined[3:5]
        expected_data_len = combined[5]
        if expected_data_len > (len(combined) - 8):
            self._rx_data_bytes = bytes(b'\xff') * expected_data_len
        else:
            if self._rx_packet_type == bytearray.fromhex("8f00"):
                # End-of-transmission packet: error code is in byte 6
                self._rx_data_bytes = combined[6:7]
            else:
                self._rx_data_bytes = combined[6:6 + expected_data_len]

        self._rx_finished.set()

    async def _send_and_wait(self, command: bytearray, timeout: float = 2.0) -> None:
        """Send a command and wait for response with retry logic."""
        for retry in range(5):
            self._rx_finished.clear()

            # Split command across TX channels
            cmd_copy = command
            channel_width = 16
            if self._config.is_single_channel:
                channel_width = max(channel_width, len(command))

            num_tx_channels = (len(command) + channel_width - 1) // channel_width
            for ch_idx in range(num_tx_channels):
                chunk = cmd_copy[:channel_width]
                _LOGGER.debug("tx ch%d > %s", ch_idx, _hex(chunk))
                if self._config.is_single_channel:
                    await self._client.write_gatt_char(
                        self._config.tx_channel_uuids[ch_idx], chunk, response=False
                    )
                else:
                    await self._client.write_gatt_char(
                        self._config.tx_channel_uuids[ch_idx], chunk
                    )
                cmd_copy = cmd_copy[channel_width:]

            # Wait for response
            try:
                await asyncio.wait_for(self._rx_finished.wait(), timeout=timeout)
                return  # Success
            except asyncio.TimeoutError:
                _LOGGER.warning("TX timeout, retry %d/5", retry + 1)

        raise ConnectionError("Failed to receive response after 5 retries")

    async def start_transmission(self) -> None:
        """Start a data readout session."""
        await self._enable_rx_notify()
        start_cmd = bytearray.fromhex("0800000000100018")
        await self._send_and_wait(start_cmd)
        if self._rx_packet_type != bytearray.fromhex("8000"):
            raise ConnectionError("Invalid response to data readout start")

    async def end_transmission(self) -> None:
        """End a data readout session."""
        stop_cmd = bytearray.fromhex("080f000000000007")
        await self._send_and_wait(stop_cmd)
        if self._rx_packet_type != bytearray.fromhex("8f00"):
            raise ConnectionError("Invalid response to data readout end")
        if self._rx_data_bytes and self._rx_data_bytes[0]:
            raise ConnectionError(
                f"Device reported error code {self._rx_data_bytes[0]}"
            )
        await self._disable_rx_notify()

    async def read_eeprom_block(self, address: int, blocksize: int) -> bytes:
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

        await self._send_and_wait(cmd)
        if self._rx_eeprom_address != address.to_bytes(2, "big"):
            raise ConnectionError(
                f"Address mismatch: got {self._rx_eeprom_address}, expected {address:#06x}"
            )
        if self._rx_packet_type != bytearray.fromhex("8100"):
            raise ConnectionError("Invalid packet type in EEPROM read")
        return self._rx_data_bytes

    async def write_eeprom_block(self, address: int, data: bytearray) -> None:
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

        await self._send_and_wait(cmd)
        if self._rx_eeprom_address != address.to_bytes(2, "big"):
            raise ConnectionError(
                f"Address mismatch in write: got {self._rx_eeprom_address}, expected {address:#06x}"
            )
        if self._rx_packet_type != bytearray.fromhex("81c0"):
            raise ConnectionError("Invalid packet type in EEPROM write")

    async def read_continuous(
        self, start_address: int, bytes_to_read: int, block_size: int = 0x10
    ) -> bytearray:
        """Read a continuous range from EEPROM in blocks."""
        result = bytearray()
        while bytes_to_read > 0:
            chunk_size = min(bytes_to_read, block_size)
            _LOGGER.debug("read %#06x size %#04x", start_address, chunk_size)
            result += await self.read_eeprom_block(start_address, chunk_size)
            start_address += chunk_size
            bytes_to_read -= chunk_size
        return result

    async def write_continuous(
        self, start_address: int, data: bytearray, block_size: int = 0x08
    ) -> None:
        """Write continuous data to EEPROM in blocks."""
        while len(data) > 0:
            chunk_size = min(len(data), block_size)
            _LOGGER.debug("write %#06x size %#04x", start_address, chunk_size)
            await self.write_eeprom_block(start_address, data[:chunk_size])
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
            try:
                await self._client.pair()
            except TypeError:
                await self._client.pair(protection_level=2)
            _LOGGER.info("OS-level BLE bonding completed")
            return

        # Custom key pairing (most legacy devices)
        if not self._config.supports_pairing:
            raise ConnectionError("Pairing is not supported for this device")

        if len(pair_key) != 16:
            raise ValueError(f"Pairing key must be 16 bytes, got {len(pair_key)}")

        # Step 1: Enable RX channel notification to trigger SMP Security Request
        _LOGGER.debug("Enabling RX notification to trigger BLE pairing")
        try:
            await self._client.start_notify(
                self._config.rx_channel_uuids[0], lambda h, d: None
            )
        except Exception as exc:
            _LOGGER.debug("Ignored error starting RX notify: %s", exc)

        # Wait a bit for SMP and service discovery
        await asyncio.sleep(1.0)

        # Step 2: Wait for unlock characteristic to resolve
        prog_event = asyncio.Event()
        response_holder: list[bytes | None] = [None]

        def _pair_callback(_: Any, rx_bytes: bytearray) -> None:
            response_holder[0] = rx_bytes
            prog_event.set()

        for _ in range(15):
            try:
                await self._client.start_notify(self._config.unlock_uuid, _pair_callback)
                break
            except Exception as exc:
                _LOGGER.debug("Waiting for unlock UUID to become available: %s", exc)
                await asyncio.sleep(1)
        else:
            raise ConnectionError(
                f"Characteristic {self._config.unlock_uuid} was not found! "
                "Try clearing Bluetooth cache."
            )

        # Step 3: Enter key programming mode
        max_retries = 15
        entered_programming = False
        for attempt in range(max_retries):
            resp = response_holder[0]
            if resp and resp[:2] == bytearray.fromhex("8200"):
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
                _LOGGER.debug("Key programming write attempt %d failed: %s", attempt + 1, exc)

            try:
                await asyncio.wait_for(prog_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

            resp = response_holder[0]
            if resp and resp[:2] == bytearray.fromhex("8200"):
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

        if resp is None or resp[:2] != bytearray.fromhex("8000"):
            raise ConnectionError(f"Failed to program pairing key. Response: {resp.hex() if resp else 'None'}")

        await self._client.stop_notify(self._config.unlock_uuid)
        await self._client.stop_notify(self._config.rx_channel_uuids[0])
        _LOGGER.info("Device paired successfully with new key")

        # Step 4: Initial handshake (required after first pairing)
        try:
            await self.start_transmission()
            await self.end_transmission()
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
        self, btobj: BluetoothTxRxHandler
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
                raw = await btobj.read_continuous(addr, size, size)
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
        self, btobj: BluetoothTxRxHandler
    ) -> list[list[dict[str, Any]]]:
        """Read all records from all users.

        Returns a list of lists: [[user1_records], [user2_records], ...]
        """
        await btobj.unlock()
        await btobj.start_transmission()

        try:
            all_user_records = []
            for user_idx in range(self._config.num_users):
                start_addr = self._config.user_start_addresses[user_idx]
                total_bytes = (
                    self._config.per_user_records_count[user_idx]
                    * self._config.record_byte_size
                )

                raw_data = await btobj.read_continuous(
                    start_addr, total_bytes, self._config.transmission_block_size
                )

                records = self._parse_user_records(raw_data, user_idx)
                all_user_records.append(records)

            await btobj.end_transmission()
        except Exception:
            # Try to cleanly end if possible
            try:
                await btobj.end_transmission()
            except Exception:
                pass
            raise

        return all_user_records

    async def get_latest_record(
        self, btobj: BluetoothTxRxHandler
    ) -> dict[str, Any] | None:
        """Read latest record using index first, then fallback to full scan."""
        layout = self._config.index_pointer_layout or {}
        indexed = await self._get_latest_via_index(btobj)
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
        return await self._get_latest_via_full_scan(btobj)

    async def _get_latest_via_full_scan(
        self, btobj: BluetoothTxRxHandler
    ) -> dict[str, Any] | None:
        """Existing full EEPROM scan path."""
        if self._config.use_layout_fallback_scan:
            all_user_records = await self._get_all_records_with_format_c_fallback(btobj)
        else:
            all_user_records = await self.get_all_records(btobj)
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
        """Wrap pointer into [min, max] range like APK memory-map logic."""
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
        self, btobj: BluetoothTxRxHandler
    ) -> dict[str, Any] | None:
        """Read index block and fetch only the latest slot per configured user."""
        layout = self._config.index_pointer_layout
        if (
            layout is None
            or self._config.settings_read_address is None
            or self._config.record_byte_size <= 0
        ):
            return None

        pointer_unsend_size = int(layout.get("pointer_unsend_size", 0))
        user_layouts = layout.get("users", [])
        if pointer_unsend_size <= 0 or not isinstance(user_layouts, list) or not user_layouts:
            return None

        record_addresses = layout.get("record_addresses") or self._config.user_start_addresses
        record_byte_size = int(layout.get("record_byte_size", self._config.record_byte_size))
        record_step = int(layout.get("record_step", record_byte_size))
        backtrack_slots = int(layout.get("backtrack_slots", 0))
        collect_all_valid = bool(layout.get("collect_all_valid_in_index_window", False))
        ptr_endian = str(layout.get("endianess", self._config.endianess))

        candidates: list[tuple[int, dict[str, Any]]] = []
        if self._config.enable_index_debug_logs:
            _LOGGER.info(
                "%s index path start: read_addr=%#06x size=%d record_size=%d record_step=%d",
                self._config.model,
                self._config.settings_read_address,
                pointer_unsend_size,
                record_byte_size,
                record_step,
            )
        await btobj.unlock()
        await btobj.start_transmission()
        try:
            index_bytes = await btobj.read_continuous(
                self._config.settings_read_address,
                pointer_unsend_size,
                self._config.transmission_block_size,
            )
            if self._config.enable_index_debug_logs:
                _LOGGER.info("%s index raw=%s", self._config.model, bytes(index_bytes).hex())
            for idx, user_cfg in enumerate(user_layouts):
                if idx >= len(record_addresses) or idx >= len(self._config.per_user_records_count):
                    continue
                pointer_offset = int(user_cfg.get("pointer_offset", -1))
                if pointer_offset < 0 or pointer_offset + 2 > len(index_bytes):
                    continue

                raw_pointer = int.from_bytes(
                    index_bytes[pointer_offset:pointer_offset + 2], ptr_endian, signed=False
                )
                pointer_mask = int(user_cfg.get("pointer_mask", 0xFF))
                pointer_min = int(user_cfg.get("pointer_min", 0))
                pointer_max = int(
                    user_cfg.get("pointer_max", self._config.per_user_records_count[idx] - 1)
                )
                correction = int(user_cfg.get("latest_pos_correction", -1))
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
                    raw_record = await btobj.read_continuous(
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
                await btobj.end_transmission()
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
        self, btobj: BluetoothTxRxHandler
    ) -> list[dict[str, Any]]:
        """Read all records, adding user index, and return a flat sorted list."""
        all_user_records = await self.get_all_records(btobj)

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
        self, btobj: BluetoothTxRxHandler
    ) -> list[list[dict[str, Any]]]:
        """Try APK-aligned layouts with early-stop to avoid excessive retries."""
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

        await btobj.unlock()
        await btobj.start_transmission()
        try:
            await self._probe_7142_counter_candidates(btobj)
            best_records: list[list[dict[str, Any]]] | None = None
            best_latest: dt.datetime | None = None
            best_layout: tuple[list[int], list[int], int] | None = None
            best_valid_count = -1
            best_score: tuple[int, int, dt.datetime, int] | None = None

            for attempt_idx, (starts, counts, record_size) in enumerate(attempts, start=1):
                all_user_records: list[list[dict[str, Any]]] = []
                for user_idx, (start_addr, count) in enumerate(zip(starts, counts)):
                    total_bytes = count * record_size
                    raw_data = await btobj.read_continuous(
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

            await btobj.end_transmission()
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
                await btobj.end_transmission()
            except Exception:
                pass
            raise
