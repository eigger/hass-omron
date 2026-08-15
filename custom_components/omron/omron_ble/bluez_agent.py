"""BlueZ Just Works 페어링 에이전트.

이 모듈에는 의도적으로 ``from __future__ import annotations`` 를 넣지 않는다.
dbus_fast 는 런타임 어노테이션 값에서 D-Bus 시그니처를 만들기 때문에,
PEP 563 으로 문자열화되면 클래스 정의 시점에
``Argument 'signature' has incorrect type (expected str, got NoneType)`` 로 죽는다.
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
    def RequestConfirmation(self, device: "o", passkey: "u") -> None:  # type: ignore[override]  # noqa: F821
        _LOGGER.debug("BlueZ agent: auto-confirming passkey %06d", passkey)

    @dbus_method()
    def RequestPasskey(self, device: "o") -> "u":  # type: ignore[override]  # noqa: F821
        return 0

    @dbus_method()
    def RequestAuthorization(self, device: "o") -> None:  # type: ignore[override]  # noqa: F821
        pass

    @dbus_method()
    def Cancel(self) -> None:  # type: ignore[override]
        pass
