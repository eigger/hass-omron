"""Per-entry runtime state for the Omron integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from blesession import SessionReports
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from sensor_state_data import SensorUpdate

if TYPE_CHECKING:
    from .coordinator import OmronBluetoothProcessorCoordinator
    from .omron_ble import OmronBluetoothDeviceData


@dataclass
class OmronRuntimeData:
    """Everything a loaded config entry holds, attached as ``entry.runtime_data``.

    Links parked by the config flow before the entry exists stay in
    ``hass.data`` (``_setup_sessions``, ``_probe_sessions``). A loaded entry
    does not own them.
    """

    address: str
    device_data: OmronBluetoothDeviceData
    bt_coordinator: OmronBluetoothProcessorCoordinator
    poll_coordinator: DataUpdateCoordinator[SensorUpdate]
    connection_coordinator: DataUpdateCoordinator[bool]
    duration_coordinator: DataUpdateCoordinator[float | None]
    readout_coordinator: DataUpdateCoordinator[datetime | None]
    failure_coordinator: DataUpdateCoordinator[datetime | None]
    failure_count_coordinator: DataUpdateCoordinator[int]
    session_reports: SessionReports
    session_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_attempt_time: float = 0.0
    pending_forced_transfer: bool = False
    pending_forced_transfer_baseline: Any = None
    pending_forced_transfer_task: asyncio.Task[None] | None = None
    force_poll_after_lock: bool = False
    credential_write: bool = False

    @property
    def identifier(self) -> str:
        """Last four hex digits of the address, lower-case, as unique ids use them."""
        return self.address.replace(":", "")[-4:].lower()

    @property
    def model(self) -> str:
        """Configured profile id (``device_model``), not the display name."""
        return self.device_data.device_model

    @property
    def model_slug(self) -> str:
        return self.model.lower().replace("-", "_")

    def entity_unique_id(self, suffix: str) -> str:
        """``{model}_{last4}_{suffix}``, the id poll-backed entities already use."""
        return f"{self.model_slug}_{self.identifier}_{suffix}"
