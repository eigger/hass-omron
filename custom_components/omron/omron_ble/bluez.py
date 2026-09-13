"""BlueZ D-Bus helpers: pairing agent, Pair/RemoveDevice/Paired, and bond error classification.

Everything here is a no-op or returns None off Linux, where there is no BlueZ;
callers keep their previous behaviour rather than act on a guess.
"""
from __future__ import annotations

import logging
import traceback
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from bleak import BleakClient
from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)


_BLUEZ_AGENT_PATH = "/omron/ble/pairagent"


def _bluez_device_path(obj: BleakClient | BLEDevice) -> str | None:
    """Return the BlueZ DBus object path for a client or device, if it has one.

    Only local BlueZ adapters carry a DBus path; devices reached through a
    remote proxy scanner report a ``source`` instead, so a None result also
    answers "is this connection going over BlueZ?".
    """
    details = getattr(obj, "details", None)
    if not isinstance(details, dict):
        details = getattr(getattr(obj, "_device", None), "details", None)
    if isinstance(details, dict) and (path := details.get("path")):
        return str(path)
    backend = getattr(obj, "_backend", None)
    # A BleakClient carries no .details; its BlueZ backend keeps the path as a
    # string. Reading it is what lets a client answer at all (#92).
    if path := getattr(backend, "_device_path", None):
        return str(path)
    # Older bleak versions expose the device on the backend instead.
    return getattr(getattr(backend, "_device", None), "path", None)


@asynccontextmanager
async def _bluez_pairing_agent() -> AsyncIterator[Any]:
    """Hold an auto-confirming BlueZ agent registered for the duration.

    Yields the DBus connection the ``KeyboardDisplay`` agent is registered
    on while it is the system default agent, or None when one could not be
    set up (non-Linux, no DBus, no BlueZ). Callers can pair inside the block
    — either via ``Device1.Pair()`` on the yielded bus or by letting bleak
    pair during connect — without the confirmation going unanswered.
    """
    try:
        from dbus_fast.aio.message_bus import MessageBus
        from dbus_fast.constants import BusType
        from dbus_fast.message import Message

        from .bluez_agent import AutoConfirmAgent
    except Exception as exc:
        _LOGGER.debug("BlueZ agent unavailable (%s); pairing without an agent", exc)
        yield None
        return

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as exc:
        _LOGGER.debug("BlueZ agent: cannot connect to system bus: %s", exc)
        yield None
        return

    registered = False
    agent_ready = False
    try:
        bus.export(_BLUEZ_AGENT_PATH, AutoConfirmAgent())
        await bus.call(
            Message(
                destination="org.bluez",
                path="/org/bluez",
                interface="org.bluez.AgentManager1",
                member="RegisterAgent",
                signature="os",
                body=[_BLUEZ_AGENT_PATH, "KeyboardDisplay"],
            )
        )
        registered = True
        await bus.call(
            Message(
                destination="org.bluez",
                path="/org/bluez",
                interface="org.bluez.AgentManager1",
                member="RequestDefaultAgent",
                signature="o",
                body=[_BLUEZ_AGENT_PATH],
            )
        )
        agent_ready = True
        _LOGGER.debug("BlueZ KeyboardDisplay agent registered")
    except Exception as exc:
        _LOGGER.debug("BlueZ agent registration failed: %s", exc)

    # ``yield`` sits outside the registration try/except above so an
    # exception raised by the caller's code inside the ``async with`` block
    # (e.g. a connect timeout) propagates unchanged. athrow() can only be
    # answered once; yielding a second time from inside that except block
    # raised "generator didn't stop after athrow()" and masked the real
    # error, which also skipped the pair=False fallback in
    # establish_connection_with_bond_settle (it only catches BleakError).
    try:
        yield bus if agent_ready else None
    finally:
        if registered:
            try:
                await bus.call(
                    Message(
                        destination="org.bluez",
                        path="/org/bluez",
                        interface="org.bluez.AgentManager1",
                        member="UnregisterAgent",
                        signature="o",
                        body=[_BLUEZ_AGENT_PATH],
                    )
                )
            except Exception:
                pass
        bus.disconnect()


def is_local_adapter(client: BleakClient | BLEDevice) -> bool:
    """Whether this link runs on a local BlueZ adapter rather than a proxy.

    BlueZ routes carry a ``/org/bluez/...`` object path; ESPHome proxy routes do
    not. Callers that gate connect-time bonding share this so the connect path
    and the config flow cannot drift apart -- closing the probe link on a proxy,
    where no pair request follows, would drop the link the bond was being made
    over and put nothing in its place.
    """
    return bool(_bluez_device_path(client))


async def _bluez_agent_pair(client: BleakClient) -> bool:
    """Pair via BlueZ DBus with a registered KeyboardDisplay agent.

    On BlueZ 5.72+, ``Device1.Pair()`` without a registered agent leaves the
    Just Works confirmation unanswered and fails with ``AuthenticationFailed``.
    A ``KeyboardDisplay`` agent that auto-confirms passkeys fixes it.

    Returns False on non-Linux platforms or when the DBus path is unavailable.
    """
    try:
        from dbus_fast.constants import MessageType
        from dbus_fast.message import Message
    except ImportError:
        return False

    device_path = _bluez_device_path(client)
    if not device_path:
        return False

    async with _bluez_pairing_agent() as bus:
        if bus is None:
            return False
        try:
            _LOGGER.debug("BlueZ agent active; calling Pair() on %s", device_path)
            reply = await bus.call(
                Message(
                    destination="org.bluez",
                    path=device_path,
                    interface="org.bluez.Device1",
                    member="Pair",
                )
            )
            success = reply.message_type == MessageType.METHOD_RETURN
            if success:
                _LOGGER.debug("BlueZ agent pair succeeded for %s", device_path)
            else:
                _LOGGER.debug(
                    "BlueZ agent pair returned non-success for %s: %s",
                    device_path,
                    reply,
                )
            return success
        except Exception as exc:
            _LOGGER.debug("BlueZ agent pair failed for %s: %s", device_path, exc)
            return False
        # The bus belongs to _bluez_pairing_agent, which unregisters the
        # agent and disconnects it on the way out — do not close it here.


