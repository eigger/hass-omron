"""A stand-in GATT session for driving the real ``async_poll`` / readout code.

Only the device boundary is faked: the parser, its driver selection, the
retry loops and the exception handling are the integration's own.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.omron.omron_ble.session_trace import SessionTrace

ADDRESS = "AA:BB:CC:DD:EE:FF"


class Hang:
    """An awaitable step that never finishes on its own; counts its starts."""

    def __init__(self) -> None:
        self.starts = 0
        self.started = asyncio.Event()
        self._released = asyncio.Event()

    def release(self) -> None:
        self._released.set()

    async def __call__(self) -> None:
        self.starts += 1
        self.started.set()
        await self._released.wait()


def raising(exc: BaseException):
    async def step() -> None:
        raise exc

    return step


async def succeed() -> None:
    return None


class _Unlock:
    def __init__(self, step) -> None:
        self._step = step

    async def __aenter__(self):
        await self._step()
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False


class FakeSession:
    """Just what ``async_poll`` touches on an ``OmronDeviceSession``.

    ``unlock_steps`` is consumed one per ``memory_session_after_unlock`` entry
    (the last one repeats); each step is an async callable that raises, hangs
    or returns.
    """

    _pairing_session = False
    is_connected = True
    new_credential = None

    def __init__(self, *, has_service: bool = True, unlock_steps=None) -> None:
        self.trace = SessionTrace()
        self.client = MagicMock()
        self.client.services.get_characteristic.return_value = None
        self.memory = SimpleNamespace(memory_session_active=False)
        self.has_service = has_service
        self.unlock_steps = list(unlock_steps or [succeed])
        self.unlock_calls = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def verify_parent_service(self) -> bool:
        if callable(self.has_service):
            return await self.has_service()
        return self.has_service

    def memory_session_after_unlock(self, pair_first: bool = False) -> _Unlock:
        index = min(self.unlock_calls, len(self.unlock_steps) - 1)
        self.unlock_calls += 1
        return _Unlock(self.unlock_steps[index])

    reset_session_state = AsyncMock()
    refresh_services = AsyncMock()

    def publish_link_to(self, trace) -> None:
        return None

    def reclaim_ownership(self) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


def parser_with_session(session: FakeSession, model: str = "BP5465"):
    """A real ``OmronBluetoothDeviceData`` whose only link is ``session``."""
    from custom_components.omron.omron_ble import OmronBluetoothDeviceData

    data = OmronBluetoothDeviceData(device_model=model)
    data._open_session = lambda ble_device, pairing_session=False: session
    # The readout itself is not under test where this is used.
    data._poll_device_readout = AsyncMock()
    return data


def ble_device():
    return SimpleNamespace(address=ADDRESS, name="cuff", details={})
