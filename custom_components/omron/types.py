"""Shared typing helpers for the Omron integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from .data import OmronRuntimeData

type OmronConfigEntry = ConfigEntry[OmronRuntimeData]
