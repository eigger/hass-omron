"""커스텀키(CUSTOM_KEY) 페어링 회귀 테스트.

HEM-7155T 계열은 RX notification 을 켜는 순간 SMP security request 를 올린다.
CUSTOM_KEY 프로필은 OS 본딩을 하지 않으므로 connect 경로의
``_bluez_pairing_agent()`` 블록을 타지 않으므로, 로컬 BlueZ 링크에서는 Just
Works 확인이 무응답으로 남아 커프가 링크를 끊었다(이슈 #125). 그 뒤 언락 재시도
루프가 이미 죽은 클라이언트를 10회 두드리고 "characteristic was not found" 로
보고해서 원인이 GATT 캐시 문제인 것처럼 보였다.

여기서는 두 가지를 고정한다:
  * 로컬 BlueZ 링크에서만 에이전트를 등록한다(프록시 링크는 시스템 기본
    에이전트를 가로채면 안 된다 — ``_connect_once`` 주석 참고).
  * 링크가 끊긴 상태면 재시도를 접고 원인이 드러나는 에러를 낸다.
"""
import asyncio
from contextlib import asynccontextmanager

import pytest

from custom_components.omron.omron_ble.devices import DeviceConfig, HostPairingMode
from custom_components.omron.omron_ble.omron_driver import OmronDeviceSession
from custom_components.omron.omron_ble import omron_driver


class FakeBLEDevice:
    """habluetooth 가 넘겨주는 BLEDevice 의 details 만 흉내낸다.

    로컬 BlueZ 어댑터는 DBus path 를 싣고(bleak 의 BlueZ 백엔드 자신이
    ``ble_device.details["path"]`` 를 읽는다), 원격 프록시 스캐너는 source 만
    싣는다. 링크 종류 판별은 이 값으로만 해야 한다.
    """

    def __init__(self, details):
        self.address = "00:5F:BF:F4:43:D1"
        self.details = details


LOCAL_BLUEZ_DEVICE_DETAILS = {
    "path": "/org/bluez/hci0/dev_00_5F_BF_F4_43_D1",
    "props": {},
    "source": "DC:A6:32:89:6D:9C",
}
PROXY_DEVICE_DETAILS = {"source": "DC:A6:32:89:6D:9C", "address_type": "public"}


class FakeClient:
    """실제 BleakClient 의 속성 표면을 흉내낸 최소 클라이언트.

    실물 ``BleakClient`` 에는 ``details`` 가 아예 없고, BlueZ 백엔드는
    ``_device`` 객체가 아니라 ``_device_path`` 문자열을 들고 있다. details dict
    를 달아둔 가짜를 쓰면 ``_bluez_device_path(client)`` 가 실제로는 절대
    성공하지 못하는데도 테스트만 통과한다 — 그래서 여기서는 달지 않는다.
    """

    def __init__(self, *, connected=True, start_notify_error=None, device_path=None):
        self.address = "00:5F:BF:F4:43:D1"
        self.is_connected = connected
        self._backend = _FakeBlueZBackend(device_path)
        self.start_notify_calls: list[str] = []
        self._start_notify_error = start_notify_error

    async def start_notify(self, uuid, callback):
        self.start_notify_calls.append(uuid)
        if self._start_notify_error is not None:
            raise self._start_notify_error


class _FakeBlueZBackend:
    def __init__(self, device_path):
        self._device_path = device_path


def _custom_key_session(ble_device, client) -> OmronDeviceSession:
    config = DeviceConfig(
        model="HEM-7155T",
        host_pairing_mode=HostPairingMode.CUSTOM_KEY,
        aggressive_gatt_timing=True,
    )
    session = OmronDeviceSession(ble_device, config)
    session._client = client
    return session


@pytest.fixture
def agent_recorder(monkeypatch):
    """``_bluez_pairing_agent()`` 진입 여부를 기록한다."""
    entered: list[bool] = []

    @asynccontextmanager
    async def _fake_agent():
        entered.append(True)
        yield None

    monkeypatch.setattr(omron_driver, "_bluez_pairing_agent", _fake_agent)
    return entered


@pytest.fixture
def pair_custom_key_recorder(monkeypatch):
    """``_pair_custom_key`` 호출 시점을 기록(실제 GATT I/O 는 건너뜀)."""
    calls: list[bytearray] = []

    async def _fake_pair_custom_key(self, pair_key):
        calls.append(pair_key)

    monkeypatch.setattr(
        OmronDeviceSession, "_pair_custom_key", _fake_pair_custom_key
    )
    return calls


def test_local_bluez_custom_key_pairing_registers_agent(
    agent_recorder, pair_custom_key_recorder
):
    """로컬 BlueZ 링크(details 에 DBus path 존재)면 에이전트를 띄운 채 페어링한다."""
    session = _custom_key_session(
        FakeBLEDevice(LOCAL_BLUEZ_DEVICE_DETAILS), FakeClient()
    )

    asyncio.run(session.pair())

    assert agent_recorder == [True]
    assert len(pair_custom_key_recorder) == 1


def test_proxy_custom_key_pairing_does_not_register_agent(
    agent_recorder, pair_custom_key_recorder
):
    """프록시 링크에서는 에이전트를 등록하지 않는다.

    에이전트는 ``RequestDefaultAgent`` 로 시스템 *기본* 에이전트가 되므로,
    원격 프록시 세션에서 띄우면 무관한 로컬 장치의 페어링 확인까지 가로챈다.
    ``pair()`` 는 config flow 뿐 아니라 페어링 모드 광고를 볼 때마다 폴링
    경로(parser.py)에서도 호출되므로 이 구분이 특히 중요하다.
    """
    session = _custom_key_session(
        FakeBLEDevice(PROXY_DEVICE_DETAILS), FakeClient()
    )

    asyncio.run(session.pair())

    assert agent_recorder == []
    assert len(pair_custom_key_recorder) == 1


