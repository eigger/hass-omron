"""Per-entry runtime state for the Omron integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from blesession import SessionReports
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from sensor_state_data import DeviceKey, SensorUpdate

from .const import DOMAIN
from .omron_ble.devices import MeasurementKind

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
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    unloading: bool = False
    force_poll_after_lock: bool = False
    credential_write: bool = False
    # Set while a BLE session runs and until its report is recorded; the
    # Duration sensor hides its attributes meanwhile.
    session_report_pending: bool = False
    # Height per user slot (1-based) from the Height number entity, in cm.
    # None means cleared; a slot that is absent has not reported yet.
    heights_cm: dict[int, float | None] = field(default_factory=dict)

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

    @property
    def measurement_kind(self) -> MeasurementKind:
        """What the configured profile measures."""
        return self.device_data.measurement_kind

    @property
    def is_scale(self) -> bool:
        """Whether the profile is a scale (weight or body composition)."""
        return self.measurement_kind != MeasurementKind.BLOOD_PRESSURE

    @property
    def is_weight_only_scale(self) -> bool:
        """A scale that reports weight alone, so BMI is derived here."""
        return self.measurement_kind == MeasurementKind.WEIGHT

    @property
    def weight_device_key(self) -> DeviceKey:
        """Poll key of the weight sensor (single-user scales)."""
        return DeviceKey(key="weight", device_id=None)


def signal_height_updated(entry_id: str) -> str:
    """Dispatcher signal sent when an entry's Height value changes."""
    return f"{DOMAIN}_{entry_id}_height_updated"
