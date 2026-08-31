"""세션 종료 직전 미러 쓰기 회귀 테스트 (이슈 #91).

공식 앱의 btsnoop 은 BP5465(이슈 #91)와 HEM-7155T(이슈 #67) 양쪽에서, **모든**
세션 — 최초 페어링 세션과 본드를 재개한 폴링 세션을 가리지 않고 — 을 종료
명령 ``080f`` 직전의 ``01c0`` EEPROM 쓰기 두 번으로 끝낸다. 각각 기기 소유
영역을 모델별로 고정된 오프셋의 미러로 복사하면서 몇 바이트만 바꾼다.

BP5465 에서 실제로 잡힌 프레임은 이렇다::

    read  0x0010 (28B) -> write 0x0058   마지막 바이트 0x01 -> 0x80
    read  0x0040 (16B) -> write 0x0088   byte[4] 0 -> 1, 타임스탬프 = 현재

우리는 둘 다 한 번도 쓴 적이 없다. 저장소에서 ``write_memory_block`` 을 부르는
곳은 EEPROM 시각 동기화 하나뿐이고, 그마저 60초 이상 드리프트가 있을 때만
동작해서 캡처된 9번의 실행이 전부 건너뛰었다.

그러므로 이건 **검증된 수정이 아니라 한 번도 시험하지 않은 변수**다. 판정 기준은
재페어링 없이 두 번째·세 번째 폴이 성공하는지이며, 첫 세션은 지금도 항상
성공하므로 판정에 쓸 수 없다.
"""
import datetime as dt

import pytest

from custom_components.omron.omron_ble import omron_driver
from custom_components.omron.omron_ble.devices import (
    TimeSyncLayout,
    get_device_config,
)
from custom_components.omron.omron_ble.omron_driver import OmronDeviceSession

# 이슈 #91 Task 4 에서 리포터가 올린 BP5465 의 실제 소스 페이로드.
_INDEX_SRC = bytes.fromhex("004002000080020075000000020000000280020280408a020280400100010100200000002200ffff000000000000fe00")
_STATUS_SRC = bytes.fromhex("c6540000000000001a081b151f0c9700")


class _FakeSession:
    """미러 쓰기만 떼어내 구동하기 위한 최소 세션."""

    def __init__(self, config):
        self._config = config
        self._memory_session_active = True
        self.address = "C1:8D:32:97:D5:BB"
        self.reads: list[tuple[int, int]] = []
        self.writes: list[tuple[int, bytes]] = []
        self.commands: list[str] = []
        self.unsubscribed = False
        self._last_reply_packet_type = bytearray.fromhex("8f00")
        self._last_reply_payload = b"\x00"

    async def read_memory_block(self, address: int, blocksize: int) -> bytes:
        self.reads.append((address, blocksize))
        src = _INDEX_SRC if address == 0x0010 else _STATUS_SRC
        return src[:blocksize]

    async def write_memory_block(self, address: int, data: bytearray) -> None:
        self.writes.append((address, bytes(data)))

    async def _write_command_and_wait_reply(self, cmd: bytearray) -> None:
        self.commands.append(bytes(cmd).hex())

    async def _unsubscribe_notify_channels(self) -> None:
        self.unsubscribed = True

    _write_session_ack_mirrors = OmronDeviceSession._write_session_ack_mirrors
    close_memory_session = OmronDeviceSession.close_memory_session


@pytest.fixture
def session():
    return _FakeSession(get_device_config("HEM-7386T1"))


def test_the_profile_under_test_asks_for_the_mirrors():
    """BP5465 만 켠다 — 나머지는 폰 캡처가 없어 오프셋이 추정일 뿐이다."""
    assert get_device_config("HEM-7386T1").session_ack_mirror_writes is True
    assert get_device_config("HEM-7380T1").session_ack_mirror_writes is False
    assert get_device_config("HEM-7376T1").session_ack_mirror_writes is False
    assert get_device_config("HEM-7142T2").session_ack_mirror_writes is False


