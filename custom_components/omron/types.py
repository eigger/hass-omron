"""Shared typing helpers for the Omron integration."""

from __future__ import annotations

from typing import TypeAlias

from homeassistant.config_entries import ConfigEntry

OmronConfigEntry: TypeAlias = ConfigEntry["OmronBluetoothProcessorCoordinator"]
