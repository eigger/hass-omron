"""WLD4.0 본드 정책(REUSE) 및 stale-bond 복구 회귀 테스트.

배경(이슈 #92, HEM-7188T1-LEO):

PER_SESSION 은 "본드를 버려도 다음 연결이 다시 본딩하니까 안전하다"는 전제
위에 서 있는데, 이 커프는 페어링 모드를 벗어나면 새 본드를 AuthenticationCanceled
/ error 102 로 거부한다. 전제가 깨지므로 본드를 버리는 순간 다음 연결은 확정
실패다.

WLD4.0 프로필들이 PER_SESSION 이었던 건 #109 당시 WLD3.0 으로 분류돼 있었기
때문이고, #112 가 WLD4.0 으로 정정하면서 본드 정책은 따라오지 않았다.

REUSE 로 되돌리는 것만으로는 실험이 성립하지 않는다: 커프가 자기 LTK 를 정말
버린다면 저장된 키로 암호화가 거부되는데, stale-bond 복구는
``_pair_os_bonding()`` 안에만 있고 ``pair()`` 는 ``pair_on_connect`` 면 조기
반환하므로 그 경로에 도달하지 못한다. 그래서 복구를 connect 경로에도 연결한다.
"""
import asyncio

import pytest

from custom_components.omron.omron_ble import omron_driver
from custom_components.omron.omron_ble.device_catalog import (
    CANONICAL_DEVICE_PROFILES,
)
from custom_components.omron.omron_ble.devices import BondPolicy, ConnectType


# ── 카탈로그 정책 ──────────────────────────────────────────────────────────

WLD4_PROFILES = ("HEM-7188T1", "HEM-7191T1", "HEM-7196T1")


@pytest.mark.parametrize("profile", WLD4_PROFILES)
def test_wld4_keeps_the_bond_but_still_brings_security_up_first(profile):
    """WLD4.0 은 본드를 유지하되 pair_on_connect 는 그대로 True 여야 한다.

    본드만 유지하고 pair_on_connect 를 잃으면 2.5.23 시절 구성으로 돌아간다 —
    연결 후 GATT 를 건드리는 동안 보안이 올라와 있지 않아 커프가 링크를 끊던
    바로 그 조합(#108). 두 조건이 동시에 성립해야 한다.
    """
    config = CANONICAL_DEVICE_PROFILES[profile]

    assert config.connect_type == ConnectType.WLD4_0
    assert config.bond_policy == BondPolicy.REUSE
    assert config.unpair_after_session is False, "본드가 세션 종료 시 삭제되면 안 된다"
    assert config.pair_on_connect is True, "보안은 GATT 디스커버리 전에 올라와야 한다"


def test_wld3_bond_policy_is_untouched():
    """WLD3.0 패밀리는 검증된 PER_SESSION 동작을 그대로 유지한다."""
    wld3 = [
        (name, cfg)
        for name, cfg in CANONICAL_DEVICE_PROFILES.items()
        if cfg.connect_type == ConnectType.WLD3_0
    ]
    assert wld3, "WLD3.0 프로필이 하나도 없다면 이 가드는 의미가 없다"
    for name, cfg in wld3:
        assert cfg.bond_policy == BondPolicy.PER_SESSION, name
        assert cfg.unpair_after_session is True, name


# ── stale-bond 복구 ────────────────────────────────────────────────────────

class FakeBLEDevice:
    def __init__(self):
        self.address = "D5:4F:40:C4:5A:E2"
        self.details = {
            "path": "/org/bluez/hci0/dev_D5_4F_40_C4_5A_E2",
            "props": {},
            "source": "hci0",
        }


class FakeClient:
    def __init__(self):
        self.is_connected = True


class ConnectRecorder:
    """``_connect_once`` 대역 — pair 인자 시퀀스를 기록한다."""

    def __init__(self, errors):
        self.errors = list(errors)
        self.pair_args: list[bool] = []

    async def __call__(self, ble_device, name, *, pair):
        self.pair_args.append(pair)
        if self.errors:
            exc = self.errors.pop(0)
            if exc is not None:
                raise exc
        return FakeClient()


@pytest.fixture
def settle_free(monkeypatch):
    """post-connect settle 의 실제 대기/GATT 갱신을 제거한다."""
    async def _noop_sleep(delay, *args, **kwargs):
        return None

    async def _noop_refresh(client):
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(omron_driver, "_bleak_refresh_services", _noop_refresh)


