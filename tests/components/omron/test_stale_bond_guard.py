"""본드가 없으면 "오래된 본드"도 없다 (#92).

AuthenticationFailed 는 두 가지를 뜻할 수 있다.

- 로컬에 본드가 있는데 기기가 그걸 잊었다 → 지우고 다음 연결에서 다시 맺는
  것이 맞다 (#83).
- 본드가 아예 없는데 상대가 페어링을 거절했다 → 지울 것이 없고, 여기서
  포기하면 사용자가 버튼을 눌러 연 페어링 창을 그대로 버린다. 예전 코드가
  안내하던 "retry the poll" 은 그 창이 닫힌 뒤에 도착한다.

둘을 구분하지 않으면 후자가 전자로 처리된다. BlueZ 에 Paired 를 직접 물어서
가른다.

실제 ``OmronDeviceSession._pair_os_bonding`` 을 구동하고, BlueZ D-Bus 경계
(``_bluez_*``)와 클라이언트만 대체한다.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.omron.omron_ble import bluez, session as session_module
from custom_components.omron.omron_ble.devices import get_device_config
from custom_components.omron.omron_ble.session import OmronDeviceSession

_BLUEZ_PATH = "/org/bluez/hci0/dev_C1_8D_32_97_D5_BB"


class _AuthFailed(Exception):
    """BlueZ 가 거절을 알리는 문구를 흉내 낸다."""

    def __init__(self) -> None:
        super().__init__("org.bluez.Error.AuthenticationFailed")


class _Client:
    """pair() 결과를 순서대로 돌려주는 클라이언트."""

    is_connected = True

    def __init__(self, outcomes: list[BaseException | None]) -> None:
        self._outcomes = list(outcomes)
        self.pair_calls = 0
        self.unpair_calls = 0

    async def pair(self, *args, **kwargs) -> bool:
        self.pair_calls += 1
        outcome = self._outcomes.pop(0)
        if outcome is not None:
            raise outcome
        return True

    async def unpair(self) -> bool:
        self.unpair_calls += 1
        return True


class _Bluez:
    """BlueZ 경계를 대신하고 호출 순서를 남긴다."""

    def __init__(self, paired: bool | None) -> None:
        self.paired = paired
        self.calls: list[tuple[str, object]] = []

    async def agent_pair(self, target) -> bool:
        self.calls.append(("agent_pair", target))
        return False  # 클라이언트의 pair() 로 떨어지게 한다

    async def is_paired(self, target):
        self.calls.append(("is_paired", target))
        return self.paired

    async def remove_device(self, target) -> bool:
        self.calls.append(("remove_device", target))
        return True

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _session(client, monkeypatch, paired, *, ble_device=None):
    monkeypatch.setattr(session_module, "_OS_BOND_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(session_module, "_OS_BOND_REFRESH_DELAY_SEC", 0)

    async def _no_refresh(_client) -> None:
        return None

    monkeypatch.setattr(session_module, "_bleak_refresh_services", _no_refresh)
    fake = _Bluez(paired)
    monkeypatch.setattr(session_module, "_bluez_agent_pair", fake.agent_pair)
    monkeypatch.setattr(session_module, "_bluez_is_paired", fake.is_paired)
    monkeypatch.setattr(session_module, "_bluez_remove_device", fake.remove_device)
    target = OmronDeviceSession(
        ble_device or SimpleNamespace(address="C1:8D:32:97:D5:BB", details={}),
        get_device_config("HEM-7386T1"),
    )
    target._client = client
    return target, fake


def test_a_bond_that_exists_is_removed_and_reported(monkeypatch):
    client = _Client([_AuthFailed()])
    target, fake = _session(client, monkeypatch, paired=True)

    with pytest.raises(ConnectionError, match="Stale BLE bond removed"):
        asyncio.run(target._pair_os_bonding())

    assert "remove_device" in fake.names()
    assert client.pair_calls == 1


def test_the_bond_is_not_removed_when_none_exists(monkeypatch):
    """본드가 없는데 지우면 페어링 창을 버린다 (#92)."""
    client = _Client([_AuthFailed(), None])
    target, fake = _session(client, monkeypatch, paired=False)

    asyncio.run(target._pair_os_bonding())

    assert "remove_device" not in fake.names()
    assert client.unpair_calls == 0


def test_an_unknown_bond_state_keeps_the_previous_behaviour(monkeypatch):
    """모르면 예전처럼 지운다 — 추측으로 새 동작을 택하지 않는다."""
    client = _Client([_AuthFailed()])
    target, fake = _session(client, monkeypatch, paired=None)

    with pytest.raises(ConnectionError, match="Stale BLE bond removed"):
        asyncio.run(target._pair_os_bonding())

    assert "remove_device" in fake.names()


def test_the_bond_state_is_read_before_removing(monkeypatch):
    client = _Client([_AuthFailed()])
    target, fake = _session(client, monkeypatch, paired=True)

    with pytest.raises(ConnectionError):
        asyncio.run(target._pair_os_bonding())

    names = fake.names()
    assert names.index("is_paired") < names.index("remove_device"), (
        "본드 존재 여부를 묻기 전에 지운다"
    )


def test_a_refused_first_pairing_retries_inside_the_window(monkeypatch):
    """거절이 곧 포기가 되면 -P- 창을 한 번밖에 못 쓴다."""
    client = _Client([_AuthFailed(), None])
    target, _fake = _session(client, monkeypatch, paired=False)

    asyncio.run(target._pair_os_bonding())

    assert client.pair_calls == 2, (
        "첫 거절에서 비치명 분기가 성공으로 반환했다 — 같은 링크에서 다시 시도해야 한다"
    )


def test_a_refusal_on_every_attempt_is_left_to_the_non_fatal_handling(monkeypatch):
    """시도를 다 쓰면 기존 비치명 처리에 맡긴다 — 본드가 필요 없는 프로필은 영향 없다."""
    client = _Client([_AuthFailed(), _AuthFailed()])
    target, fake = _session(client, monkeypatch, paired=False)

    asyncio.run(target._pair_os_bonding())

    assert client.pair_calls == 2
    assert "remove_device" not in fake.names()


def test_the_probe_answers_none_when_it_cannot_tell():
    """모르면 모른다고 해야 예전 동작으로 안전하게 떨어진다."""
    no_path = SimpleNamespace(details={})
    assert asyncio.run(bluez._bluez_is_paired(no_path)) is None


def test_the_probe_answers_none_when_the_system_bus_is_unreachable(monkeypatch):
    from dbus_fast.aio import message_bus

    class _NoBus:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def connect(self):
            raise OSError("no system bus")

    monkeypatch.setattr(message_bus, "MessageBus", _NoBus)
    device = SimpleNamespace(details={"path": _BLUEZ_PATH})

    assert asyncio.run(bluez._bluez_is_paired(device)) is None


def test_the_bluez_calls_ask_the_target_not_the_client(monkeypatch):
    """클라이언트만 물으면 경로를 못 찾아 가드가 통째로 무력해진다 (#92).

    ``BleakClient`` 에는 ``details`` 가 없고, 로컬 BlueZ 링크의 경로는 BLEDevice
    쪽에 있을 수 있다. ``_bluez_target()`` 이 경로가 있는 쪽을 골라야 한다.
    """
    ble_device = SimpleNamespace(
        address="C1:8D:32:97:D5:BB", details={"path": _BLUEZ_PATH}
    )
    client = _Client([_AuthFailed()])
    target, fake = _session(client, monkeypatch, paired=True, ble_device=ble_device)

    with pytest.raises(ConnectionError):
        asyncio.run(target._pair_os_bonding())

    asked = {name: obj for name, obj in fake.calls}
    assert asked["agent_pair"] is ble_device
    assert asked["is_paired"] is ble_device
    assert asked["remove_device"] is ble_device


def test_the_target_is_the_client_when_the_client_has_the_path(monkeypatch):
    client = _Client([_AuthFailed()])
    client._backend = SimpleNamespace(_device_path=_BLUEZ_PATH)
    target, fake = _session(client, monkeypatch, paired=True)

    with pytest.raises(ConnectionError):
        asyncio.run(target._pair_os_bonding())

    assert all(obj is client for _name, obj in fake.calls)


def test_the_path_probe_reads_the_backend_string():
    """bleak 3 백엔드는 ``_device`` 객체가 아니라 ``_device_path`` 문자열을 든다."""
    client = SimpleNamespace(_backend=SimpleNamespace(_device_path=_BLUEZ_PATH))

    assert bluez._bluez_device_path(client) == _BLUEZ_PATH
    assert bluez.is_local_adapter(client) is True
    assert bluez.is_local_adapter(SimpleNamespace(details={})) is False
