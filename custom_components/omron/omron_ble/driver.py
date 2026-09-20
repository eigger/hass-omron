"""Record readout on top of a session: EEPROM time, index walk, latest-record selection."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from .devices import DeviceConfig, HostPairingMode
from .session import OmronDeviceSession
from .settings_mirror import SettingsMirrorLayout, clock_block
from .util import _hex

_LOGGER = logging.getLogger(__name__)

# Sub-measurements in one TruRead session (pos=1, 2, 3).
TRUREAD_SEQUENCE_LEN = 3
# A TruRead session takes roughly 3 x (measure + 60 s rest); anything wider
# than this is two separate sessions.
TRUREAD_SESSION_WINDOW = dt.timedelta(minutes=15)


def _decode_eeprom_time_payload(layout: str, cached: bytearray) -> dt.datetime:
    """Decode wall time from an EEPROM time-sync section (naive datetime)."""
    if layout == "eeprom_time_at_8":
        year_off, month, day, hour, minute, second = (int(b) for b in cached[8:14])
        return dt.datetime(
            year_off + 2000, month, day, hour, minute, min(second, 59)
        )
    if layout == "eeprom_time_at_8_swapped":
        month, year_off, hour, day, second, minute = (int(b) for b in cached[8:14])
        return dt.datetime(
            year_off + 2000, month, day, hour, minute, min(second, 59)
        )
    if layout == "eeprom_time_at_0":
        year_off, month, day, hour, minute, second = (int(b) for b in cached[0:6])
        return dt.datetime(
            year_off + 2000, month, day, hour, minute, min(second, 59)
        )
    if layout == "eeprom_time_at_2":
        year_off, month, day, hour, minute, second = (int(b) for b in cached[2:8])
        return dt.datetime(
            year_off + 2000, month, day, hour, minute, min(second, 59)
        )
    # Default: eeprom_time_at_2_swapped
    month, year_off, hour, day, second, minute = (int(b) for b in cached[2:8])
    return dt.datetime(
        year_off + 2000, month, day, hour, minute, min(second, 59)
    )


def _encode_eeprom_time_payload(
    layout: str, cached: bytearray, now: dt.datetime
) -> bytearray:
    """Build EEPROM time-sync bytes for writing (includes checksum/padding per layout)."""
    if layout == "eeprom_time_at_8":
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
    if layout == "eeprom_time_at_8_swapped":
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
    if layout == "eeprom_time_at_0":
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
    if layout == "eeprom_time_at_2":
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
    # Default: eeprom_time_at_2_swapped
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
        """Synchronize time via an EEPROM settings write.

        Memory-protocol cuffs -- the classic custom-key ones and the WLD3
        token-key ones alike -- do not take their time from the BLE CTS
        characteristic. It lives in a clock record inside the settings block,
        read from the device-owned region and written to its mirror.

        Layout keys (``DeviceConfig.time_sync_layout`` / ``resolved_time_sync_layout``):

        eeprom_time_at_2_swapped (default for [0x14, 0x1E] classic block)
            Time bytes [2:8] = [month, year-2000, hour, day, second, minute]
            Checksum [9] = sum(bytes[0:9]) & 0xFF

        eeprom_time_at_2 (same 10-byte window, chronological field order)
            Time bytes [2:8] = [year-2000, month, day, hour, minute, second]
            Checksum [9] = sum(bytes[0:9]) & 0xFF

        eeprom_time_at_8 ([0x2C, 0x3C] 16-byte block)
            Time bytes [8:14] = [year-2000, month, day, hour, minute, second]
            Checksum [14] = sum(bytes[0:14]) & 0xFF

        eeprom_time_at_0 (HEM-6401 family 16-byte settings slice)
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

    async def complete_measurement_readout(
        self, transport: OmronDeviceSession
    ) -> None:
        """Apply profile-defined end-of-readout EEPROM mirrors."""
        completion = self._config.measurement_completion
        if completion is None:
            return

        if not transport.memory_session_active:
            raise ConnectionError(
                "Cannot complete measurement readout without an active memory session"
            )

        layout = SettingsMirrorLayout(self._config)

        if completion.index_flag_offset >= layout.head_write_size:
            raise ConnectionError(
                "Measurement completion index offset lies outside the index region: "
                f"offset={completion.index_flag_offset} size={layout.head_write_size}"
            )

        index_mirror = bytearray(
            await transport.read_memory_block(
                layout.head_read_address,
                layout.head_write_size,
            )
        )
        if len(index_mirror) != layout.head_write_size:
            raise ConnectionError(
                "Measurement completion mirror 1 short read: "
                f"expected {layout.head_write_size}, got {len(index_mirror)}"
            )
        index_mirror[completion.index_flag_offset] = completion.index_flag_value
        await transport.write_memory_block(
            layout.head_write_address,
            index_mirror,
        )

        status_mirror = bytearray(
            await transport.read_memory_block(
                layout.clock_read_address,
                layout.clock_write_size,
            )
        )
        if len(status_mirror) != layout.clock_write_size:
            raise ConnectionError(
                "Measurement completion mirror 2 short read: "
                f"expected {layout.clock_write_size}, got {len(status_mirror)}"
            )
        # Flag bit, current time and checksum, the same record the official
        # app writes before its close (#175) and the pairing registration
        # sends. Not the bytes just read: those hold the cuff's own clock,
        # which is exactly what the time sync at the start of this session
        # corrected. This is the last clock write of the session and the one
        # carrying the flag, so it is the one the cuff keeps -- copying the
        # read bytes back handed it the stale time again and set the clock
        # back a little on every poll (#190).
        status_mirror = bytearray(
            clock_block(status_mirror, self._now_func(), layout.clock_write_size)
        )
        status_mirror[layout.clock_write_size - 1] = 0x00
        await transport.write_memory_block(
            layout.clock_write_address,
            status_mirror,
        )

        _LOGGER.debug(
            "Applied profile-defined measurement completion mirrors for %s",
            self._config.model,
        )

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
        indexed = await self._get_latest_via_index(transport)
        if indexed is not None:
            return indexed
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
        # Models that store a TruRead session as three consecutive slots
        # (pos=1, 2, 3) opt in with ``truread_sequence``; the probe then
        # keeps reading past the cursor until it holds a full sequence, so
        # the average can be reconstructed. Everything else stops at the
        # first plausible record, as before.
        collect_limit = (
            TRUREAD_SEQUENCE_LEN if layout.get("truread_sequence") else 1
        )
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
                # Unrecorded users have their pointer set to clear_value (0x8000 on legacy/classic profiles).
                # Modern formatVersion 4 (WLD3/WLD4) profiles always set bit15 (e.g. 0x8006) and rely on the
                # empty-slot (all-0xFF) backtrack heuristic instead.
                clear_value = user_cfg.get("clear_value", 0x8000)
                if clear_value is not None and raw_pointer == clear_value:
                    _LOGGER.debug(
                        "User%d [%s]: cursor raw=0x%04X matches clear_value 0x%04X "
                        "(no recorded measurements) — skipping and marking user confirmed empty",
                        idx + 1,
                        self._config.model,
                        raw_pointer,
                        clear_value,
                    )
                    confirmed_empty_users.add(idx + 1)
                    continue

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
                # backtrack_slots only widens the corrupt-slot skip window;
                # a TruRead sequence needs at least the two older slots too.
                max_probe = min(
                    max(backtrack_slots, collect_limit - 1),
                    max(record_count - 1, 0),
                )
                parsed = None
                base_addr = int(record_addresses[idx])
                # Track whether every probed slot for this user was the
                # device's empty marker (all-0xFF).  If so, the user has
                # never recorded a measurement and the caller can skip the
                # full-scan fallback safely.
                user_had_any_read = False
                user_all_probed_slots_empty = True
                user_collected = 0
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
                    # Appended newest-first: the cursor slot, then each
                    # older slot in probe order.
                    candidates.append((idx + 1, parsed))
                    user_collected += 1
                    if user_collected >= collect_limit:
                        break
                    # Only keep reading while this slot is still part of a
                    # TruRead sequence counting down toward the cursor
                    # (pos 3 at the cursor, then 2, then 1). A Single
                    # measurement stops here at one read, as before.
                    if parsed.get("pos") != TRUREAD_SEQUENCE_LEN - user_collected + 1:
                        break
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

        # Reduce each user's newest-first probe results to one record: the
        # reconstructed TruRead average when the cursor closes a sequence,
        # otherwise the record nearest the cursor (as before).
        selected_per_user: dict[int, tuple[int, dict[str, Any]]] = {}
        for user_idx in range(len(user_layouts)):
            user = user_idx + 1
            user_candidates = [c for c in candidates if c[0] == user]
            if not user_candidates:
                continue
            avg_record = self._truread_average(user_candidates)
            if avg_record is not None:
                selected_per_user[user] = (user, avg_record)
                continue
            record = user_candidates[0][1]
            record["measurement_type"] = "Single"
            if collect_limit > 1:
                # On these models pos is the TruRead sequence index, not a
                # posture flag; a lone pos=1..3 (session in progress, or a
                # sequence the probe could not complete) must not surface
                # as improper_position=True.
                record["pos"] = 0
            selected_per_user[user] = (user, record)

        if return_all_users:
            return (
                {
                    user: self._finalize_public_latest_record(item[1], user)
                    for user, item in selected_per_user.items()
                },
                confirmed_empty_users,
            )

        selected = self._select_latest_candidate(list(selected_per_user.values()))
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

    def _truread_average(
        self, user_candidates: list[tuple[int, dict[str, Any]]]
    ) -> dict[str, Any] | None:
        """Rebuild the TruRead average from a user's newest-first probe results.

        The monitor stores a TruRead session as three consecutive slots
        tagged pos=1, 2, 3 and only displays their average. ``user_candidates``
        must be in probe order (cursor slot first), which keeps the sequence
        intact across the ring-buffer wrap where slot numbers restart at 0.
        Returns ``None`` unless the three newest records form a complete
        session within ``TRUREAD_SESSION_WINDOW``.
        """
        if len(user_candidates) < TRUREAD_SEQUENCE_LEN:
            return None
        newest = [c[1] for c in user_candidates[:TRUREAD_SEQUENCE_LEN]]
        c3, c2, c1 = newest
        if [r.get("pos") for r in newest] != [3, 2, 1]:
            return None
        dt3 = c3.get("datetime")
        dt1 = c1.get("datetime")
        if not (isinstance(dt3, dt.datetime) and isinstance(dt1, dt.datetime)):
            return None
        if not (dt.timedelta(0) <= dt3 - dt1 <= TRUREAD_SESSION_WINDOW):
            return None

        avg_record = dict(c3)
        for key in ("sys", "dia", "bpm"):
            avg_record[key] = round(sum(r[key] for r in newest) / TRUREAD_SEQUENCE_LEN)
        avg_record["measurement_type"] = "TruRead Average"
        # c3 carries pos=3 (sequence index); clear it so the aggregate does
        # not surface as improper_position=True.
        avg_record["pos"] = 0
        avg_record["truread_details"] = [
            {
                "sys": r.get("sys"),
                "dia": r.get("dia"),
                "bpm": r.get("bpm"),
                "time": r["datetime"].isoformat() if r.get("datetime") else None,
                "pos": r.get("pos"),
            }
            for r in (c1, c2, c3)
        ]
        _LOGGER.debug(
            "TruRead [%s] user=%d slots=%s → avg sys=%d dia=%d bpm=%d",
            self._config.model, user_candidates[0][0],
            [r.get("_slot_index") for r in (c1, c2, c3)],
            avg_record["sys"], avg_record["dia"], avg_record["bpm"],
        )
        return avg_record


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
        """Choose the latest record across users by datetime, slot index as tiebreaker."""
        if not candidates:
            return None

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
