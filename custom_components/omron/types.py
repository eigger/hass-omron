"""Shared typing helpers for the Omron integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeAlias

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from .coordinator import OmronBluetoothProcessorCoordinator

OmronConfigEntry: TypeAlias = ConfigEntry["OmronBluetoothProcessorCoordinator"]
