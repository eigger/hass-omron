"""알림 CCCD 를 세션 내내 켜 둔 채로 두는 것에 대한 회귀 테스트 (이슈 #91).

이슈 #91 에서 확정된 원인이다. WLD3.0 커프는 페어링 후 모든 재접속을
``PIN or Key Missing (0x06)`` 으로 거절해 왔는데, 세션 종료 뒤 CCCD 를
비활성화하지 않게 하자 재접속이 살아났다.

리포터의 통제된 A/B (2.7.8-beta.15, BP5465, 로컬 BlueZ, 프록시·폰 전부 차단):
한 번 페어링한 뒤 이어진 네 번의 연결이 전부 저장된 본드를 재개했다. SMP
페어링은 최초 1회뿐, AES-CCM 암호화 성공 5회, CCCD 비활성화(0x0000) 0회,
``0x06`` 0회. 같은 빌드에서 세션 종료 미러 쓰기는 꺼져 있었으므로, 재접속을
살린 것은 CCCD 쪽이다.

근거는 공식 앱 캡처다. 이슈 #91(BP5465)과 #67(HEM-7155T) 두 캡처의 모든 ATT
쓰기를 세어 보면 앱이 쓰는 CCCD 값은 0x000B=0x0002(페어링 세션만),
0x001C=0x0100, 0x0021=0x0100 뿐이고 **비활성화는 한 건도 없다.** 그냥 끊는다.

우리는 세션당 여섯 번 썼다. 토큰 언락이 둘을 켰다가 끄고, 메모리 세션이 RX 를
다시 켜고, 세션 종료가 ``080f`` **다음에** 또 껐다 — 링크가 내려가기 직전의
마지막 GATT 동작이다. 규격(Vol 3 Part G, 3.3.3.3)은 페리페럴이 CCCD 설정을
본드된 클라이언트별로 보존하게 하고, 작은 스택은 그것을 본드 레코드에 함께
담는 경우가 흔하다.
"""
import asyncio
from types import SimpleNamespace

from custom_components.omron.omron_ble.devices import get_device_config
from custom_components.omron.omron_ble.const import UNLOCK_CHARACTERISTIC_UUID
from custom_components.omron.omron_ble.omron_driver import OmronDeviceSession


class _FakeSession:
    """세션 종료 경로만 떼어내 구동하기 위한 최소 세션."""

    def __init__(self, config):
        self._config = config
        self._memory_session_active = True
        self.address = "C1:8D:32:97:D5:BB"
        self.commands: list[str] = []
        self._last_reply_packet_type = bytearray.fromhex("8f00")
        self._last_reply_payload = bytes(1)

    async def _write_command_and_wait_reply(self, cmd: bytearray) -> None:
        self.commands.append(bytes(cmd).hex())

    close_memory_session = OmronDeviceSession.close_memory_session


def test_the_wld3_and_wld4_families_keep_their_notify_subscriptions():
    """앱은 CCCD 를 켜기만 하고 끄지 않는다 — 두 캡처 4개 세션에서 0x0000 이 0건.

    앱이 쓰는 CCCD 값 전부: 0x000B=0x0002(페어링 세션만), 0x001C=0x0100,
    0x0021=0x0100. 비활성화는 한 번도 없고 그냥 끊는다.

    우리는 세션당 6번 쓴다. 토큰 언락이 둘을 켰다가 끄고, 메모리 세션이 RX 를
    다시 켜고, 세션 종료가 **080f 다음에** 또 끈다 — 링크가 내려가기 직전의
    마지막 GATT 동작이다.

    규격(Vol 3 Part G, 3.3.3.3)은 페리페럴이 CCCD 설정을 본드된 클라이언트별로
    보존하게 한다. 작은 스택은 이걸 본드 레코드에 같이 담는 경우가 흔하다.
    """
    # 계열 전체에 적용한다: 근거가 두 기기(#91 BP5465, #67 HEM-7155T)에 걸쳐
    # 있고, 기기에 쓰는 것을 늘리는 게 아니라 없애는 변경이기 때문이다.
    for model in (
        "HEM-7386T1",
        "HEM-7380T1",
        "HEM-7376T1",
        "HEM-7377T1",
        "HEM-7155T-MW3",
        "HEM-7188T1",
        "HEM-7191T1",
        "HEM-7196T1",
    ):
        assert get_device_config(model).keep_notify_subscriptions is True, model
    # 계열 밖은 건드리지 않는다.
    assert get_device_config("HEM-7142T2").keep_notify_subscriptions is False


