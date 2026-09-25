"""Omron manufacturer-specific advertisement field decode."""
from __future__ import annotations

from typing import Any


def _decode_omron_msd_fields(payload: bytes) -> dict[str, Any] | None:
    """Decode OMRON MSD into a normalized dict, or ``None`` on mismatch.

    Bleak strips the 2-byte LE manufacturer ID before delivering the
    payload, so ``payload[0]`` is the first format byte of the MSD body.

    Format 0x03 (older BPM, MSD >= 5 bytes):
        ``len(payload) >= 3``; reads ``payload[1]`` (status bits) and
        ``payload[2]`` (result identifier num).

    Formats 0x01 / 0x02 / 0x06 (legacy WLP single/multi-user; 3-byte
    per-user stride):
        Status byte at ``payload[1]`` uses the same bit layout as
        0x08 / 0x09.  Per-user sequence numbers occupy
        ``payload[i*3 + 2 .. i*3 + 4]``, so minimum required length is
        ``4 + (user_count * 3)``.  HEM-7142T2 and other older
        single-user cuffs emit format 0x01 with ``len(payload) == 5``.

    Format 0x08 (BLS-style, fixed per user count):
        user_count == 0 → ``len(payload) == 10`` (MSD 12B)
        user_count == 1 → ``len(payload) == 13`` (MSD 15B)
        user_count 2 / 3 → not defined for this format → rejected.

    Format 0x09 (newest, 2 bytes per registered user):
        user_count == 0 → ``len(payload) == 9``  (MSD 11B)
        user_count == 1 → ``len(payload) == 11`` (MSD 13B)
        user_count == 2 → ``len(payload) == 13`` (MSD 15B)
        user_count == 3 → ``len(payload) == 15`` (MSD 17B)

    Any other ``payload[0]`` value is unsupported → ``None``.
    Payloads shorter than 2 bytes also return ``None`` (defensive: the
    caller already filters, but the function is reusable).
    """
    if len(payload) < 2:
        return None
    b11 = payload[0]

    if b11 == 0x03:
        if len(payload) < 3:
            return None
        b12 = payload[1]
        return {
            "user_register_count": b12 & 0x03,
            "invalid_time": bool(b12 & 0x04),
            "pairing_mode": bool(b12 & 0x08),
            "guidance_mode": (b12 & 0x30) >> 4,
            "result_identifier_num": payload[2],
            # Not present in this format.
            "streaming_mode": False,
            "service_uuid_mode": False,
            "forced_transfer": False,
        }

    if b11 in (0x01, 0x02, 0x06):
        b13 = payload[1]
        user_count = b13 & 0x03
        min_len = 4 + (user_count * 3)
        if len(payload) < min_len:
            return None
        return {
            "user_register_count": user_count,
            "invalid_time": bool(b13 & 0x04),
            "pairing_mode": bool(b13 & 0x08),
            "streaming_mode": bool(b13 & 0x10),
            "service_uuid_mode": bool(b13 & 0x20),
            "forced_transfer": bool(b13 & 0x40),
            # Not present in this format.
            "guidance_mode": 0,
            "result_identifier_num": 0,
        }

    if b11 == 0x08:
        b13 = payload[1]
        user_count = b13 & 0x03
        length_ok = (
            (user_count == 0 and len(payload) == 10)
            or (user_count == 1 and len(payload) == 13)
        )
        if not length_ok:
            return None
        return {
            "user_register_count": user_count,
            "invalid_time": bool(b13 & 0x04),
            "pairing_mode": bool(b13 & 0x08),
            "streaming_mode": bool(b13 & 0x10),
            "service_uuid_mode": bool(b13 & 0x20),
            "forced_transfer": bool(b13 & 0x40),
            # Not present in this format.
            "guidance_mode": 0,
            "result_identifier_num": 0,
        }

    if b11 == 0x09:
        b10 = payload[1]
        user_count = b10 & 0x03
        expected_len = {0: 9, 1: 11, 2: 13, 3: 15}.get(user_count)
        if expected_len is None or len(payload) != expected_len:
            return None
        return {
            "user_register_count": user_count,
            "invalid_time": bool(b10 & 0x04),
            "pairing_mode": bool(b10 & 0x08),
            "streaming_mode": bool(b10 & 0x10),
            "service_uuid_mode": bool(b10 & 0x20),
            "forced_transfer": bool(b10 & 0x40),
            # Not present in this format.
            "guidance_mode": 0,
            "result_identifier_num": 0,
        }

    return None
