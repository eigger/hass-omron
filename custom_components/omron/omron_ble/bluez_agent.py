"""BlueZ Just Works pairing agent.

Deliberately does NOT use ``from __future__ import annotations``: dbus_fast
builds the D-Bus method signatures from the *runtime* annotation values, so
PEP 563 stringisation makes the ``@method()`` decorator fail at class
definition time with
``TypeError: Argument 'signature' has incorrect type (expected str, got NoneType)``.
"""

import logging
from dbus_fast.service import ServiceInterface, method as dbus_method

_LOGGER = logging.getLogger(__name__)


class AutoConfirmAgent(ServiceInterface):
    """Minimal BlueZ pairing agent that auto-accepts Just Works."""

    def __init__(self):
        super().__init__("org.bluez.Agent1")

    @dbus_method()
    def Release(self) -> None:  # type: ignore[override]
        pass

    @dbus_method()
    def RequestPinCode(self, device: "o") -> "s":  # type: ignore[override]  # noqa: F821
        return ""

    @dbus_method()
    def DisplayPinCode(self, device: "o", pincode: "s") -> None:  # type: ignore[override]  # noqa: F821
        pass

    @dbus_method()
    def RequestPasskey(self, device: "o") -> "u":  # type: ignore[override]  # noqa: F821
        return 0

    @dbus_method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:  # type: ignore[override]  # noqa: F821
        _LOGGER.debug("BlueZ agent: DisplayPasskey %06d (entered=%d)", passkey, entered)

    @dbus_method()
    def RequestConfirmation(self, device: "o", passkey: "u") -> None:  # type: ignore[override]  # noqa: F821
        _LOGGER.debug("BlueZ agent: auto-confirming passkey %06d", passkey)

    @dbus_method()
    def RequestAuthorization(self, device: "o") -> None:  # type: ignore[override]  # noqa: F821
        pass

    @dbus_method()
    def AuthorizeService(self, device: "o", uuid: "s") -> None:  # type: ignore[override]  # noqa: F821
        pass

    @dbus_method()
    def Cancel(self) -> None:  # type: ignore[override]
        pass