class _FakeClient:
    def __init__(self) -> None:
        self.stopped: list[str] = []
        self.started: list[str] = []

    async def stop_notify(self, uuid: str) -> None:
        self.stopped.append(uuid)

    async def start_notify(self, uuid: str, _cb) -> None:
        self.started.append(uuid)


def _release_target(model: str) -> _FakeSession:
    target = _FakeSession(get_device_config(model))
    target._client = _FakeClient()
    target._notify_subscribed = True
    target._debug_ble_link = lambda *_a, **_k: None
    target._unsubscribe_notify_channels = (
        OmronDeviceSession._unsubscribe_notify_channels.__get__(target)
    )
    return target


def test_the_normal_close_leaves_the_cccd_enabled():
    """정상 종료에서 stop_notify 가 한 건도 나가면 안 된다 — 앱이 남기는 상태다."""
    target = _release_target("HEM-7386T1")

    asyncio.run(OmronDeviceSession.close_memory_session(target))

    assert target._client.stopped == []
    assert target.commands == ["080f000000000007"]


def test_profiles_outside_the_two_families_still_disable_it_on_close():
    """계열 밖 프로필의 종료 동작은 그대로여야 한다.

    HEM-7376T1 을 쓰던 테스트였는데, 계열 전체로 확대하면서 그 기기가 안쪽으로
    들어와 자기모순이 됐다. 바깥에 있는 프로필로 바꾼다.
    """
    target = _release_target("HEM-7142T2")

    asyncio.run(OmronDeviceSession.close_memory_session(target))

    assert target._client.stopped == list(
        get_device_config("HEM-7142T2").rx_channel_uuids
    )


def test_force_releases_it_even_on_the_profile_under_test():
    """실패 경로는 강제로 풀 수 있어야 한다 — 아니면 재시도가 막힌다."""
    target = _release_target("HEM-7386T1")

    asyncio.run(target._unsubscribe_notify_channels(force=True))

    assert target._client.stopped == list(
        get_device_config("HEM-7386T1").rx_channel_uuids
    )


def test_reset_releases_the_subscription_on_the_profile_under_test():
    """실패한 세션까지 구독을 붙들고 있으면 재시도가 막힌다 — reset 은 풀어야 한다."""
    target = _release_target("HEM-7386T1")
    target._secure_session = None
    target._channel_fragments = [None] * 4
    target._expected_reply_packet_type = None
    target._reply_ready = asyncio.Event()
    target._unlocked = True

    asyncio.run(OmronDeviceSession.reset_session_state(target))

    # The unlock characteristic is released too. It is not an RX channel, so the
    # loop over rx_channel_uuids never covered it -- and on BlueZ it is the one
    # left holding a notify session from the previous connection (#92).
    assert target._client.stopped == list(
        get_device_config("HEM-7386T1").rx_channel_uuids
    ) + [UNLOCK_CHARACTERISTIC_UUID]
    assert target._unlocked is False


def test_a_kept_subscription_is_not_subscribed_again():
    """이미 켜진 CCCD 에 start_notify 를 다시 걸면 백엔드가 거부하고, 복구 경로가
    CCCD 를 0x0000 으로 되돌린다 — 없애려던 바로 그 churn 이다.

    토큰 언락이 진짜 핸들러로 프라임하고 _notify_subscribed 를 세우므로, 메모리
    세션의 구독은 아무 프레임도 내보내지 않아야 한다.
    """
    target = _release_target("HEM-7386T1")
    target._client.started = []
    target._notify_subscribed = True

    asyncio.run(OmronDeviceSession._subscribe_notify_channels(target))

    assert target._client.started == []


