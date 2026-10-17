"""WLD3.0/WLD4.0 본드 유지 회귀 테스트 (#91).

본드를 지키는 것은 세션 종료 시 CCCD 를 끄지 않는 것
(``keep_notify_subscriptions``) 하나다. 이 파일은 그 설정과, 그것을
무용지물로 만드는 주변 동작들을 고정한다.

이슈 #91 의 BP5465 폰 HCI 캐처에서 공식 앱은 평상 동기화에서
페어링을 전혀 하지 않는다 — 커프가 연결 후 ~53ms 에 SMP Security
Request 를 올리고, 폰은 이미 들고 있는 본드로 암호화를 재개한다.

연결 경로는 실제 ``establish_connection_with_bond_settle`` /
``OmronDeviceSession`` 을, 설정 플로우는 실제 HA 플로우 매니저를 구동하고
BLE 경계(``establish_connection``, GATT 세션)만 대체한다.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from homeassistant.config_entries import SOURCE_BLUETOOTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.omron.config_flow as config_flow
from custom_components.omron.const import CONF_DEVICE_MODEL, DOMAIN
from custom_components.omron.omron_ble import (
    connection,
    pairing,
    parser,
    session as session_module,
)
from custom_components.omron.omron_ble.const import OMRON_MANUFACTURER_ID
from custom_components.omron.omron_ble.device_catalog import (
    CANONICAL_DEVICE_PROFILES,
)
from custom_components.omron.omron_ble.devices import (
    ConnectType,
    get_device_config,
    resolve_profile_model_id,
)
from custom_components.omron.omron_ble.session import OmronDeviceSession
from custom_components.omron.session_handoff import stash_probe_session

from bt import service_info

ADDRESS = "AA:BB:CC:DD:EE:33"
_BLUEZ_DETAILS = {"path": "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_33"}
_PROXY_DETAILS = {"source": "proxy-1"}
_QUIET_MSD = bytes([0x01, 0x00, 0x00, 0x00])


def test_reporter_device_maps_to_the_profile_under_test():
    """BP5465 는 자기 프로필이 없다 — 바꾸는 곳이 실제로 그 기기에 닿아야 한다."""
    assert resolve_profile_model_id("HEM-7382T1-AZAZ") == "HEM-7386T1"


def test_hem_7386t1_keeps_its_bond():
    config = CANONICAL_DEVICE_PROFILES["HEM-7386T1"]

    assert config.keep_notify_subscriptions is True, (
        "세션 종료 시 본드를 지우면 다음 연결이 암호화를 재개할 게 없다"
    )
    # 프록시에서 재개를 일으키는 것이 이것이다: 이미 본딩된 상대에게 보내는
    # ESPHome 의 pair 요청은 재페어링이 아니라 저장된 키로 암호화를 시작한다 —
    # 캡처 속 폰의 동작과 같다.


@pytest.mark.parametrize(
    "variant", ["HEM-7382T1-AZAZ", "HEM-7382T1", "HEM-7386T1-AJF3", "HEM-7381T1-AZ"]
)
def test_variants_inherit_it(variant):
    config = get_device_config(variant)
    assert config.keep_notify_subscriptions is True


def test_both_families_are_on_the_confirmed_bond_settings():
    """실험이 끝났으므로 계열 전체가 같은 설정을 쓴다.

    원인이 CCCD 로 확정되고(#91) PER_SESSION 이 이 계열에서 성립 불가임이
    확인된 뒤(#133 의 AuthenticationCanceled — 커프는 -P- 밖에서 새 페어링을
    거부한다) 두 프로필에만 걸려 있던 본드 설정을 WLD3.0/WLD4.0 전체로 옮겼다.
    """
    for name, config in CANONICAL_DEVICE_PROFILES.items():
        if config.connect_type not in (ConnectType.WLD3_0, ConnectType.WLD4_0):
            continue
        assert config.keep_notify_subscriptions is True, name


def test_driver_module_has_no_undefined_names():
    """소스 문자열만 보는 단언은 NameError 를 못 잡는다 — 실제로 겪었다."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "custom_components/"],
        capture_output=True,
        text=True,
    )
    if "No module named" in result.stderr:
        pytest.skip("ruff not installed")
    assert result.returncode == 0, result.stdout + result.stderr


