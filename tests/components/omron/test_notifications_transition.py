"""RACP 와 CTS 읽기가 blesession.Notifications 위에서 예전과 같게 동작하는지."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from blesession.testing import FakeClient

from custom_components.omron.omron_ble import time_sync
from custom_components.omron.omron_ble.const import (
    BP_MEASUREMENT_CHAR_UUID,
    BP_RACP_CHAR_UUID,
    CTS_CHARACTERISTIC_UUID,
    LOCAL_TIME_INFO_UUID,
)
from custom_components.omron.omron_ble import parser as parser_module
from custom_components.omron.omron_ble.parser import OmronBluetoothDeviceData

_MEASUREMENT = bytes.fromhex("00780050005d00")  # 120/80, MAP 93, no extras
_BLS = "00001810-0000-1000-8000-00805f9b34fb"
_CTS = "00001805-0000-1000-8000-00805f9b34fb"


@pytest.fixture(autouse=True)
def _fast_waits(monkeypatch):
    for module, names in (
        (parser_module, ("_RACP_SETTLE_S", "_RACP_MEASUREMENT_TIMEOUT_S", "_RACP_DONE_TIMEOUT_S")),
        (time_sync, ("_CTS_SETTLE_S", "_CTS_NOTIFY_TIMEOUT_S")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, 0.01)


class _Services:
    """FakeServices only answers get_service; the code under test looks characteristics up directly."""

    def __init__(self, services):
        self._services = services

    def get_characteristic(self, uuid):
        for service in self._services.by_uuid.values():
            found = service.get_characteristic(uuid)
            if found is not None:
                return found
        return None


class _Client(FakeClient):
    def __init__(self):
        super().__init__()
        self.services = _Services(self.services)

    def add_characteristic(self, service_uuid, char_uuid, **kwargs):
        # the wrapper above owns the lookup; add through the wrapped table
        wrapped = self.services
        self.services = wrapped._services
        try:
            return super().add_characteristic(service_uuid, char_uuid, **kwargs)
        finally:
            self.services = wrapped

    async def read_gatt_char(self, characteristic):
        return b"\x00" * 10


def _racp_client(*, measurement: bytes | None, racp: bytes | None) -> _Client:
    client = _Client()
    client.add_characteristic(_BLS, BP_MEASUREMENT_CHAR_UUID)
    client.add_characteristic(_BLS, BP_RACP_CHAR_UUID)

    def on_write(characteristic, data):
        if measurement is not None:
            client.reply(measurement, BP_MEASUREMENT_CHAR_UUID)
        if racp is not None:
            client.reply(racp, BP_RACP_CHAR_UUID)

    client.on_write = on_write
    return client


async def _read(client):
    owner = SimpleNamespace(_bls_racp_unavailable_logged=False)
    return await OmronBluetoothDeviceData._read_latest_via_bls_racp(owner, client)


@pytest.mark.asyncio
async def test_racp_returns_the_record_and_releases_both_subscriptions(monkeypatch):
    client = _racp_client(measurement=_MEASUREMENT, racp=b"\x06\x00\x01\x01")
    record = await _read(client)
    assert record is not None
    assert client.writes == [(BP_RACP_CHAR_UUID, b"\x01\x06", True)]
    assert client.subscribed == {}, "구독을 풀지 않았다"


@pytest.mark.asyncio
async def test_racp_does_not_need_the_completion_indication():
    client = _racp_client(measurement=_MEASUREMENT, racp=None)
    # The completion wait is bounded (1.5 s); shorten it by answering nothing.
    record = await _read(client)
    assert record is not None
    assert client.subscribed == {}


@pytest.mark.asyncio
async def test_racp_without_a_measurement_gives_none_and_still_unsubscribes():
    client = _racp_client(measurement=None, racp=b"\x06\x00\x01\x01")
    assert await _read(client) is None
    assert client.subscribed == {}


@pytest.mark.asyncio
async def test_racp_skips_an_empty_notification_and_takes_the_next_real_one():
    client = _racp_client(measurement=None, racp=None)

    def on_write(characteristic, data):
        client.reply(b"", BP_MEASUREMENT_CHAR_UUID)
        client.reply(_MEASUREMENT, BP_MEASUREMENT_CHAR_UUID)

    client.on_write = on_write
    assert await _read(client) is not None


@pytest.mark.asyncio
async def test_racp_keeps_a_measurement_that_arrives_before_the_write():
    client = _racp_client(measurement=None, racp=None)
    original = client.start_notify

    async def start_notify(characteristic, handler):
        await original(characteristic, handler)
        if characteristic == BP_MEASUREMENT_CHAR_UUID:
            client.reply(_MEASUREMENT, characteristic)

    client.start_notify = start_notify
    assert await _read(client) is not None


@pytest.mark.asyncio
async def test_cts_notification_before_the_write_is_accepted():
    client = _Client()
    client.add_characteristic(_CTS, CTS_CHARACTERISTIC_UUID)
    original = client.start_notify

    async def start_notify(characteristic, handler):
        await original(characteristic, handler)
        client.reply(b"\x00" * 10, characteristic)

    client.start_notify = start_notify
    assert await time_sync._sync_time_via_cts(client, "HEM-TEST") is True


@pytest.mark.asyncio
async def test_cts_sync_writes_after_the_snapshot_and_releases_the_subscription():
    client = _Client()
    client.add_characteristic(_CTS, CTS_CHARACTERISTIC_UUID)
    client.add_characteristic(_CTS, LOCAL_TIME_INFO_UUID)
    client.get_services = None  # no refresh on this backend
    assert await time_sync._sync_time_via_cts(client, "HEM-TEST") is True
    written = [uuid for uuid, _data, _resp in client.writes]
    assert written == [CTS_CHARACTERISTIC_UUID, LOCAL_TIME_INFO_UUID]
    assert client.subscribed == {}


@pytest.mark.asyncio
async def test_cts_sync_still_runs_when_the_device_never_notifies():
    client = _Client()
    client.add_characteristic(_CTS, CTS_CHARACTERISTIC_UUID)
    assert await time_sync._sync_time_via_cts(client, "HEM-TEST") is True
