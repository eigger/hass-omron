"""Shared helpers for Omron integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry

from .const import CONF_USER_ALIASES



def aliases_dict_from_entry(entry: ConfigEntry) -> dict[int, str]:
    """Load per-device-user display labels from config entry (1-based indices)."""
    raw = entry.options.get(CONF_USER_ALIASES, entry.data.get(CONF_USER_ALIASES))
    if not raw or not isinstance(raw, dict):
        return {}
    out: dict[int, str] = {}
    for k, v in raw.items():
        ks = str(k)
        if not ks.isdigit():
            continue
        idx = int(ks)
        label = str(v).strip() if v is not None else ""
        out[idx] = label if label else f"user{idx}"
    return out