@pytest.mark.asyncio
async def test_both_mirrors_go_out_before_the_close_command(session):
    """앱의 순서 그대로여야 한다: 미러 두 번 -> 080f. 뒤집히면 의미가 없다."""
    await session.close_memory_session()

    assert [addr for addr, _ in session.writes] == [0x0058, 0x0088]
    assert session.commands == ["080f000000000007"]
    assert session.unsubscribed is True


@pytest.mark.asyncio
async def test_the_index_mirror_matches_the_captured_frame(session):
    """0x0010 을 28바이트 복사하고 마지막 바이트만 0x80 으로."""
    await session.close_memory_session()

    assert (0x0010, 0x1C) in session.reads
    addr, payload = session.writes[0]
    assert addr == 0x0058
    assert len(payload) == 0x1C
    assert payload[:-1] == _INDEX_SRC[: 0x1C - 1]
    assert payload[-1] == 0x80
    # 캡처된 프레임과 바이트 단위로 같아야 한다.
    assert payload.hex() == "004002000080020075000000020000000280020280408a0202804080"


@pytest.mark.asyncio
async def test_the_status_mirror_sets_the_flag_and_recomputes_the_checksum(session):
    """byte[4]=1, 타임스탬프는 현재, 체크섬은 sum(bytes[0:14]) & 0xFF."""
    await session.close_memory_session()

    addr, payload = session.writes[1]
    assert addr == 0x0088
    assert len(payload) == 0x10
    # 카운터 머리는 기기가 쓴 값을 그대로 들고 간다.
    assert payload[0:4] == _STATUS_SRC[0:4]
    assert payload[4] == 0x01, "0 은 기기가 쓴 것, 1 이 클라이언트 확인이다"
    assert payload[5:8] == b"\x00\x00\x00"
    # 타임스탬프는 지금이어야 한다 — 소스의 것을 그대로 옮기면 확인이 아니다.
    year, month, day, hour, minute, second = payload[8:14]
    now = dt.datetime.now()
    assert (year, month, day) == (now.year - 2000, now.month, now.day)
    assert 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59
    # 두 캡처의 6개 샘플 전부에서 성립하는 규칙.
    assert payload[14] == sum(payload[0:14]) & 0xFF
    assert payload[15] == 0x00


@pytest.mark.asyncio
async def test_the_status_mirror_reads_the_same_region_the_clock_sync_does(session):
    """0x0010+0x30 = 0x0040. 앱이 읽는 주소이자 우리가 이미 시각으로 읽는 주소다."""
    await session.close_memory_session()

    assert (0x0040, 0x10) in session.reads
    config = get_device_config("HEM-7386T1")
    section_start, section_end = config.settings_time_sync_bytes
    assert config.settings_read_address + section_start == 0x0040
    assert config.settings_write_address + section_start == 0x0088
    assert config.resolved_time_sync_layout() == TimeSyncLayout.MODERN_OFFSET8


@pytest.mark.asyncio
async def test_a_failed_mirror_write_still_closes_the_session(session, caplog):
    """미러 쓰기가 종료 명령을 막으면 지금보다 나빠진다 — best effort 여야 한다."""

    async def _boom(address, data):
        raise OSError("device went away")

    session.write_memory_block = _boom

    await session.close_memory_session()

    assert session.commands == ["080f000000000007"]
    assert session.unsubscribed is True
    assert "Session ack mirror writes failed" in caplog.text


@pytest.mark.asyncio
async def test_profiles_that_do_not_ask_write_nothing():
    """켜지 않은 프로필은 프레임 하나도 늘어나면 안 된다."""
    off = _FakeSession(get_device_config("HEM-7376T1"))

    await off.close_memory_session()

    assert off.writes == []
    assert off.reads == []
    assert off.commands == ["080f000000000007"]


def test_a_short_read_is_not_written_back(session):
    """길이가 모자란 응답을 그대로 미러에 쓰면 장부가 깨진다."""
    import inspect

    source = inspect.getsource(omron_driver.OmronDeviceSession._write_session_ack_mirrors)
    assert "Short index read" in source
    assert "Short status read" in source


