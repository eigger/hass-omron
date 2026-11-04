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
async def _bluez_pairing_agent(device_path: str | None) -> AsyncIterator[Any]:
    """Hold an auto-confirming BlueZ agent registered for the duration.

    Yields the DBus connection the ``KeyboardDisplay`` agent is registered
    on while it is the system default agent, or None when one could not be
    set up (non-Linux, no DBus, no BlueZ). Callers can pair inside the block
    — either via ``Device1.Pair()`` on the yielded bus or by letting bleak
    pair during connect — without the confirmation going unanswered.

    The agent answers only for ``device_path``; with no path nothing is
    registered and None is yielded.
    """
    if not device_path:
        yield None
        return
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
        bus.export(_BLUEZ_AGENT_PATH, AutoConfirmAgent(device_path))
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
        try:
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
        finally:
            # Cancellation during UnregisterAgent must not strand the D-Bus
            # connection after the caller's bounded cleanup gives up.
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

    async with _bluez_pairing_agent(device_path) as bus:
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


async def _bluez_find_device_paths(address_or_path: str) -> list[str]:
    """Find all BlueZ DBus object paths for a MAC address across all adapters."""
    if address_or_path.startswith("/org/bluez/") and "/dev_" in address_or_path:
        return [address_or_path]
    if not isinstance(address_or_path, str) or not address_or_path.strip():
        return []
    dev_suffix = "dev_" + address_or_path.strip().upper().replace(":", "_").replace(
        "-", "_"
    )
    try:
        from dbus_fast.aio.message_bus import MessageBus
        from dbus_fast.constants import BusType, MessageType
        from dbus_fast.message import Message
    except ImportError:
        return []

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as exc:
        _LOGGER.debug("BlueZ find device paths: cannot connect to system bus: %s", exc)
        return []
    try:
        reply = await bus.call(
            Message(
                destination="org.bluez",
                path="/",
                interface="org.freedesktop.DBus.ObjectManager",
                member="GetManagedObjects",
            )
        )
        if reply.message_type != MessageType.METHOD_RETURN or not reply.body:
            return []
        objects: dict[str, Any] = reply.body[0]
        paths: list[str] = []
        for path, interfaces in objects.items():
            if "org.bluez.Device1" in interfaces:
                if str(path).endswith(f"/{dev_suffix}"):
                    paths.append(str(path))
                    continue
                addr_prop = interfaces["org.bluez.Device1"].get("Address")
                if (
                    addr_prop
                    and str(getattr(addr_prop, "value", addr_prop)).upper()
                    == address_or_path.strip().upper()
                ):
                    paths.append(str(path))
        return paths
    except Exception as exc:
        _LOGGER.debug("BlueZ find device paths failed for %s: %s", address_or_path, exc)
        return []
    finally:
        bus.disconnect()


async def _bluez_find_device_path(address_or_path: str) -> str | None:
    """Find a BlueZ DBus object path for a MAC address (first matching path)."""
    paths = await _bluez_find_device_paths(address_or_path)
    return paths[0] if paths else None


async def _bluez_remove_device(client: BleakClient | BLEDevice | str) -> bool:
    """Remove the BlueZ device + bond via DBus Adapter1.RemoveDevice.

    Used because BleakClient.unpair() is a no-op on the HA/habluetooth backend.
    Accepts a bare BLEDevice or MAC address str too, so a bond can still be cleared
    after a connect attempt failed or when an integration entry is removed.
    In multi-adapter environments, removing by address clears the device and its bond
    across all local adapters.
    Returns True on success; False otherwise (caller falls back to unpair()).
    """
    try:
        from dbus_fast.aio.message_bus import MessageBus
        from dbus_fast.constants import BusType, MessageType
        from dbus_fast.message import Message
    except ImportError:
        return False

    if (
        isinstance(client, str)
        and client.startswith("/org/bluez/")
        and "/dev_" in client
    ):
        paths = [client]
    else:
        addr = client if isinstance(client, str) else getattr(client, "address", None)
        paths = await _bluez_find_device_paths(str(addr)) if addr else []
        if not paths and not isinstance(client, str):
            dev_path = _bluez_device_path(client)
            if dev_path:
                paths = [dev_path]

    if not paths:
        return False

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as exc:
        _LOGGER.debug("BlueZ RemoveDevice: cannot connect to system bus: %s", exc)
        return False

    success = False
    try:
        for device_path in paths:
            if not device_path or "/dev_" not in device_path:
                continue
            adapter_path = device_path.rsplit("/", 1)[0]
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
                if ok:
                    success = True
            except Exception as exc:
                _LOGGER.debug("BlueZ RemoveDevice failed for %s: %s", device_path, exc)
        return success
    finally:
        bus.disconnect()


async def _bluez_is_paired(client: BleakClient | BLEDevice | str) -> bool | None:
    """Whether BlueZ currently holds a bond for this device.

    In multi-adapter environments, returns True if the device is paired on ANY
    local adapter, False if found on at least one adapter but none are paired,
    and None when it cannot be determined.
    """
    try:
        from dbus_fast.aio.message_bus import MessageBus
        from dbus_fast.constants import BusType, MessageType
        from dbus_fast.message import Message
    except ImportError:
        return None

    if (
        isinstance(client, str)
        and client.startswith("/org/bluez/")
        and "/dev_" in client
    ):
        paths = [client]
    else:
        addr = client if isinstance(client, str) else getattr(client, "address", None)
        paths = await _bluez_find_device_paths(str(addr)) if addr else []
        if not paths and not isinstance(client, str):
            dev_path = _bluez_device_path(client)
            if dev_path:
                paths = [dev_path]

    if not paths:
        return None

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as exc:
        _LOGGER.debug("BlueZ Paired: cannot connect to system bus: %s", exc)
        return None

    has_valid_read = False
    try:
        for device_path in paths:
            if not device_path or "/dev_" not in device_path:
                continue
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
                if reply.message_type == MessageType.METHOD_RETURN and reply.body:
                    has_valid_read = True
                    if bool(reply.body[0].value):
                        return True
            except Exception as exc:
                _LOGGER.debug("BlueZ Paired read failed for %s: %s", device_path, exc)
        return False if has_valid_read else None
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
    for attr in (
        "dbus_path",
        "name",
        "details",
        "reply",
        "error_name",
        "error_message",
    ):
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
