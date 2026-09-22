"""Support for Omron button entities."""

from __future__ import annotations

import time

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.exceptions import HomeAssistantError

from .entity import OmronEntity
from .session_handoff import (
    omron_poll_ble_telemetry,
    poll_parked_session,
    run_post_pairing_poll,
)
from .types import OmronConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OmronConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Omron button entities."""
    runtime = entry.runtime_data
    refresh_description = ButtonEntityDescription(
        key=runtime.entity_unique_id("refresh_data"),
        name=f"{runtime.model} {runtime.identifier.upper()} Refresh Data",
        icon="mdi:refresh",
        entity_category=EntityCategory.CONFIG,
    )
    pairing_retry_description = ButtonEntityDescription(
        key=runtime.entity_unique_id("retry_pairing"),
        name=f"{runtime.model} {runtime.identifier.upper()} Retry Pairing",
        icon="mdi:bluetooth-connect",
        entity_category=EntityCategory.CONFIG,
    )

    async_add_entities(
        [
            OmronRefreshDataButtonEntity(entry, refresh_description),
            OmronRetryPairingButtonEntity(entry, pairing_retry_description),
        ]
    )


class OmronRefreshDataButtonEntity(OmronEntity, ButtonEntity):
    """Button entity to trigger an immediate data refresh poll."""

    entity_description: ButtonEntityDescription

    def __init__(
        self,
        entry: OmronConfigEntry,
        description: ButtonEntityDescription,
    ) -> None:
        """Initialize entity."""
        self.entity_description = description
        self._bind(entry)
        self._attr_unique_id = description.key

    async def async_press(self) -> None:
        """Handle button press to poll device and refresh sensor data."""
        try:
            await self._runtime.poll_coordinator.async_request_refresh()
        except Exception as err:
            raise HomeAssistantError(f"Failed to refresh data: {err}") from err


class OmronRetryPairingButtonEntity(OmronEntity, ButtonEntity):
    """Button entity to retry BLE pairing/bonding on demand."""

    entity_description: ButtonEntityDescription

    def __init__(
        self,
        entry: OmronConfigEntry,
        description: ButtonEntityDescription,
    ) -> None:
        """Initialize entity."""
        self.entity_description = description
        self._bind(entry)
        self._attr_unique_id = description.key

    async def async_press(self) -> None:
        """Handle button press to retry pairing/bonding."""
        runtime = self._runtime
        ble_device = async_ble_device_from_address(self.hass, self._address)
        if ble_device is None:
            raise HomeAssistantError(f"BLE device not available: {self._address}")

        session_lock = runtime.session_lock
        # Fail fast if another BLE session is already running; tell the user to
        # retry rather than racing the existing connection (concurrent BLE
        # sessions to the same Omron device cause SMP auth failures).
        if session_lock.locked():
            raise HomeAssistantError(
                f"BLE session already in progress for {self._address}; retry in a moment"
            )
        poll_coordinator = runtime.poll_coordinator
        # An earlier attempt may have left a session parked and still
        # connected because its poll skipped — which is exactly when a user
        # presses this button again. Pairing now would put a second BLE link
        # on the same cuff. Checked before taking the lock: the poll needs it
        # to adopt the parked session.
        if await poll_parked_session(self.hass, self._address, poll_coordinator):
            return
        try:
            async with session_lock:
                async with omron_poll_ble_telemetry(self.hass, runtime, "pairing"):
                    paired_session = await runtime.device_data.async_retry_pairing(
                        ble_device
                    )
                # Seed the advertisement-trigger cooldown the way setup does:
                # a pairing-mode advert arriving now would otherwise start an
                # auto-session that takes the lock before the poll below, and
                # that poll would skip and leave the fresh link unused.
                runtime.last_attempt_time = time.time()
        except Exception as err:
            raise HomeAssistantError(f"Failed to retry pairing: {err}") from err
        # Lock auto-released by the context manager. Mirror setup behavior:
        # run an immediate poll after pairing so protected GATT paths are
        # exercised and bond/session state settles. async_poll_data acquires
        # the lock on its own, and adopts the link parked for it rather than
        # reconnecting — a PER_SESSION cuff refuses that second connect.
        await run_post_pairing_poll(
            self.hass, self._address, paired_session, poll_coordinator
        )