def test_the_profile_under_test_keeps_its_notify_subscriptions():
    """앱은 CCCD 를 켜기만 하고 끄지 않는다 — 두 캡처 4개 세션에서 0x0000 이 0건.

    앱이 쓰는 CCCD 값 전부: 0x000B=0x0002(페어링 세션만), 0x001C=0x0100,
    0x0021=0x0100. 비활성화는 한 번도 없고 그냥 끊는다.

    우리는 세션당 6번 쓴다. 토큰 언락이 둘을 켰다가 끄고, 메모리 세션이 RX 를
    다시 켜고, 세션 종료가 **080f 다음에** 또 끈다 — 링크가 내려가기 직전의
    마지막 GATT 동작이다.

    규격(Vol 3 Part G, 3.3.3.3)은 페리페럴이 CCCD 설정을 본드된 클라이언트별로
    보존하게 한다. 작은 스택은 이걸 본드 레코드에 같이 담는 경우가 흔하다.
    """
    assert get_device_config("HEM-7386T1").keep_notify_subscriptions is True
    assert get_device_config("HEM-7380T1").keep_notify_subscriptions is False
    assert get_device_config("HEM-7376T1").keep_notify_subscriptions is False
    assert get_device_config("HEM-7142T2").keep_notify_subscriptions is False


class _FakeClient:
    def __init__(self) -> None:
        self.stopped: list[str] = []

    async def stop_notify(self, uuid: str) -> None:
        self.stopped.append(uuid)


def _release_target(model: str) -> _FakeSession:
    target = _FakeSession(get_device_config(model))
    target._client = _FakeClient()
    target._notify_subscribed = True
    target._debug_ble_link = lambda *_a, **_k: None
    target._unsubscribe_notify_channels = (
        OmronDeviceSession._unsubscribe_notify_channels.__get__(target)
    )
    return target


@pytest.mark.asyncio
async def test_the_normal_close_leaves_the_cccd_enabled():
    """정상 종료에서 stop_notify 가 한 건도 나가면 안 된다 — 앱이 남기는 상태다."""
    target = _release_target("HEM-7386T1")

    await OmronDeviceSession.close_memory_session(target)

    assert target._client.stopped == []
    assert target.commands == ["080f000000000007"]


@pytest.mark.asyncio
async def test_other_profiles_still_disable_it_on_close():
    """켜지 않은 프로필의 종료 동작은 그대로여야 한다."""
    target = _release_target("HEM-7376T1")

    await OmronDeviceSession.close_memory_session(target)

    assert target._client.stopped == list(
        get_device_config("HEM-7376T1").rx_channel_uuids
    )


@pytest.mark.asyncio
async def test_force_releases_it_even_on_the_profile_under_test():
    """실패 경로는 강제로 풀 수 있어야 한다 — 아니면 재시도가 막힌다."""
    target = _release_target("HEM-7386T1")

    await target._unsubscribe_notify_channels(force=True)

    assert target._client.stopped == list(
        get_device_config("HEM-7386T1").rx_channel_uuids
    )


@pytest.mark.asyncio
async def test_failure_paths_can_still_release_the_subscription():
    """실패한 세션까지 구독을 붙들고 있으면 재시도가 막힌다 — force 로 풀린다."""
    import inspect

    for name in ("reset_session_state", "open_memory_session"):
        source = inspect.getsource(getattr(OmronDeviceSession, name))
        assert "_unsubscribe_notify_channels(force=True)" in source, name

    source = inspect.getsource(OmronDeviceSession._unsubscribe_notify_channels)
    assert "if self._config.keep_notify_subscriptions and not force:" in source


def test_the_token_unlock_primes_the_channel_it_will_keep():
    """구독을 유지하면 프라임 콜백이 진짜 핸들러여야 한다.

    죽은 콜백으로 켜 두면 메모리 세션이 이미 켜진 CCCD 에 start_notify 를 다시
    걸고, 백엔드는 그걸 거부하며 복구 경로가 CCCD 를 0x0000 으로 되돌린다 —
    없애려던 바로 그 churn 이다.
    """
    import inspect

    source = inspect.getsource(OmronDeviceSession._token_unlock)
    assert "self._on_notify_channel_data if keep_notify else" in source
    assert "self._notify_subscribed = True" in source

    unlock = inspect.getsource(OmronDeviceSession.unlock)
    assert "keep_notify=self._config.keep_notify_subscriptions" in unlock