@pytest.fixture
def remove_device_recorder(monkeypatch):
    calls: list[str] = []

    async def _fake_remove(obj):
        calls.append(getattr(obj, "address", "?"))
        return True

    monkeypatch.setattr(omron_driver, "_bluez_remove_device", _fake_remove)
    return calls


class FakeBleakError(Exception):
    """conftest 가 bleak 을 MagicMock 으로 대체하므로 실제 예외 클래스가 없다.

    드라이버가 ``except BleakError`` 로 잡을 수 있도록 진짜 예외 클래스를
    심어 준다.
    """


@pytest.fixture(autouse=True)
def real_bleak_error(monkeypatch):
    monkeypatch.setattr(omron_driver, "BleakError", FakeBleakError)


def _stale_bond_error():
    return FakeBleakError(
        "[org.bluez.Error.AuthenticationFailed] Authentication Failed"
    )


def _refused_bond_error():
    return FakeBleakError(
        "[org.bluez.Error.AuthenticationCanceled] Authentication Canceled"
    )


def test_stale_bond_is_cleared_and_rebonded_not_downgraded(
    monkeypatch, settle_free, remove_device_recorder
):
    """AuthenticationFailed 면 본드를 지우고 pair=True 로 다시 시도한다.

    바로 pair=False 로 내려가면 비암호화 링크가 되고, 그 위에서 토큰 언락과
    메모리 세션이 실패한다 — 이슈 #92 에서 보고된 그 경로다.
    """
    connect = ConnectRecorder([_stale_bond_error(), None])
    monkeypatch.setattr(omron_driver, "_connect_once", connect)

    client = asyncio.run(
        omron_driver.establish_connection_with_bond_settle(
            FakeBLEDevice(), "cuff", pair_on_connect=True, model="HEM-7188T1"
        )
    )

    assert isinstance(client, FakeClient)
    assert remove_device_recorder == ["D5:4F:40:C4:5A:E2"], "본드를 지워야 한다"
    # 두 번째 시도도 pair=True — 비암호화 폴백으로 내려가지 않았다.
    assert connect.pair_args == [True, True]


def test_refused_bond_still_falls_back_to_unpaired_connect(
    monkeypatch, settle_free, remove_device_recorder
):
    """stale-bond 가 아닌 거부(error 102 등)는 기존 pair=False 폴백 그대로."""
    connect = ConnectRecorder([_refused_bond_error(), None])
    monkeypatch.setattr(omron_driver, "_connect_once", connect)

    asyncio.run(
        omron_driver.establish_connection_with_bond_settle(
            FakeBLEDevice(), "cuff", pair_on_connect=True, model="HEM-7188T1"
        )
    )

    assert remove_device_recorder == [], "멀쩡한 본드를 지우면 안 된다"
    assert connect.pair_args == [True, False]


def test_stale_bond_recovery_runs_at_most_once(
    monkeypatch, settle_free, remove_device_recorder
):
    """복구 후에도 같은 인증 실패가 나면 본드 문제가 아니다 — 다시 지우지 않는다."""
    connect = ConnectRecorder(
        [_stale_bond_error(), _stale_bond_error(), None]
    )
    monkeypatch.setattr(omron_driver, "_connect_once", connect)

    asyncio.run(
        omron_driver.establish_connection_with_bond_settle(
            FakeBLEDevice(), "cuff", pair_on_connect=True, model="HEM-7188T1"
        )
    )

    assert len(remove_device_recorder) == 1
    # 지우고 재본딩(True) → 또 실패 → 비암호화 폴백(False).
    assert connect.pair_args == [True, True, False]


def test_no_bond_recovery_when_not_pairing_on_connect(
    monkeypatch, settle_free, remove_device_recorder
):
    """pair_on_connect=False 프로필에서는 이 경로가 아예 열리지 않는다."""
    connect = ConnectRecorder([_stale_bond_error()])
    monkeypatch.setattr(omron_driver, "_connect_once", connect)

    with pytest.raises(Exception):
        asyncio.run(
            omron_driver.establish_connection_with_bond_settle(
                FakeBLEDevice(), "cuff", pair_on_connect=False, model="HEM-7142T2"
            )
        )

    assert remove_device_recorder == []
    assert connect.pair_args == [False]
