"""Setup helpers for Omron Bluetooth."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .const import DEFAULT_DEVICE_MODEL
from .devices import get_device_config
from .omron_driver import OmronDeviceSession
from .setup_time_sync import (
    async_sync_device_time,
    async_sync_eeprom_time,
    build_cts_payload,
)

if TYPE_CHECKING:
    from homeassistant.components.bluetooth import BLEDevice

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "async_fetch_device_model_number",
    "async_pair_and_sync_device",
    "async_sync_device_time",
    "async_sync_eeprom_time",
    "build_cts_payload",
]


async def async_fetch_device_model_number(
    ble_device: BLEDevice, *, keep_session_open: bool = False
) -> tuple[str | None, OmronDeviceSession | None]:
    """Read the model number, returning the session when asked to keep it open.

    The link is not incidental: WLD3.0 cuffs raise an SMP Security Request a few
    tens of milliseconds after connect, so this short read is where the bond
    actually gets made. Closing it mid key distribution leaves a stored bond
    missing the EDIV/Rand half, which legacy pairing needs to resume later.

    ``keep_session_open`` hands the caller the live session instead, so the
    pairing that follows continues on the link that bonded. The caller owns it
    from then on, including closing it if the flow does not get that far.
    """
    # Model is unknown here; a placeholder profile is enough because
    # read_model_number() only uses the standard DIS characteristic.
    session = OmronDeviceSession(
        ble_device, get_device_config(DEFAULT_DEVICE_MODEL)
    )
    try:
        await session.connect()
    except Exception as exc:
        _LOGGER.debug("Could not connect to read Model Number: %s", exc)
        return None, None
    model_num: str | None = None
    try:
        model_num = await session.read_model_number()
        if model_num:
            _LOGGER.debug("Fetched Model Number during setup: %s", model_num)
    except Exception as exc:
        _LOGGER.debug("Error reading Model Number: %s", exc)
    if keep_session_open:
        return model_num, session
    await session.aclose()
    return model_num, None


async def async_pair_and_sync_device(
    session: OmronDeviceSession,
    model: str,
    *,
    leave_memory_session_open: bool = False,
) -> None:
    """Pair and run the initial time sync on an already-open device session."""
    if not await session.verify_parent_service():
        # Fallback to standard BP service if the parent is not found yet
        _LOGGER.debug(
            "Parent service %s not found on %s, continuing anyway",
            session.config.parent_service_uuid,
            session.address,
        )

    await session.pair()
    await async_sync_device_time(
        session.client,
        model,
        session.config,
        session,
        leave_memory_session_open=leave_memory_session_open,
    )
    _LOGGER.debug("Successfully paired and synced with %s (%s)", model, session.address)
