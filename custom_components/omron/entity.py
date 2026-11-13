"""Shared entity bases for Omron."""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN
from .data import OmronRuntimeData
from .history import async_entry_device
from .omron_ble.devices import get_device_config
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


class OmronCoordinatorEntity[T](
    OmronEntity, CoordinatorEntity[DataUpdateCoordinator[T]]
):
    """Entry-bound entity fed by one of the per-entry coordinators."""

    def __init__(
        self,
        entry: OmronConfigEntry,
        coordinator: DataUpdateCoordinator[T],
    ) -> None:
        super().__init__(coordinator)
        self._bind(entry)


def user_device_identifier(entry_id: str, subentry_id: str) -> tuple[str, str]:
    """Identifier of the sub-device that holds one user's entities."""
    return (DOMAIN, f"{entry_id}_{subentry_id}")


def user_device_info(
    hass: HomeAssistant, entry: OmronConfigEntry, subentry: ConfigSubentry
) -> DeviceInfo:
    """The sub-device of one user, hanging off the cuff or scale's own device."""
    info = DeviceInfo(
        identifiers={user_device_identifier(entry.entry_id, subentry.subentry_id)},
        manufacturer="Omron",
        model=get_device_config(entry.runtime_data.model).display_model,
        name=subentry.title,
    )
    # Newer Home Assistant links a device to its parent by id and deprecates
    # the identifier pair; the minimum supported version knows only the pair,
    # which the main device carries once the entry has users (see setup).
    if "via_device_id" in DeviceInfo.__annotations__:
        parent = async_entry_device(hass, entry)
        if parent is not None:
            info["via_device_id"] = parent.id  # type: ignore[typeddict-unknown-key]
    else:
        info["via_device"] = (DOMAIN, entry.entry_id)  # type: ignore[typeddict-unknown-key]
    return info
