"""평범한 연결 실패가 빨간 트레이스백으로 올라가지 않게 한다 (#133).

``async_poll`` 은 예외를 두 갈래로 나눈다.

- ``ConnectionError`` → warning 한 줄. 커프가 꺼져 있거나, 범위 밖이거나,
  세션 도중 링크가 끊긴 평범한 경우.
- 그 외 ``Exception`` → ``exc_info`` 를 붙인 ERROR. Home Assistant 가
  "This error originated from a custom integration" 배너와 트레이스백으로
  띄운다.

settle-drop 은 전자인데 ``BleakError`` 로 던져지고 있었다. ``BleakError`` 는
``Exception`` 을 직접 상속하므로 후자로 떨어졌고, 서랍에 들어 있는 커프가
매 폴마다 오류로 보고됐다. 지금은 ``ConnectFailed`` (``ConnectionError``
하위)라서 같은 갈래에 남고, 리포트는 그 드롭을 ``failed_detail: settle`` 로
남긴다.

실패한 폴은 예외를 위로 올리고 코디네이터가 마지막 값을 유지한다 (아래
마지막 테스트). 실제 설정 항목과 가짜 GATT 세션으로 돌린다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bleak.backends.device import BLEDevice
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.omron import async_poll_data
from custom_components.omron.const import CONF_DEVICE_MODEL, DOMAIN

from poll_fakes import (
    ADDRESS,
    FakeSession,
    ble_device,
    parser_with_session,
)


async def test_a_settle_drop_is_a_connection_error(monkeypatch):
    """The cuff dropping the link during the post-connect settle, on every
    attempt, is the ordinary case: a ConnectionError, never a BleakError."""
    from blesession import ConnectFailed

    from custom_components.omron.omron_ble import connection as connection_module

    class _Client:
        is_connected = False

        async def disconnect(self):
            pass

    async def _connect(cls, device, name, **kwargs):
        return _Client()  # 연결 직후 끊긴 링크

    async def _no_sleep(_):
        pass

    monkeypatch.setattr(connection_module, "establish_connection", _connect)
    monkeypatch.setattr(connection_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(connection_module, "is_local_adapter", lambda device: False)
    device = SimpleNamespace(address=ADDRESS, details={"source": "AA:BB"})

    with pytest.raises(ConnectFailed, match="settle") as caught:
        await connection_module.establish_connection_with_bond_settle(
            device, "cuff", model="HEM-7155T"
        )

    assert isinstance(caught.value, ConnectionError)
    assert caught.value.detail == "settle"


async def test_the_poll_sorts_the_ordinary_case_from_the_unexpected_one(caplog):
    """A dropped link is one warning line; anything else is an ERROR with the
    traceback attached. Both still fail the poll."""
    ordinary = parser_with_session(FakeSession())
    ordinary._open_session = lambda *a, **k: (_ for _ in ()).throw(
        ConnectionError("cuff is asleep")
    )
    unexpected = parser_with_session(FakeSession())
    unexpected._open_session = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("synthetic bug")
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ConnectionError):
            await ordinary.async_poll(ble_device())
        interrupted = [
            r for r in caplog.records if "Poll interrupted" in r.getMessage()
        ]
        assert len(interrupted) == 1
        assert interrupted[0].levelno == logging.WARNING
        assert interrupted[0].exc_info is None
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

        caplog.clear()
        with pytest.raises(RuntimeError):
            await unexpected.async_poll(ble_device())
        failed = [r for r in caplog.records if "Poll failed" in r.getMessage()]
        assert len(failed) == 1
        assert failed[0].levelno == logging.ERROR
        assert failed[0].exc_info is not None


async def test_a_failed_poll_keeps_the_last_values_and_fails_the_refresh(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """A failed poll does not blank the entities: the coordinator keeps its
    last data and marks the refresh failed."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="HEM-7155T EEFF",
        data={CONF_DEVICE_MODEL: "HEM-7155T"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    async with asyncio.timeout(5):
        while entry.runtime_data.background_tasks:
            await asyncio.sleep(0.01)
    runtime = entry.runtime_data
    device = BLEDevice(ADDRESS, "HEM-7155T", {})
    good = runtime.device_data._finish_update()
    poll = AsyncMock(return_value=good)
    with (
        patch(
            "custom_components.omron.async_ble_device_from_address", return_value=device
        ),
        patch.object(runtime.device_data, "async_poll", poll),
    ):
        await runtime.poll_coordinator.async_refresh()
        assert runtime.poll_coordinator.last_update_success is True
        kept = runtime.poll_coordinator.data
        assert kept is not None

        poll.side_effect = ConnectionError("cuff is asleep")
        await runtime.poll_coordinator.async_refresh()

        assert runtime.poll_coordinator.last_update_success is False
        assert isinstance(runtime.poll_coordinator.last_exception, UpdateFailed)
        assert runtime.poll_coordinator.data is kept
        with pytest.raises(UpdateFailed):
            await async_poll_data(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
