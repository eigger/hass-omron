"""Shared BLE poll / pairing session telemetry (connection + duration tickers)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter
from typing import TYPE_CHECKING, Any

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

    from .omron_ble.omron_driver import OmronDeviceSession

_LOGGER = logging.getLogger(__name__)


async def stash_handoff_session(
    hass: HomeAssistant, address: str, session: OmronDeviceSession
) -> None:
    """Park a still-open pairing session for the next poll to adopt.

    WLD3.0 cuffs serve data during the pairing session and reject later
    connections, so closing the link here and reconnecting for the poll is
    what makes the follow-up read fail. Parking the session lets
    ``async_poll`` reuse the very link that just bonded.
    """
    handoff = hass.data.setdefault(DOMAIN, {}).setdefault("_setup_sessions", {})
    previous = handoff.get(address)
    if previous is not None and previous is not session:
        # Backstop only: callers check poll_parked_session() first so they do
        # not pair while a link is parked. Overwriting the key without this
        # would drop that link with nothing left to close it.
        _LOGGER.debug(
            "Replacing the session parked for %s; closing the previous link",
            address,
        )
        await discard_handoff_session(hass, address)
    handoff[address] = session.release_for_handoff()


async def poll_parked_session(
    hass: HomeAssistant, address: str, poll_coordinator: DataUpdateCoordinator[Any]
) -> bool:
    """Poll an already-parked session instead of pairing again.

    Returns True when a parked link was found and polled, meaning the caller
    must not pair. A skipped poll leaves its session parked and connected, so
    a retry that paired anyway would open a second BLE link to the same cuff
    — the SMP auth failure this integration serializes against — and would
    replace the parked session without closing it.

    A parked link that has since dropped is discarded and False returned:
    polling it would only reconnect without pairing, which is the connect a
    PER_SESSION cuff refuses, and the caller asked to pair for a reason.
    """
    session = hass.data.get(DOMAIN, {}).get("_setup_sessions", {}).get(address)
    if session is None:
        return False
    if not session.is_connected:
        _LOGGER.debug(
            "The session parked for %s is no longer connected; dropping it so "
            "the caller can pair again",
            address,
        )
        await discard_handoff_session(hass, address)
        return False
    _LOGGER.debug(
        "A session is already parked and connected for %s; polling it instead "
        "of opening a second link",
        address,
    )
    await poll_coordinator.async_refresh()
    return True


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


def has_handoff_session(hass: HomeAssistant, address: str) -> bool:
    """Whether a pairing session is parked for this address, without taking it."""
    return address in hass.data.get(DOMAIN, {}).get("_setup_sessions", {})


def request_poll(hass: HomeAssistant, entry_id: str) -> None:
    """Mark the next poll as asked for by a person rather than by the clock."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
    if entry_data is not None:
        entry_data["user_requested_poll"] = True


def take_poll_request(hass: HomeAssistant, entry_id: str) -> bool:
    """Consume the request flag.

    Consumed whatever the poll goes on to do: a request that ends up skipped
    for some other reason must not leave the flag armed for a later scheduled
    poll nobody asked for.
    """
    entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
    if entry_data is None:
        return False
    return bool(entry_data.pop("user_requested_poll", False))


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


async def run_post_pairing_poll(
    hass: HomeAssistant,
    address: str,
    session: OmronDeviceSession,
    poll_coordinator: DataUpdateCoordinator[Any],
) -> None:
    """Park the freshly paired session and poll at once so it gets adopted.

    Uses ``async_refresh`` rather than ``async_request_refresh``: the latter
    goes through a 10 s debouncer that schedules the poll and returns without
    running it when a refresh fired recently — pressing Refresh Data and then
    Retry Pairing is exactly that — so the poll meant to adopt the parked link
    would not have run by the time this returns.

    Nothing is discarded afterwards. When the poll bails out (no device, BLE
    session lock held) the session stays parked for the next one, the way the
    config flow leaves it: closing it here would send that retry back to the
    reconnect a PER_SESSION cuff refuses. ``async_poll`` closes the link if it
    has dropped by then, and unloading the entry clears whatever is left.
    """
    await stash_handoff_session(hass, address, session)
    await poll_coordinator.async_refresh()


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
