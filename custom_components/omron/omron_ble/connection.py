"""Establishing the BLE link: connect, let bonding settle, refresh the GATT cache."""
from __future__ import annotations

import asyncio
import logging

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from .bluez import _bluez_pairing_agent, is_local_adapter

_LOGGER = logging.getLogger(__name__)

# Wait between establish_connection() returning and the first GATT operation.
# The L2CAP link comes up before encryption settles, and touching GATT in that
# window gives "Characteristic not found" or a silently dropped CCCD write.
_POST_CONNECT_BOND_SETTLE_SEC: float = 1.5
# If the device drops during the post-connect settle (multi-proxy ESPHome
# setups: connection routed through a proxy that did not bond the device),
# re-establish to let habluetooth re-score and possibly pick a working proxy.
# The settle is polled in small steps so a drop is detected immediately instead
# of waiting out the full settle before retrying.
_CONNECT_SETTLE_ATTEMPTS: int = 3
_SETTLE_POLL_STEP_SEC: float = 0.25


async def _bleak_refresh_services(client: BleakClient) -> None:
    """Re-run GATT discovery so characteristics appear after connection."""
    gs = getattr(client, "get_services", None)
    if not callable(gs):
        return
    try:
        await gs()
    except Exception as exc:
        _LOGGER.debug("get_services refresh: %s", exc)


async def _bleak_clear_cache(client: BleakClient) -> bool:
    """Force-drop the backend/proxy GATT cache (a stale cache can hide fe4a).

    No-op on backends without ``clear_cache``.
    """
    cc = getattr(client, "clear_cache", None)
    if not callable(cc):
        return False
    try:
        await cc()
        return True
    except Exception as exc:
        _LOGGER.debug("clear_cache failed (ignored): %s", exc)
        return False


def _connection_source(ble_device: BLEDevice) -> str:
    """Best-effort proxy/adapter id (BLEDevice.details source) for connection logs."""
    details = getattr(ble_device, "details", None)
    if isinstance(details, dict):
        for key in ("source", "scanner", "path"):
            val = details.get(key)
            if val:
                return str(val)
    return "unknown"


def _connected_path(client: BleakClient, ble_device: BLEDevice) -> str:
    """Best-effort identity of the link a connection actually went over.

    ``_connection_source`` names the scanner that saw the advertisement, not
    the radio the connection took, and on a multi-proxy setup only one of them
    holds the bond (#91). The backend is the only place the real answer exists;
    its shapes are private to bleak and bleak-esphome and may change.
    """
    backend = getattr(client, "_backend", None)
    if backend is not None:
        for attr in ("_source", "source"):
            if value := getattr(backend, attr, None):
                return str(value)
        if path := getattr(backend, "_device_path", None):
            return str(path)
    return _connection_source(ble_device)