def test_only_os_bonding_profiles_bond_at_connect():
    """커스텀 키 프로파일은 OS 본드를 만들지 않는다."""
    assert get_device_config("HEM-7386T1").pair_on_connect is True
    assert get_device_config("HEM-7155T").pair_on_connect is False


def test_one_poll_is_one_connection_attempt_on_the_profile_under_test():
    """본드가 걸려 있으면 재시도는 방어가 아니라 추가 위험이다."""
    assert get_device_config("HEM-7386T1").connect_settle_attempts == 1
    # 다른 프로필의 기존 동작은 그대로.
    assert get_device_config("HEM-7376T1").connect_settle_attempts == 3


def test_the_single_attempt_throttle_did_not_spread():
    """한 번만 시도하는 제한은 캡처가 있는 두 프로필에만 남는다."""
    for model in (
        "HEM-7376T1",
        "HEM-7377T1",
        "HEM-7155T-MW3",
        "HEM-7191T1",
        "HEM-7196T1",
    ):
        assert get_device_config(model).connect_settle_attempts == 3, model
    for model in ("HEM-7386T1", "HEM-7380T1"):
        assert get_device_config(model).connect_settle_attempts == 1, model


def test_the_two_profiles_under_test_stay_comparable():
    """7380T1 과 7386T1 프로필이 갈리면 두 기기의 보고를 비교할 수 없다."""
    a = get_device_config("HEM-7386T1")
    b = get_device_config("HEM-7380T1")
    for field in ("connect_settle_attempts", "keep_notify_subscriptions"):
        assert getattr(a, field) == getattr(b, field), field


# --- 연결 경로 (establish_connection_with_bond_settle) -------------------------


class _Link:
    """BleakClient 대역. ``alive`` 는 settle 이 끝난 뒤의 연결 상태를 정한다."""

    def __init__(self, *, alive: bool = True) -> None:
        self.is_connected = alive
        self.disconnects = 0

    async def disconnect(self) -> None:
        self.disconnects += 1


class _Connector:
    """``establish_connection`` 대역: 호출마다 미리 정한 결과를 낸다."""

    def __init__(self, *outcomes) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def __call__(self, client_class, ble_device, name, **kwargs):
        self.calls.append(kwargs)
        outcome = self._outcomes.pop(0) if self._outcomes else _Link()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    @property
    def paired_calls(self) -> int:
        return sum(1 for kwargs in self.calls if kwargs.get("pair"))


@pytest.fixture
def agent_log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """``_bluez_pairing_agent`` 가 열리고 닫힌 기록."""
    events: list[str] = []

    @asynccontextmanager
    async def _agent():
        events.append("enter")
        try:
            yield object()
        finally:
            events.append("exit")

    monkeypatch.setattr(connection, "_bluez_pairing_agent", _agent)
    monkeypatch.setattr(connection, "_POST_CONNECT_BOND_SETTLE_SEC", 0)
    monkeypatch.setattr(connection, "_SETTLE_POLL_STEP_SEC", 0.001)
    return events


def _connect(monkeypatch, connector, *, details, **kwargs):
    monkeypatch.setattr(connection, "establish_connection", connector)
    device = BLEDevice(address=ADDRESS, name="BLESmart", details=details)
    return connection.establish_connection_with_bond_settle(
        device, ADDRESS, model="HEM-7386T1", **kwargs
    )


