"""Shared BLE poll / pairing session telemetry (connection + duration tickers)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter
from typing import TYPE_CHECKING

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .omron_ble.omron_driver import OmronDeviceSession

_LOGGER = logging.getLogger(__name__)


def stash_handoff_session(
    hass: HomeAssistant, address: str, session: OmronDeviceSession
) -> None:
    """Park a still-open pairing session for the next poll to adopt.

    WLD3.0 cuffs serve data during the pairing session and reject later
    connections, so closing the link here and reconnecting for the poll is
    what makes the follow-up read fail. Parking the session lets
    ``async_poll`` reuse the very link that just bonded.
    """
    handoff = hass.data.setdefault(DOMAIN, {}).setdefault("_setup_sessions", {})
    handoff[address] = session.release_for_handoff()


def adopt_handoff_session(
    hass: HomeAssistant, address: str
) -> OmronDeviceSession | None:
    """Take the parked pairing session for a poll that is about to run.

    Call this only once the poll is committed to connecting. Taking it on a
    path that then bails out (no device, session lock held) hands the caller
    a link it will close, and a PER_SESSION cuff refuses the reconnect that
    the retried poll would have to make.
    """
    return hass.data.get(DOMAIN, {}).get("_setup_sessions", {}).pop(address, None)


async def discard_handoff_session(hass: HomeAssistant, address: str) -> None:
    """Close a parked pairing session that no poll ended up adopting.

    ``release_for_handoff()`` cleared the disconnect responsibility, so
    ownership has to be reclaimed before ``aclose()`` will drop the link.
    """
    session = hass.data.get(DOMAIN, {}).get("_setup_sessions", {}).pop(address, None)
    if session is None:
        return
    try:
        session.reclaim_ownership()
        await session.aclose()
    except Exception as exc:
        _LOGGER.debug(
            "Discarding unused pairing session for %s failed: %s", address, exc
        )


@asynccontextmanager
async def handed_off_session(
    hass: HomeAssistant, address: str, session: OmronDeviceSession
) -> AsyncIterator[None]:
    """Park ``session`` for the poll run inside the block, then clean up.

    Discarding on exit is a no-op once the poll adopted the session; it only
    closes the link when the refresh was debounced away or never reached the
    poll, which would otherwise leak the connection.
    """
    stash_handoff_session(hass, address, session)
    try:
        yield
    finally:
        await discard_handoff_session(hass, address)


@asynccontextmanager
async def omron_poll_ble_telemetry(entry_data: dict) -> AsyncIterator[None]:
    """Mark BLE session active, tick duration each second, finalize elapsed time on exit."""
    connection_coordinator = entry_data["connection_coordinator"]
    duration_coordinator = entry_data["duration_coordinator"]
    started = perf_counter()
    ticker_task: asyncio.Task[None] | None = None

    async def _duration_ticker() -> None:
        while True:
            elapsed_tick = round(perf_counter() - started, 3)
            duration_coordinator.async_set_updated_data(elapsed_tick)
            await asyncio.sleep(1)

    connection_coordinator.async_set_updated_data(True)
    duration_coordinator.async_set_updated_data(0.0)
    ticker_task = asyncio.create_task(_duration_ticker())
    try:
        yield
    finally:
        if ticker_task is not None:
            ticker_task.cancel()
            try:
                await ticker_task
            except asyncio.CancelledError:
                pass
        elapsed = round(perf_counter() - started, 3)
        duration_coordinator.async_set_updated_data(elapsed)
        connection_coordinator.async_set_updated_data(False)