async def establish_connection_with_bond_settle(
    ble_device: BLEDevice,
    name: str,
    *,
    model: str = "",
    max_attempts: int = _CONNECT_SETTLE_ATTEMPTS,
    pair_on_connect: bool = False,
    hold_pairing_agent: bool = False,
) -> BleakClient:
    """Connect, let bonding/encryption settle, then refresh the GATT cache.

    Retries if the device drops during the settle (common on multi-proxy setups).

    ``pair_on_connect`` bonds before service discovery, which is what the one
    run with a working retained-bond reconnect did (2.7.8-beta.15, local
    BlueZ). It is honoured only over a local adapter: on an ESP32 proxy a pair
    request that does not complete is read as an authentication failure and
    ESP-IDF drops the stored bond from flash (#142), and the same flag brought
    no benefit there when 2.8.3 carried it. Ordinary reconnects never set it --
    the cuff raises its own Security Request and both stacks answer it.
    """
    # A bare BLEDevice is enough: BlueZ routes carry a /org/bluez/... path,
    # proxy routes do not.
    pair_this_attempt = pair_on_connect and is_local_adapter(ble_device)
    # Profiles that never call Pair() still need someone to answer the Just
    # Works confirmation the cuff's own Security Request triggers. The caller
    # holds an agent for the whole session (see OmronDeviceSession.connect);
    # this one only covers the connect itself for callers that do not.
    agent_this_attempt = hold_pairing_agent and is_local_adapter(ble_device)
    if pair_on_connect and not pair_this_attempt:
        _LOGGER.debug(
            "%s: not a local adapter, leaving the bond to pair() after discovery",
            name,
        )
    last_source = "unknown"
    for attempt in range(1, max_attempts + 1):
        source = _connection_source(ble_device)
        last_source = source
        _LOGGER.debug(
            "Connecting to %s [%s] via proxy/source=%s (attempt %d/%d)",
            name, model or "?", source, attempt, max_attempts,
        )
        # Per attempt: a connect that bonded and then dropped during the
        # settle says nothing about the one that replaces it.
        bonded_this_client = False
        try:
            if pair_this_attempt:
                try:
                    # BlueZ 5.72+ leaves the Just Works confirmation unanswered
                    # without a registered agent and fails the pair with
                    # AuthenticationFailed, so the agent is what makes this path
                    # actually bond rather than quietly fall back.
                    async with _bluez_pairing_agent():
                        client = await establish_connection(
                            BleakClient, ble_device, name, pair=True
                        )
                    bonded_this_client = True
                    _LOGGER.info(
                        "%s bonded before service discovery via %s", name, source
                    )
                except (BleakError, TimeoutError, asyncio.TimeoutError) as pair_exc:
                    # Fall back rather than fail: pair() after discovery still
                    # makes the bond, as it does on every build since #142. One
                    # shot only -- retrying the request on each attempt turns
                    # one refusal into several.
                    pair_this_attempt = False
                    _LOGGER.warning(
                        "Connect-time bonding for %s failed (%s: %s); connecting "
                        "without it and leaving the bond to pair()",
                        name,
                        type(pair_exc).__name__,
                        pair_exc,
                    )
                    client = await establish_connection(BleakClient, ble_device, name)
            elif agent_this_attempt:
                async with _bluez_pairing_agent():
                    client = await establish_connection(BleakClient, ble_device, name)
            else:
                client = await establish_connection(BleakClient, ble_device, name)
        except (BleakError, TimeoutError, asyncio.TimeoutError) as connect_exc:
            # An ordinary reconnect (pair_this_attempt False, so nothing above
            # catches this) used to let a transient failure here -- e.g.
            # "failed to discover services, device disconnected" -- escape the
            # loop on the first iteration, spending none of max_attempts's
            # retry budget; that budget only ever covered a settle-drop below.
            # Retry like one instead.
            if attempt == max_attempts:
                raise
            _LOGGER.warning(
                "Connecting to %s via source=%s failed (attempt %d/%d): %s: %s "
                "— retrying",
                name,
                source,
                attempt,
                max_attempts,
                type(connect_exc).__name__,
                connect_exc,
            )
            continue
        # Only the radio that paired holds the bond, so report both paths.
        connected_via = _connected_path(client, ble_device)
        # Read back by pair(), which skips its own bonding only when this
        # connect made the bond: doing it again on the same link rotates the
        # keys just made, and skipping on the profile flag instead would also
        # skip after a fallback and leave no bond at all.
        client._omron_bonded_at_connect = bonded_this_client  # type: ignore[attr-defined]
        _LOGGER.debug(
            "BLE link established to %s (advertised by source=%s, connected via "
            "%s, bonded_this_connect=%s, is_connected=%s); settling up to %.1fs "
            "for bonding/encryption before first GATT op",
            name,
            source,
            connected_via,
            bonded_this_client,
            getattr(client, "is_connected", "?"),
            _POST_CONNECT_BOND_SETTLE_SEC,
        )
        # Settle for bonding/encryption, polling so a drop is caught early.
        waited = 0.0
        while waited < _POST_CONNECT_BOND_SETTLE_SEC:
            await asyncio.sleep(_SETTLE_POLL_STEP_SEC)
            waited += _SETTLE_POLL_STEP_SEC
            if not getattr(client, "is_connected", False):
                break
        if getattr(client, "is_connected", False):
            _LOGGER.debug(
                "Post-settle state for %s via source=%s: is_connected=True",
                name, source,
            )
            await _bleak_refresh_services(client)
            return client

        # Dropped during settle; retry — re-establishing lets habluetooth
        # re-score and possibly route through a working/bonded proxy.
        _LOGGER.warning(
            "%s dropped ~%.2fs into the post-connect settle via source=%s "
            "(attempt %d/%d) — retrying",
            name, waited, source,
            attempt, max_attempts,
        )
        try:
            await client.disconnect()
        except Exception as exc:
            _LOGGER.debug("disconnect after settle-drop ignored: %s", exc)

    # ConnectionError, not BleakError: a cuff that is off, out of range, or
    # drops the link mid-settle is the ordinary case, and async_poll sorts the
    # ordinary case from the unexpected one by exactly this type. BleakError
    # inherits straight from Exception, so this landed in the branch that logs
    # at ERROR with a traceback -- which Home Assistant renders as "This error
    # originated from a custom integration" for what is a cuff sitting in a
    # drawer (#133). Nothing catches BleakError on this path.
    raise ConnectionError(
        f"{name} dropped during the post-connect settle on all "
        f"{max_attempts} attempt(s) (last source={last_source})"
    )