async def test_connect_logs_the_path_that_owns_the_bond(monkeypatch, agent_log, caplog):
    """본드를 유지하면 "어느 라디오가 그걸 갖고 있나" 가 진단의 핵심이 된다.

    habluetooth 는 연결마다 점수로 프록시를 고른다. 본딩한 세션과 재연결하는
    세션이 다른 라디오에 안착하면, 유지한 본드는 있으나 마나다.
    """
    link = _Link()
    link._backend = SimpleNamespace(_device_path=_BLUEZ_DETAILS["path"])
    connector = _Connector(link)
    details = {"source": "scanner-A"}

    with caplog.at_level(logging.DEBUG, logger=connection.__name__):
        await _connect(monkeypatch, connector, details=details)

    established = [r.getMessage() for r in caplog.records if "established" in r.message]
    assert established, "연결 로그가 없다"
    line = established[0]
    assert "advertised by source=scanner-A" in line
    assert f"connected via {_BLUEZ_DETAILS['path']}" in line


async def test_connect_time_bonding_runs_on_a_local_adapter_inside_an_agent(
    monkeypatch, agent_log
):
    """연결 시점 본딩은 로컬 어댑터에서 agent 와 함께, 디스커버리 전에 나간다.

    agent 없이 Device1.Pair() 를 부르면 BlueZ 5.72+ 는 Just Works 확인을
    응답하지 않고 AuthenticationFailed 로 떨어진다.
    """
    link = _Link()
    connector = _Connector(link)
    info: dict = {}

    client = await _connect(
        monkeypatch,
        connector,
        details=_BLUEZ_DETAILS,
        pair_on_connect=True,
        link_info=info,
    )

    assert client is link
    assert connector.calls == [{"pair": True}]
    assert agent_log == ["enter", "exit"]
    assert client._omron_bonded_at_connect is True
    assert info["bonded_at_connect"] is True


async def test_connect_time_bonding_is_never_sent_over_a_proxy(monkeypatch, agent_log):
    """프록시에서 완료되지 않은 pair 요청 하나가 저장된 본드를 영구히 날린다 (#142)."""
    connector = _Connector(_Link())

    client = await _connect(
        monkeypatch, connector, details=_PROXY_DETAILS, pair_on_connect=True
    )

    assert connector.calls == [{}]
    assert agent_log == []
    assert client._omron_bonded_at_connect is False


async def test_a_session_that_does_not_ask_never_bonds_at_connect(
    monkeypatch, agent_log
):
    connector = _Connector(_Link())

    await _connect(monkeypatch, connector, details=_BLUEZ_DETAILS)

    assert connector.paired_calls == 0


@pytest.mark.parametrize(
    "refusal",
    [
        BleakError("AuthenticationFailed"),
        TimeoutError("connect"),
        asyncio.TimeoutError(),
    ],
    ids=["bleak-error", "timeout-error", "asyncio-timeout"],
)
async def test_a_refused_connect_time_bond_falls_back_to_a_plain_connect(
    monkeypatch, agent_log, refusal
):
    """bleak_retry_connector 는 소진 후 TimeoutError 를 던진다 — 그것도 물러나야 한다."""
    link = _Link()
    connector = _Connector(refusal, link)

    client = await _connect(
        monkeypatch, connector, details=_BLUEZ_DETAILS, pair_on_connect=True
    )

    assert client is link
    assert connector.calls == [{"pair": True}, {}]
    assert client._omron_bonded_at_connect is False


async def test_a_refusal_is_not_repeated_on_the_remaining_attempts(
    monkeypatch, agent_log
):
    """한 번 거절되면 남은 시도는 평범한 연결이어야 한다."""
    connector = _Connector(
        BleakError("AuthenticationFailed"),  # pair=True 거절
        _Link(alive=False),  # 폴백 연결이 settle 중 끊김
        _Link(),  # 2차 시도
    )

    await _connect(
        monkeypatch,
        connector,
        details=_BLUEZ_DETAILS,
        pair_on_connect=True,
        max_attempts=2,
    )

    assert connector.paired_calls == 1
    assert connector.calls == [{"pair": True}, {}, {}]