def test_unlock_subscribe_reports_disconnect_instead_of_missing_characteristic(
    monkeypatch,
):
    """링크가 끊겼으면 첫 실패에서 중단하고 끊김을 그대로 보고한다."""
    monkeypatch.setattr(omron_driver, "_bleak_refresh_services", _noop_refresh)

    client = FakeClient(
        connected=False,
        start_notify_error=Exception("[org.bluez.Error.NotConnected] Not Connected"),
    )
    session = _custom_key_session(FakeBLEDevice(LOCAL_BLUEZ_DEVICE_DETAILS), client)

    with pytest.raises(ConnectionError) as excinfo:
        asyncio.run(session._pair_custom_key(bytearray(16)))

    message = str(excinfo.value)
    assert "dropped the link" in message
    assert "was not found" not in message
    # The SMP trigger's own failure, which used to go only to a debug log (#2).
    assert "Not Connected" in message
    # RX notify만 1회. 죽은 링크에는 언락 구독을 시도하지도 않는다.
    assert len(client.start_notify_calls) == 1


def test_unlock_subscribe_still_retries_while_connected(monkeypatch):
    """연결이 살아있는 동안은 기존 재시도 동작(10회)을 유지한다."""
    monkeypatch.setattr(omron_driver, "_bleak_refresh_services", _noop_refresh)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    client = FakeClient(connected=True, start_notify_error=Exception("not ready yet"))
    session = _custom_key_session(FakeBLEDevice(LOCAL_BLUEZ_DEVICE_DETAILS), client)

    with pytest.raises(ConnectionError) as excinfo:
        asyncio.run(session._pair_custom_key(bytearray(16)))

    assert "was not found" in str(excinfo.value)
    # RX notify 1회 + 언락 구독 10회(aggressive_gatt_timing).
    assert len(client.start_notify_calls) == 1 + omron_driver._PAIR_UNLOCK_ATTEMPTS_AGGRESSIVE


async def _noop_refresh(client):
    return None


async def _noop_sleep(delay, *args, **kwargs):
    return None


def test_bluez_device_path_reads_the_client_and_the_device():
    """로컬 BlueZ 링크는 클라이언트로도, BLEDevice 로도 판별돼야 한다.

    실물 ``BleakClient`` 에는 ``details`` 가 없고 BlueZ 백엔드가 ``_device``
    객체가 아니라 ``_device_path`` 문자열을 들고 있다. 그 문자열을 안 읽으면
    클라이언트는 로컬 링크에서도 None 을 돌려주고, 본드 조회가 통째로
    "판단 불가"가 된다 (#92).
    """
    path = "/org/bluez/hci0/dev_00_5F_BF_F4_43_D1"
    assert omron_driver._bluez_device_path(FakeClient(device_path=path)) == path
    assert (
        omron_driver._bluez_device_path(FakeBLEDevice(LOCAL_BLUEZ_DEVICE_DETAILS))
        == path
    )
    # 프록시 링크에는 어느 쪽에도 경로가 없다.
    assert omron_driver._bluez_device_path(FakeClient()) is None
    assert omron_driver._bluez_device_path(FakeBLEDevice(PROXY_DEVICE_DETAILS)) is None


def test_bluez_target_falls_back_to_the_device():
    """클라이언트가 경로를 못 내면 BLEDevice 로 넘어간다."""
    path = "/org/bluez/hci0/dev_00_5F_BF_F4_43_D1"
    device = FakeBLEDevice(LOCAL_BLUEZ_DEVICE_DETAILS)

    with_path = _custom_key_session(device, FakeClient(device_path=path))
    assert with_path._bluez_target() is with_path._client

    without = _custom_key_session(device, FakeClient())
    assert without._bluez_target() is device

    proxy = _custom_key_session(FakeBLEDevice(PROXY_DEVICE_DETAILS), FakeClient())
    assert omron_driver._bluez_device_path(proxy._bluez_target()) is None


def test_pairing_error_names_both_failures(monkeypatch):
    """링크가 언락 구독 도중에 죽으면 SMP 촉발 실패까지 함께 보고한다."""
    monkeypatch.setattr(omron_driver, "_bleak_refresh_services", _noop_refresh)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    class _DiesOnUnlock(FakeClient):
        """RX notify 는 실패하되 링크는 살아있고, 언락 구독에서 끊긴다."""

        async def start_notify(self, uuid, callback):
            self.start_notify_calls.append(uuid)
            if len(self.start_notify_calls) > 1:
                self.is_connected = False
            raise Exception(
                "rx failed"
                if len(self.start_notify_calls) == 1
                else "unlock failed"
            )

    client = _DiesOnUnlock(connected=True)
    session = _custom_key_session(FakeBLEDevice(LOCAL_BLUEZ_DEVICE_DETAILS), client)

    with pytest.raises(ConnectionError) as excinfo:
        asyncio.run(session._pair_custom_key(bytearray(16)))

    message = str(excinfo.value)
    assert "unlock failed" in message
    assert "rx failed" in message, "the SMP trigger's failure was swallowed again"