async def _bluez_remove_device(client: BleakClient | BLEDevice) -> bool:
    """Remove the BlueZ device + bond via DBus Adapter1.RemoveDevice.

    Used because BleakClient.unpair() is a no-op on the HA/habluetooth backend.
    Accepts a bare BLEDevice too, so a bond can still be cleared after a
    connect attempt failed and there is no client to unpair through.
    Returns True on success; False otherwise (caller falls back to unpair()).
    """
    try:
        from dbus_fast.aio.message_bus import MessageBus
        from dbus_fast.constants import BusType, MessageType
        from dbus_fast.message import Message
    except ImportError:
        return False

    device_path = _bluez_device_path(client)
    if not device_path or "/dev_" not in device_path:
        return False
    # Adapter path is the device path minus the trailing dev_XX_.. segment.
    adapter_path = device_path.rsplit("/", 1)[0]

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as exc:
        _LOGGER.debug("BlueZ RemoveDevice: cannot connect to system bus: %s", exc)
        return False
    try:
        reply = await bus.call(
            Message(
                destination="org.bluez",
                path=adapter_path,
                interface="org.bluez.Adapter1",
                member="RemoveDevice",
                signature="o",
                body=[device_path],
            )
        )
        ok = reply.message_type == MessageType.METHOD_RETURN
        _LOGGER.debug(
            "BlueZ RemoveDevice(%s) -> %s", device_path, "ok" if ok else reply
        )
        return ok
    except Exception as exc:
        _LOGGER.debug("BlueZ RemoveDevice failed for %s: %s", device_path, exc)
        return False
    finally:
        bus.disconnect()


async def _bluez_is_paired(client: BleakClient | BLEDevice) -> bool | None:
    """Whether BlueZ currently holds a bond for this device.

    ``None`` when it cannot be determined -- no dbus_fast, no device path, or
    the property read failed -- so a caller can keep its previous behaviour
    rather than act on a guess.
    """
    try:
        from dbus_fast.aio.message_bus import MessageBus
        from dbus_fast.constants import BusType, MessageType
        from dbus_fast.message import Message
    except ImportError:
        return None

    device_path = _bluez_device_path(client)
    if not device_path or "/dev_" not in device_path:
        return None

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as exc:
        _LOGGER.debug("BlueZ Paired: cannot connect to system bus: %s", exc)
        return None
    try:
        reply = await bus.call(
            Message(
                destination="org.bluez",
                path=device_path,
                interface="org.freedesktop.DBus.Properties",
                member="Get",
                signature="ss",
                body=["org.bluez.Device1", "Paired"],
            )
        )
        if reply.message_type != MessageType.METHOD_RETURN or not reply.body:
            _LOGGER.debug("BlueZ Paired(%s) -> %s", device_path, reply)
            return None
        return bool(reply.body[0].value)
    except Exception as exc:
        _LOGGER.debug("BlueZ Paired read failed for %s: %s", device_path, exc)
        return None
    finally:
        bus.disconnect()


def _is_non_fatal_os_pairing_error(exc: BaseException) -> bool:
    """Whether an OS-level BLE pairing exception can be safely ignored.

    Modern-stack Omron devices (pairing=false in ubpm) do not require an
    explicit pair() call; the BLE stack negotiates security automatically
    when GATT operations are performed.  Therefore most pair() errors on
    these devices are non-fatal and should not block the config flow.
    """
    msg = str(exc).lower()
    non_fatal_markers = (
        "alreadyexists",
        "already exists",
        "already paired",
        "already bonded",
        "authentication canceled",
        "authenticationcanceled",
        "authentication cancelled",
        "authenticationcancelled",
        "authenticationfailed",
        "authentication failed",
        "authenticationrejected",
        "authentication rejected",
        "notready",
        "not ready",
        "in progress",
    )
    return any(marker in msg for marker in non_fatal_markers)


def _is_stale_bond_auth_error(exc: BaseException) -> bool:
    """Whether a bonding failure looks like a stale/rotated bond (AuthenticationFailed)."""
    msg = str(exc).lower()
    return any(
        marker in msg
        for marker in (
            "authenticationfailed",
            "authentication failed",
            "authenticationrejected",
            "authentication rejected",
        )
    )


def _log_pairing_failure_detail(prefix: str, exc: BaseException) -> None:
    """Emit structured detail for BLE bonding/pairing failures."""
    lines = [
        prefix,
        f"  type: {type(exc).__module__}.{type(exc).__name__}",
        f"  str: {exc!s}",
        f"  repr: {exc!r}",
    ]
    dbus_error = getattr(exc, "dbus_error", None)
    if dbus_error is not None:
        lines.append(f"  dbus_error: {dbus_error!s}")
    for attr in ("dbus_path", "name", "details", "reply", "error_name", "error_message"):
        val = getattr(exc, attr, None)
        if val is not None:
            lines.append(f"  {attr}: {val!r}")

    cause = exc.__cause__
    depth = 0
    while cause is not None and depth < 8:
        lines.append(
            f"  __cause__[{depth}]: "
            f"{type(cause).__module__}.{type(cause).__name__}: {cause!s}"
        )
        cause = cause.__cause__
        depth += 1

    _LOGGER.error("\n".join(lines))
    tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    _LOGGER.debug("%s (full traceback)\n%s", prefix, "".join(tb_lines))