async def test_the_bonding_result_is_scoped_to_the_connection_it_describes(
    monkeypatch, agent_log
):
    """settle 중 끊기고 재시도하면, 앞 시도의 결과가 남아 있으면 안 된다.

    1차에서 pair=True 가 성공하고 settle 중 링크가 끊긴 뒤 2차가 폴백하면,
    이번 연결은 본딩하지 않았는데 pair() 가 건너뛴다 — 본드 없이 끝난다.
    """
    second = _Link()
    connector = _Connector(
        _Link(alive=False),  # 1차: 본딩 성공, settle 중 끊김
        BleakError("AuthenticationFailed"),  # 2차: pair=True 거절
        second,  # 2차 폴백
    )
    info: dict = {}

    client = await _connect(
        monkeypatch,
        connector,
        details=_BLUEZ_DETAILS,
        pair_on_connect=True,
        max_attempts=2,
        link_info=info,
    )

    assert client is second
    assert client._omron_bonded_at_connect is False
    assert info["bonded_at_connect"] is False


# --- 세션 -----------------------------------------------------------------------


def _session(model: str, *, pairing_session: bool, client=None) -> OmronDeviceSession:
    device = BLEDevice(address=ADDRESS, name="BLESmart", details=_BLUEZ_DETAILS)
    target = OmronDeviceSession(
        device, get_device_config(model), pairing_session=pairing_session
    )
    target._client = client
    return target


@pytest.mark.parametrize(
    ("pairing_session", "model", "expected"),
    [
        (True, "HEM-7386T1", True),
        # 재접속이 pair 요청을 보내면 안 된다 — 그게 프록시에서 본드를 날린 경로다.
        (False, "HEM-7386T1", False),
        # 커스텀 키 프로파일은 OS 본드를 만들지 않는다.
        (True, "HEM-7155T", False),
    ],
)
async def test_only_the_pairing_session_bonds_at_connect(
    monkeypatch, pairing_session, model, expected
):
    seen: dict = {}

    async def _establish(ble_device, name, **kwargs):
        seen.update(kwargs)
        return _Link()

    monkeypatch.setattr(
        session_module, "establish_connection_with_bond_settle", _establish
    )
    target = _session(model, pairing_session=pairing_session)

    await target.connect()

    assert seen["pair_on_connect"] is expected


@pytest.mark.parametrize(
    ("bonded_at_connect", "expect_bonding"), [(True, False), (False, True)]
)
async def test_a_second_bonding_is_skipped_only_when_the_connect_actually_bonded(
    monkeypatch, bonded_at_connect, expect_bonding
):
    """플래그가 아니라 결과로 판단해야 한다 — 폴백이 탄 뒤에도 본드는 만들어져야."""
    calls: list[str] = []

    async def _bond(self) -> None:
        calls.append("bond")

    monkeypatch.setattr(OmronDeviceSession, "_pair_os_bonding", _bond)
    client = _Link()
    client._omron_bonded_at_connect = bonded_at_connect
    # pair_on_connect 가 켜진 프로필이다: 프로파일 플래그로 판단하면 둘 다 건너뛴다.
    target = _session("HEM-7386T1", pairing_session=True, client=client)
    assert target.config.pair_on_connect is True

    await target.pair()

    assert (calls == ["bond"]) is expect_bonding


async def test_the_token_unlock_asks_for_what_the_profile_wants(monkeypatch):
    """unlock() 이 프로필 값을 그대로 넘겨야 한다 — 기본값이면 아무것도 안 바뀐다."""
    seen: list[bool] = []

    async def _token_unlock(target, *, keep_notify: bool = False) -> None:
        seen.append(keep_notify)

    monkeypatch.setattr(session_module, "_token_unlock", _token_unlock)
    base = get_device_config("HEM-7386T1")
    assert base.unlock_mode is session_module.UnlockMode.TOKEN_KEY
    for keep in (True, False):
        target = _session("HEM-7386T1", pairing_session=False, client=_Link())
        target._config = dataclasses.replace(base, keep_notify_subscriptions=keep)
        await target.unlock()
        assert seen[-1] is keep


