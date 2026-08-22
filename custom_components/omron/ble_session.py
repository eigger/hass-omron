"""Shared BLE poll / pairing session telemetry (connection + duration tickers)."""

from __future__ import annotations

import asyncio
import logging
import time
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


# A poll someone actually asked for stays valid this long. Requests are not
# consumed until a connect is committed, so without a bound a request that
# never got its chance would sit armed and later open an unrelated scheduled
# poll long after the moment that justified it had passed.
POLL_REQUEST_TTL_SECONDS = 300

# Advertisement flags are only refreshed when an MSD decodes. A packet that
# fails its length contract, or a cuff that stops advertising, leaves the last
# values frozen — and a frozen True would reopen exactly the doomed connects
# the gate exists to stop. Past this age the flags are treated as unknown.
ADVERT_FLAG_FRESHNESS_SECONDS = 60


def request_poll(entry_data: dict, source: str, *, now: float | None = None) -> None:
    """Record that something asked for a poll, for the gate to honour.

    ``async_request_refresh`` is debounced by roughly ten seconds, so the poll
    it schedules reads the advertisement flags well after the advertisement
    that prompted it. A cuff that lowered the bit in between would have its
    request silently dropped by the gate — the buttonless-collection path
    being the one most likely to lose that race. Latching the request means
    the gate honours the moment the flag was seen, not the moment the poll
    happened to run.
    """
    entry_data["poll_request"] = (source, time.monotonic() if now is None else now)


def peek_poll_request(
    entry_data: dict, *, now: float | None = None
) -> str | None:
    """Return the pending poll request's source without consuming it.

    Named for the peek because that is the part callers get wrong: the request
    is only cleared once a connect is actually committed, so that a press or
    an advertisement blocked by the session lock is served by the retry
    instead of vanishing. The one thing this does remove is a request that
    aged out — an expired one is not a request any more.
    """
    request = entry_data.get("poll_request")
    if request is None:
        return None
    source, requested_at = request
    if (time.monotonic() if now is None else now) - requested_at > POLL_REQUEST_TTL_SECONDS:
        entry_data.pop("poll_request", None)
        return None
    return source


def should_skip_scheduled_poll(
    config: Any,
    *,
    pairing_mode: bool,
    forced_transfer: bool,
    flags_age: float | None,
    poll_request: str | None,
    handoff_parked: bool,
) -> bool:
    """Whether a poll would be spending a connect on a certain failure.

    Split out of ``_async_poll_data`` so it can be called directly: the
    conditions here are the whole behaviour of the gate, and a test that
    restates them in its own words checks only that someone typed the same
    thing twice.

    ``flags_age`` is seconds since the advertisement flags were last refreshed
    from a decoded MSD, or None if they never were.
    """
    if not config.poll_requires_pairing_window:
        return False
    # Someone asked for this connect — a person, or an advertisement that said
    # a read could work. Either way it is not the clock talking.
    if poll_request is not None:
        return False
    # A parked link is already open and bonded; skipping strands it and loses
    # the reading it was opened for.
    if handoff_parked:
        return False
    if flags_age is None or flags_age > ADVERT_FLAG_FRESHNESS_SECONDS:
        return True
    return not (pairing_mode or forced_transfer)


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


def has_handoff_session(hass: HomeAssistant, address: str) -> bool:
    """Whether a pairing session is parked for ``address``, without taking it.

    The poll gate needs to know this before deciding to skip: a parked link is
    the one connect that is guaranteed to work, so a skip there would strand
    it and throw away the read it was opened for. ``adopt_handoff_session``
    pops, which is exactly what a gate must not do.
    """
    return address in hass.data.get(DOMAIN, {}).get("_setup_sessions", {})


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
