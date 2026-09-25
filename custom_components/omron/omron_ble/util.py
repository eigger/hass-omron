"""Helpers shared by the parser and the entity platforms."""

from __future__ import annotations

import re


def slugify_for_entity_key(raw: str) -> str:
    """Normalize a display label into a stable entity-key fragment (lowercase, a-z0-9_)."""
    s = str(raw).strip().lower()
    if not s:
        return ""
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:48]


# Longest first so ``blood_pressure_systolic`` is not parsed as a shorter base.
_TRANSLATED_NAME_BASES: tuple[str, ...] = (
    "mean_arterial_pressure_estimated",
    "blood_pressure_systolic",
    "blood_pressure_diastolic",
    "blood_pressure_category",
    "rate_pressure_product",
    "measurement_timestamp",
    "improper_position",
    "irregular_pulse",
    "forced_transfer",
    "signal_strength",
    "pulse_pressure",
    "body_movement",
    "pairing_mode",
    "invalid_time",
    "shock_index",
    "heart_rate",
    "cuff_fit",
)

# Slot label is part of the entity key only for these. Device-level flags are not.
_USER_NAME_BASES = frozenset({
    "mean_arterial_pressure_estimated",
    "blood_pressure_systolic",
    "blood_pressure_diastolic",
    "blood_pressure_category",
    "rate_pressure_product",
    "measurement_timestamp",
    "improper_position",
    "irregular_pulse",
    "pulse_pressure",
    "body_movement",
    "shock_index",
    "heart_rate",
    "cuff_fit",
})


def entity_name_translation(
    key: str, aliases: dict[int, str] | None
) -> tuple[str, dict[str, str]] | None:
    """Map a sensor key to a translation key and optional ``{user}`` placeholder.

    Single-slot cuffs use the base key (``blood_pressure_systolic``). Extra
    slots append the alias slug and use ``<base>_user`` so the entered name
    stays a placeholder instead of a translated word. ``None`` means the key
    is not one of ours.
    """
    key = str(key)
    for base in _TRANSLATED_NAME_BASES:
        if key == base:
            return base, {}
        if base not in _USER_NAME_BASES or not key.startswith(f"{base}_"):
            continue
        slug = key[len(base) + 1 :]
        if not slug:
            return None
        return f"{base}_user", {"user": _label_for_alias_slug(slug, aliases)}
    return None


def _label_for_alias_slug(slug: str, aliases: dict[int, str] | None) -> str:
    """Return the configured display label for an entity-key slug."""
    if aliases:
        for idx, label in aliases.items():
            text = str(label).strip() or f"user{idx}"
            candidate = slugify_for_entity_key(text) or f"user{idx}"
            if candidate == slug:
                return text
    return slug


def _hex(data: bytes | bytearray) -> str:
    """Convert byte array to hex string."""
    return bytes(data).hex()
