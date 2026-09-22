"""Shared entity bases for Omron."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .data import OmronRuntimeData
from .types import OmronConfigEntry


class OmronEntity:
    """Mixin binding an entity to one loaded Omron entry.

    No ``__init__``: Home Assistant entity bases do not all chain
    ``super().__init__()``. Concrete classes call ``_bind()`` after their
    base initialisers. List this class before the HA base.
    """

    hass: HomeAssistant
    _entry: OmronConfigEntry
    _runtime: OmronRuntimeData
    _address: str

    def _bind(self, entry: OmronConfigEntry) -> None:
        """Bind to the cuff of ``entry``."""
        self._entry = entry
        self._runtime = entry.runtime_data
        self._address = self._runtime.address
        self.hass = self._runtime.bt_coordinator.hass

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._address)},
        )


class OmronCoordinatorEntity[T](OmronEntity, CoordinatorEntity[DataUpdateCoordinator[T]]):
    """Entry-bound entity fed by one of the per-entry coordinators."""

    def __init__(
        self,
        entry: OmronConfigEntry,
        coordinator: DataUpdateCoordinator[T],
    ) -> None:
        super().__init__(coordinator)
        self._bind(entry)
