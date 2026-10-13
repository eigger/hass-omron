"""Stored-record sync: every measurement the cuff took between two polls (#234).

The cuff keeps its measurements in EEPROM, but a poll only surfaces the newest.
The parser reads the ones in between; this module hands each to Home Assistant
as an ``omron_measurement`` event and remembers how far it got, so the next
poll starts after it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.storage import Store
from sensor_state_data import SensorUpdate

from .const import DOMAIN

if TYPE_CHECKING:
    from .types import OmronConfigEntry

_LOGGER = logging.getLogger(__name__)

EVENT_MEASUREMENT = "omron_measurement"
STORE_VERSION = 1


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    # Not entry.data: updating it fires the update listener, which reloads
    # the integration in the middle of the poll that wrote it.
    return Store(hass, STORE_VERSION, f"{DOMAIN}.history_{entry_id}")


async def async_load_last_sequences(
    hass: HomeAssistant, entry_id: str
) -> dict[int, int]:
    """Last cuff sequence handled per user (1-based); empty before the first sync."""
    stored = await _store(hass, entry_id).async_load()
    if not isinstance(stored, dict):
        return {}
    raw = stored.get("last_seq")
    if not isinstance(raw, dict):
        return {}
    result: dict[int, int] = {}
    for key, value in raw.items():
        try:
            result[int(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return result


async def async_remove_history(hass: HomeAssistant, entry_id: str) -> None:
    """Forget the sync position of a removed entry."""
    await _store(hass, entry_id).async_remove()


async def async_push_history_updates(
    poll_coordinator, updates: list[SensorUpdate], merge
) -> None:
    """Give each older record its own state change, oldest first.

    Yielding after each one lets the state machine (and the recorder) see them
    as separate changes before the poll's own, newest result lands.
    """
    for update in updates:
        previous = poll_coordinator.data
        poll_coordinator.async_set_updated_data(
            merge(previous, update) if previous is not None else update
        )
        await asyncio.sleep(0)


async def async_publish_history(hass: HomeAssistant, entry: OmronConfigEntry) -> None:
    """Fire one event per synced record, then remember the new position."""
    device_data = entry.runtime_data.device_data
    pending = device_data.pending_history
    if pending is None:
        return
    device_data.pending_history = None
    device = dr.async_get(hass).async_get_device(
        connections={(CONNECTION_BLUETOOTH, entry.runtime_data.address)}
    )
    for event in pending["events"]:
        hass.bus.async_fire(
            EVENT_MEASUREMENT,
            {
                "device_id": device.id if device else None,
                "address": entry.runtime_data.address,
                **event,
            },
        )
    # After the events, so a crash in between replays them (at least once)
    # instead of dropping them.
    last_seq = {**(device_data.history_last_seq or {}), **pending["next_seq"]}
    await _store(hass, entry.entry_id).async_save(
        {"last_seq": {str(user): seq for user, seq in last_seq.items()}}
    )
    device_data.history_last_seq = last_seq
    device_data.history_shown.update(pending.get("shown", {}))
    if pending["events"]:
        _LOGGER.debug(
            "Published %d stored measurement(s) for %s",
            len(pending["events"]), entry.entry_id,
        )
