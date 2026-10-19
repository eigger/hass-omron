"""BlueZ Just Works pairing agent.

Deliberately does NOT use ``from __future__ import annotations``: dbus_fast
builds the D-Bus method signatures from the *runtime* annotation values, so
PEP 563 stringisation makes the ``@method()`` decorator fail at class
definition time with
``TypeError: Argument 'signature' has incorrect type (expected str, got NoneType)``.
"""

import logging
from dbus_fast.errors import DBusError
from dbus_fast.service import ServiceInterface, method as dbus_method

_LOGGER = logging.getLogger(__name__)


def _device_segment(device_path: str) -> str:
    """The ``dev_XX_XX_XX_XX_XX_XX`` leaf of a BlueZ device object path."""
    return device_path.rsplit("/", 1)[-1].upper()


class AutoConfirmAgent(ServiceInterface):
    """BlueZ pairing agent that auto-accepts Just Works for one device only.

    The agent is the system default while it is up, so every request is checked
    against the device being paired and anything else is rejected. The adapter
    part of the path is ignored: the same device has one object per adapter.
    """

    def __init__(self, device_path: str):
        super().__init__("org.bluez.Agent1")
        self._segment = _device_segment(device_path)

    def _check(self, device: str) -> None:
        if not (
            device.startswith("/org/bluez/")
            and _device_segment(device) == self._segment
        ):
            _LOGGER.warning("BlueZ agent: rejecting request from %s", device)
            raise DBusError("org.bluez.Error.Rejected", "Not the device being paired")

    @dbus_method()
    def Release(self) -> None:  # type: ignore[override]
        pass

    @dbus_method()
    def RequestPinCode(self, device: "o") -> "s":  # type: ignore[override]  # noqa: F821
        self._check(device)
        return ""

    @dbus_method()
    def DisplayPinCode(self, device: "o", pincode: "s") -> None:  # type: ignore[override]  # noqa: F821
        self._check(device)
        pass

    @dbus_method()
    def RequestPasskey(self, device: "o") -> "u":  # type: ignore[override]  # noqa: F821
        self._check(device)
        return 0

    @dbus_method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:  # type: ignore[override]  # noqa: F821
        self._check(device)
        _LOGGER.debug("BlueZ agent: DisplayPasskey %06d (entered=%d)", passkey, entered)

    @dbus_method()
    def RequestConfirmation(self, device: "o", passkey: "u") -> None:  # type: ignore[override]  # noqa: F821
        self._check(device)
        _LOGGER.debug("BlueZ agent: auto-confirming passkey %06d", passkey)

    @dbus_method()
    def RequestAuthorization(self, device: "o") -> None:  # type: ignore[override]  # noqa: F821
        self._check(device)
        pass

    @dbus_method()
    def AuthorizeService(self, device: "o", uuid: "s") -> None:  # type: ignore[override]  # noqa: F821
        self._check(device)
        pass

    @dbus_method()
    def Cancel(self) -> None:  # type: ignore[override]
        pass