def test_the_token_unlock_asks_for_what_the_profile_wants():
    """unlock() 이 프로필 값을 그대로 넘겨야 한다 — 기본값이면 아무것도 안 바뀐다."""
    import inspect

    sig = inspect.signature(OmronDeviceSession._token_unlock)
    assert sig.parameters["keep_notify"].default is False
    assert get_device_config("HEM-7386T1").keep_notify_subscriptions is True


class _BlueZError(Exception):
    """conftest 가 bleak 를 MagicMock 으로 치환하므로 실제 예외 클래스가 필요하다."""


class _NotifyHeldClient:
    """첫 start_notify 를 BlueZ 가 거부하고, stop_notify 뒤에는 받아주는 클라이언트."""

    def __init__(self, uuid: str) -> None:
        self._uuid = uuid
        self.started: list[tuple[str, object]] = []
        self.stopped: list[str] = []
        self._released = False

    async def start_notify(self, uuid: str, callback) -> None:
        self.started.append((uuid, callback))
        if uuid == self._uuid and not self._released:
            raise _BlueZError(
                "[org.bluez.Error.Failed] Failed to register notify session"
            )

    async def stop_notify(self, uuid: str) -> None:
        self.stopped.append(uuid)
        if uuid == self._uuid:
            self._released = True


def test_a_notify_session_bluez_still_holds_is_released_and_retried(monkeypatch):
    """이전 연결이 남긴 세션 때문에 재구독이 거부되면, 풀고 다시 걸어야 한다 (#92).

    keep_notify 프로파일은 언락 캐릭터리스틱을 해제하지 않으므로 BlueZ 가
    ``Failed to register notify session`` 을 돌려준다. 그 문구가 복구 대상에서
    빠져 있으면 첫 시도가 그대로 죽고, 재시도는 이미 끊긴 링크에 쓰기를 시도해
    ``Failed to initiate write`` 로 이어진다.
    """
    from custom_components.omron.omron_ble import omron_driver

    monkeypatch.setattr(omron_driver, "BleakError", _BlueZError)
    client = _NotifyHeldClient(UNLOCK_CHARACTERISTIC_UUID)
    target = SimpleNamespace(
        _client=client,
        _config=get_device_config("HEM-7188T1"),
        _on_notify_channel_data=lambda *_: None,
    )
    sentinel = object()

    asyncio.run(
        OmronDeviceSession._start_notify_with_recovery(
            target, UNLOCK_CHARACTERISTIC_UUID, sentinel
        )
    )

    assert client.stopped == [UNLOCK_CHARACTERISTIC_UUID], "구독을 풀지 않았다"
    assert [u for u, _ in client.started] == [UNLOCK_CHARACTERISTIC_UUID] * 2, (
        "풀고 나서 다시 걸지 않았다"
    )
    # 호출자가 준 콜백이 그대로 전달돼야 한다 — RX 핸들러로 바뀌면 언락 응답을
    # 받을 곳이 없어진다.
    assert all(cb is sentinel for _, cb in client.started)


def test_the_unlock_subscribe_stays_on_the_recovery_path():
    """언락 구독이 start_notify 를 직접 부르면 위 복구가 걸리지 않는다 (#92).

    소스 문자열이 아니라 AST 로 본다 — 줄바꿈을 바꿔도 깨지지 않아야 한다.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(OmronDeviceSession._token_unlock))
    )
    direct: list[str] = []
    recovered: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        first = ast.unparse(node.args[0]) if node.args else ""
        if node.func.attr == "start_notify":
            direct.append(first)
        elif node.func.attr == "_start_notify_with_recovery":
            recovered.append(first)

    assert not direct, f"복구를 우회하는 직접 호출이 남아 있다: {direct}"
    assert "UNLOCK_CHARACTERISTIC_UUID" in recovered, (
        "언락 구독이 복구 경로를 타지 않는다"
    )
    assert any("rx_channel_uuids" in arg for arg in recovered), (
        "RX 프라임이 복구 경로를 타지 않는다"
    )