def test_an_adopted_link_can_be_marked_as_the_pairing_session():
    """adopt() 는 __init__ 을 우회한다 — 플래그를 손으로 안 넣으면 폴처럼 붙는다."""
    config = get_device_config("HEM-7386T1")

    assert OmronDeviceSession.adopt(_Link(), config)._pairing_session is False
    assert (
        OmronDeviceSession.adopt(_Link(), config, pairing_session=True)._pairing_session
        is True
    )


# --- 모델 조회 링크 ------------------------------------------------------------------


class _ProbeSession:
    """``async_fetch_device_model_number`` 가 여는 세션의 대역."""

    instances: list[_ProbeSession] = []

    def __init__(self, ble_device, config) -> None:
        self.closed = 0
        _ProbeSession.instances.append(self)

    async def connect(self):
        return self

    async def read_model_number(self) -> str:
        return "HEM-7191T1"

    async def aclose(self) -> None:
        self.closed += 1


async def test_the_model_probe_can_hand_its_link_to_pairing(monkeypatch):
    """모델 조회 연결이 본드를 맺는 자리다 — 거기서 끊으면 키 배포가 잘린다.

    이슈 #91 의 프록시 트레이스: 커프의 Encryption Information(LTK) 은 도착했고
    EDIV/Rand 를 담은 Master Identification 은 오지 않은 채, SMP 가
    ``BOND_PENDING`` 인 상태에서 우리가 링크를 끊었다. LE legacy 는 나중에
    암호화를 재개하려면 셋이 다 필요하므로, 저장된 본드는 못 쓰는 물건이 되고
    첫 폴이 ``SMP_ENC_FAIL``(97) 로 떨어진다.
    """
    monkeypatch.setattr(pairing, "OmronDeviceSession", _ProbeSession)
    _ProbeSession.instances = []
    device = BLEDevice(address=ADDRESS, name="BLESmart", details={})

    model, kept = await pairing.async_fetch_device_model_number(
        device, keep_session_open=True
    )
    assert model == "HEM-7191T1"
    assert kept is _ProbeSession.instances[0]
    assert kept.closed == 0, "링크를 넘긴다면서 닫았다"

    model, kept = await pairing.async_fetch_device_model_number(device)
    assert kept is None
    assert _ProbeSession.instances[1].closed == 1


# --- 설정 플로우 -----------------------------------------------------------------------


class _ProbeLink:
    """모델 조회가 열어 둔 링크: 실제 세션으로 감싼 연결된 클라이언트."""

    def __init__(self) -> None:
        self.client = _Link()
        self.session = OmronDeviceSession.adopt(
            self.client, get_device_config("HEM-7600T")
        )
        self.closed = 0

        async def _aclose() -> None:
            self.closed += 1

        self.session.aclose = _aclose  # type: ignore[method-assign]


