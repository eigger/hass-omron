"""Support for Omron button entities."""

from __future__ import annotations

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.exceptions import HomeAssistantError

from .ble_session import omron_poll_ble_telemetry
from .const import DOMAIN
from .types import OmronConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OmronConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Omron button entities."""
    address = hass.data[DOMAIN][entry.entry_id]["address"]
    model = hass.data[DOMAIN][entry.entry_id]["data"].device_model
    identifier = address.replace(":", "")[-4:].lower()
    model_slug = model.lower().replace("-", "_")
    refresh_description = ButtonEntityDescription(
        key=f"{model_slug}_{identifier}_refresh_data",
        name=f"{model} {identifier.upper()} Refresh Data",
        icon="mdi:refresh",
        entity_category=EntityCategory.CONFIG,
    )
    pairing_retry_description = ButtonEntityDescription(
        key=f"{model_slug}_{identifier}_retry_pairing",
        name=f"{model} {identifier.upper()} Retry Pairing",
        icon="mdi:bluetooth-connect",
        entity_category=EntityCategory.CONFIG,
    )

    async_add_entities(
        [
            OmronRefreshDataButtonEntity(hass, entry, refresh_description),
            OmronRetryPairingButtonEntity(hass, entry, pairing_retry_description),
        ]
    )


class OmronRefreshDataButtonEntity(ButtonEntity):
    """Button entity to trigger an immediate data refresh poll."""

    entity_description: ButtonEntityDescription

    def __init__(
        self,
        hass: HomeAssistant,
        entry: OmronConfigEntry,
        description: ButtonEntityDescription,
    ) -> None:
        """Initialize entity."""
        self.hass = hass
        self.entity_description = description
        self._entry_id = entry.entry_id
        self._entry = entry
        self._address = hass.data[DOMAIN][entry.entry_id]["address"]
        self._attr_unique_id = description.key

    @property
    def device_info(self) -> DeviceInfo:
        """Attach button to the same BLE device."""
        return DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._address)},
        )

    async def async_press(self) -> None:
        """Handle button press to poll device and refresh sensor data."""
        poll_coordinator = self._entry.runtime_data.poll_coordinator
        try:
            await poll_coordinator.async_request_refresh()
        except Exception as err:
            raise HomeAssistantError(f"Failed to refresh data: {err}") from err


class OmronRetryPairingButtonEntity(ButtonEntity):
    """Button entity to retry BLE pairing/bonding on demand."""

    entity_description: ButtonEntityDescription

    def __init__(
        self,
        hass: HomeAssistant,
        entry: OmronConfigEntry,
        description: ButtonEntityDescription,
    ) -> None:
        """Initialize entity."""
        self.hass = hass
        self.entity_description = description
        self._entry_id = entry.entry_id
        self._entry = entry
        self._address = hass.data[DOMAIN][entry.entry_id]["address"]
        self._attr_unique_id = description.key

    @property
    def device_info(self) -> DeviceInfo:
        """Attach button to the same BLE device."""
        return DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._address)},
        )

    async def async_press(self) -> None:
        """Handle button press to retry pairing/bonding."""
        ble_device = async_ble_device_from_address(self.hass, self._address)
        if ble_device is None:
            raise HomeAssistantError(f"BLE device not available: {self._address}")

        entry_data = self.hass.data[DOMAIN][self._entry_id]
        session_lock = entry_data["session_lock"]
        # Fail fast if another BLE session is already running; tell the user to
        # retry rather than racing the existing connection (concurrent BLE
        # sessions to the same Omron device cause SMP auth failures).
        if session_lock.locked():
            raise HomeAssistantError(
                f"BLE session already in progress for {self._address}; retry in a moment"
            )
        data = entry_data["data"]
        try:
            async with session_lock:
                async with omron_poll_ble_telemetry(entry_data):
                    await data.async_retry_pairing(ble_device)
        except Exception as err:
            raise HomeAssistantError(f"Failed to retry pairing: {err}") from err
        # Lock auto-released by the context manager. Mirror setup behavior:
        # run an immediate poll after pairing so protected GATT paths are
        # exercised and bond/session state settles. _async_poll_data will
        # acquire the lock on its own.
        poll_coordinator = self._entry.runtime_data.poll_coordinator
        await poll_coordinator.async_request_refresh()
