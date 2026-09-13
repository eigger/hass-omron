"""The settings mirror a session writes back before it closes.

Every OMRON memory-protocol cuff keeps a device-owned settings region and an
app-maintained mirror of it at a fixed, model-specific offset. The official app
refreshes the mirror at the end of a session: the index region (with the unread
counters cleared), a per-transfer slot record, and the clock record stamped with
the current time. Where and how much is entirely a property of the profile --
``settings_read_address``, ``settings_write_address``,
``settings_time_sync_bytes`` and the index region size already describe both
regions -- so nothing here is written for one model.

Two paths use this: the secure session (``secure_flow``), whose initialization
is what earns its credential, and the token-key pairing commit
(``OmronDeviceSession.commit_pairing_registration``), which a WLD3.0 cuff needs
before it will resume the bond on the next connection (#175, #91).
"""
from __future__ import annotations

from datetime import datetime

from .devices import DeviceConfig


class SettingsMirrorLayout:
    """Addresses the mirror write copies, all derived from the profile.

    The device-owned settings region is mirrored into the write region, and the
    clock record inside it is stamped with the current time. ``settings_read_address``,
    ``settings_write_address``, ``settings_time_sync_bytes`` and the index region
    size already describe both regions, so no profile needs new fields for this.
    """

    def __init__(self, config: DeviceConfig) -> None:
        read_addr = config.settings_read_address
        write_addr = config.settings_write_address
        time_range = config.settings_time_sync_bytes
        index_size = int(
            (config.index_pointer_layout or {}).get("index_region_byte_size", 0)
        )
        if (
            read_addr is None
            or write_addr is None
            or not time_range
            or len(time_range) != 2
            or index_size <= 0
        ):
            raise ValueError(
                f"Profile {config.model} cannot describe a secure initialization: "
                "settings addresses, time-sync range and index region size are all required"
            )
        clock_size = time_range[1] - time_range[0]
        if clock_size <= 0:
            raise ValueError(
                f"Profile {config.model} has an empty time-sync range {time_range}"
            )
        self.head_read_address = read_addr
        # Everything ahead of the clock record.
        self.head_read_size = time_range[0]
        # Only the index region is mirrored; the rest is device-owned.
        self.head_write_address = write_addr
        self.head_write_size = index_size
        self.clock_read_address = read_addr + time_range[0]
        # Never shorter than what gets written back, or the record cannot be
        # built at all -- and the head write has already landed by then. The
        # reference reads past the record on the one profile where the index
        # region is the larger of the two, so keep that read length.
        self.clock_read_size = max(clock_size, index_size)
        self.clock_write_address = write_addr + time_range[0]
        self.clock_write_size = clock_size


def clock_block(tail: bytes, now: datetime, size: int) -> bytes:
    """Stamp the clock record: set its flag, write the time, fix the checksum.

    ``tail`` may be read longer than the record; only the first ``size`` bytes
    are written back. The checksum is the additive sum of everything ahead of
    it and sits in the second-to-last byte, so it moves with ``size`` rather
    than living at a fixed offset -- verified on every 16-byte sample in the
    #67 and #91 captures, where byte 14 holds the sum of bytes 0..13.
    """
    if len(tail) < size or size < 16 or not 2000 <= now.year <= 2255:
        raise ValueError(
            f"Invalid secure initialization clock record (size={size}, "
            f"available={len(tail)}) or year {now.year}"
        )
    block = bytearray(tail[:size])
    checksum_at = size - 2
    block[4] |= 1
    block[8:14] = bytes(
        (now.year - 2000, now.month, now.day, now.hour, now.minute, now.second)
    )
    block[checksum_at] = sum(block[:checksum_at]) & 0xFF
    return bytes(block)
