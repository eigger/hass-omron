"""마지막 성공 판독 타임스탬프의 의미를 고정한다 (#91).

폴은 실패해도 엔티티를 지우지 않고 마지막 값을 유지하도록 설계돼 있다 —
혈압계는 측정 사이에 대부분 꺼져 있고, 그때마다 엔티티를 unavailable 로 만들
수는 없기 때문이다. 대가로, 아무것도 못 읽은 실행과 조용한 실행이 밖에서 보면
똑같아진다. 이슈 #91 에서 제보자가 본 것이 정확히 그것이다: 메모리 세션 시도
3회가 전부 실패했는데 `Finished fetching ... (success: True)` 가 찍혔다.

Last Readout 센서는 그 구분을 위해 존재한다. 따라서 스탬프는 **레코드를 실제로
디코딩한 폴에서만** 찍혀야 한다. 실패한 폴이나 레코드가 없는 폴이 스탬프를
움직이는 순간 센서는 "성공했다"는 거짓말을 하게 되고, 없느니만 못해진다.

실제 파서 객체에 가짜 GATT 세션과 가짜 드라이버 결과를 물려 돌린다.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.omron import async_poll_data
from custom_components.omron.const import CONF_DEVICE_MODEL, DOMAIN
from custom_components.omron.omron_ble import OmronBluetoothDeviceData
from custom_components.omron.omron_ble import parser as parser_module
from custom_components.omron.omron_ble.session_trace import SessionTrace

from tests.components.omron.poll_fakes import (
    ADDRESS,
    FakeSession,
    ble_device,
    parser_with_session,
)

_RECORD = {
    "datetime": dt.datetime(2026, 9, 18, 21, 54),
    "sys": 120,
    "dia": 80,
    "bpm": 60,
    "user": 1,
}


def _readout_target(latest_by_user):
    data = OmronBluetoothDeviceData(device_model="HEM-7361T")
    data._driver.write_user_profile = AsyncMock()
    data._driver.get_latest_records_per_user = AsyncMock(return_value=latest_by_user)
    data._driver.complete_measurement_readout = AsyncMock()
    session = MagicMock()
    session.trace = SessionTrace()
    session._pairing_session = False
    client = MagicMock()
    client.services.get_characteristic.return_value = None
    return data, session, client


async def _readout(data, session, client):
    with patch.object(parser_module, "async_sync_eeprom_time", AsyncMock()):
        await data._poll_device_readout(
            session,
            client,
            SimpleNamespace(address=ADDRESS),
            memory_session_active=True,
        )


class TestLastReadoutStamp:
    async def test_a_decoded_record_stamps_the_readout(self):
        data, session, client = _readout_target({1: dict(_RECORD)})
        assert data.last_readout_at is None

        before = dt.datetime.now(dt.timezone.utc)
        await _readout(data, session, client)

        assert data.last_readout_at is not None
        assert data.last_readout_at >= before

    async def test_a_cuff_with_nothing_stored_does_not_stamp(self):
        """커프가 비어 있는 것은 실패가 아니지만 '판독한 레코드'도 없다."""
        data, session, client = _readout_target({})

        await _readout(data, session, client)

        assert data.last_readout_at is None

    async def test_the_stamp_is_not_set_by_a_failed_poll(self, monkeypatch):
        """실패한 폴(핸들러를 지나는 경로)은 스탬프를 건드리지 않는다."""
        real_sleep = asyncio.sleep

        async def fast_sleep(_delay):
            await real_sleep(0)

        monkeypatch.setattr(parser_module.asyncio, "sleep", fast_sleep)

        # Every memory-session attempt, and the fallback, fails.
        async def fail():
            raise OSError("memory session refused")

        for session in (
            FakeSession(has_service=False),
            FakeSession(unlock_steps=[fail]),
        ):
            data = parser_with_session(session)
            with pytest.raises((ConnectionError, OSError)):
                await data.async_poll(ble_device())
            assert data.last_readout_at is None

    async def test_a_failure_after_a_good_poll_keeps_the_earlier_stamp(self):
        data, session, client = _readout_target({1: dict(_RECORD)})
        await _readout(data, session, client)
        stamped = data.last_readout_at

        failing = parser_with_session(FakeSession(has_service=False))
        failing._last_readout_at = stamped
        with pytest.raises(ConnectionError):
            await failing.async_poll(ble_device())

        assert failing.last_readout_at == stamped


async def test_the_poll_publishes_it_to_its_own_coordinator(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """센서는 readout_coordinator 를 통해서만 값을 받는다."""
    from bleak.backends.device import BLEDevice

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="HEM-7155T EEFF",
        data={CONF_DEVICE_MODEL: "HEM-7155T"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    stamp = dt.datetime(2026, 9, 18, 21, 54, tzinfo=dt.timezone.utc)

    async def poll(device, preconnected_session=None):
        runtime.device_data._last_readout_at = stamp
        return runtime.device_data._finish_update()

    with (
        patch(
            "custom_components.omron.async_ble_device_from_address",
            return_value=BLEDevice(ADDRESS, "HEM-7155T", {}),
        ),
        patch.object(runtime.device_data, "async_poll", poll),
    ):
        assert runtime.readout_coordinator.data is None
        await async_poll_data(hass, entry)

    assert runtime.readout_coordinator.data == stamp
    assert await hass.config_entries.async_unload(entry.entry_id)
