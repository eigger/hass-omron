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