async def _pair_through_the_flow(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    *,
    details: dict,
    probe: _ProbeLink,
) -> list[OmronDeviceSession]:
    """실제 플로우 매니저로 페어링 단계까지 가서 ``async_pair_and_sync_device`` 가 받은 세션을 돌려준다."""
    paired: list[OmronDeviceSession] = []

    def _device(hass, address, *args, **kwargs):
        return BLEDevice(address=address, name="BLESmart_0000123", details=details)

    async def _fetch(ble_device, *, keep_session_open: bool = False):
        assert keep_session_open, "페어링이 이어받을 수 있게 링크를 열어 둬야 한다"
        return "HEM-7600T", probe.session

    async def _pair(session, model, **kwargs) -> None:
        paired.append(session)

    async def _connect(self):
        if self._client is None:
            self._client = _Link()
        return self

    monkeypatch.setattr(config_flow, "async_ble_device_from_address", _device)
    monkeypatch.setattr(config_flow, "async_fetch_device_model_number", _fetch)
    monkeypatch.setattr(config_flow, "async_pair_and_sync_device", _pair)
    monkeypatch.setattr(OmronDeviceSession, "connect", _connect)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=service_info(
            ADDRESS,
            name="BLESmart_0000123",
            manufacturer_data={OMRON_MANUFACTURER_ID: _QUIET_MSD},
        ),
    )
    if result["step_id"] == "bluetooth_confirm":
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )
    assert result["step_id"].startswith("select_model")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_MODEL: model}
    )
    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] in ("pairing", "pairing_os")
    with (
        patch("custom_components.omron.async_setup_entry", return_value=True),
        patch("custom_components.omron.async_unload_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.CREATE_ENTRY, result
        await hass.config_entries.async_remove(result["result"].entry_id)
    return paired


async def test_the_pairing_flow_continues_on_the_probe_link_and_marks_it_a_pairing_session(
    hass: HomeAssistant, enable_bluetooth: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """프로필이 연결 시점에 본딩하지 않으면 조회 링크를 이어받아 페어링한다.

    플래그를 안 넘기면 설정 플로우도 조용히 폴처럼 붙는다.
    """
    probe = _ProbeLink()

    paired = await _pair_through_the_flow(
        hass, monkeypatch, "HEM-7600T", details=_BLUEZ_DETAILS, probe=probe
    )

    [session] = paired
    assert session.client is probe.client, "조회 링크를 이어받지 않았다"
    assert session._pairing_session is True
    assert probe.closed == 0


async def test_the_probe_link_is_not_adopted_when_the_profile_bonds_at_connect(
    hass: HomeAssistant, enable_bluetooth: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """프로브 링크를 이어받으면 connect-time 본딩이 조용히 건너뛰어진다 (#24, #92).

    프로브는 모델을 모르는 상태에서 링크를 열었으니 본딩할 수 없었고, 이미
    디스커버리를 끝낸 링크는 "디스커버리보다 먼저" 본딩할 수 없다. 그대로
    adopt 하면 connect() 가 즉시 반환해 pair=True 가 나가지 않는다.
    """
    probe = _ProbeLink()

    paired = await _pair_through_the_flow(
        hass, monkeypatch, "HEM-7191T1", details=_BLUEZ_DETAILS, probe=probe
    )

    [session] = paired
    assert probe.closed == 1, "프로브 링크를 버리지 않았다"
    assert session.client is not probe.client
    assert session._pairing_session is True, "새 연결도 페어링 세션이어야 한다"


async def test_the_probe_link_is_kept_over_a_proxy_even_when_the_profile_bonds_at_connect(
    hass: HomeAssistant, enable_bluetooth: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """프록시에서는 pair=True 가 나가지 않는다 — 프로브를 버리면 순수한 손해다.

    설정 플로우와 연결 경로가 같은 술어(``is_local_adapter``)를 써야 한다.
    """
    probe = _ProbeLink()

    paired = await _pair_through_the_flow(
        hass, monkeypatch, "HEM-7191T1", details=_PROXY_DETAILS, probe=probe
    )

    [session] = paired
    assert session.client is probe.client
    assert probe.closed == 0


async def test_a_parked_probe_link_is_closed_when_the_entry_unloads(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """아무도 이어받지 않은 링크가 언로드 뒤까지 살아 있으면 슬롯을 잡아먹는다."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="HEM-7155T",
        data={CONF_DEVICE_MODEL: "HEM-7155T"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    probe = _ProbeLink()
    stash_probe_session(hass, entry.runtime_data.address, probe.session)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert probe.closed == 1


async def test_retry_pairing_is_a_pairing_session(monkeypatch):
    """async_retry_pairing 도 본드를 만드는 자리다."""
    opened: list[bool] = []

    async def _connect(self):
        opened.append(self._pairing_session)
        raise ConnectionError("stop here")

    monkeypatch.setattr(OmronDeviceSession, "connect", _connect)
    data = parser.OmronBluetoothDeviceData(device_model="HEM-7386T1")
    device = BLEDevice(address=ADDRESS, name="BLESmart", details={})

    with pytest.raises(ConnectionError):
        await data.async_retry_pairing(device)

    assert opened == [True]
