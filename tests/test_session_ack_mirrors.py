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

from dataclasses import replace

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
        self.released: list[bool] = []
        self.writes: list[tuple[int, bytes]] = []
        self.commands: list[str] = []
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

    async def _unsubscribe_notify_channels(self, *, force: bool = False) -> None:
        self.released.append(force)

    _write_session_ack_mirrors = OmronDeviceSession._write_session_ack_mirrors
    close_memory_session = OmronDeviceSession.close_memory_session


@pytest.fixture
def session():
    # This branch turns the mirrors off in the catalog to A/B them against the
    # CCCD change, so force the flag back on here: these tests cover the mirror
    # code path itself, not whether any shipped profile asks for it.
    return _FakeSession(
        replace(get_device_config("HEM-7386T1"), session_ack_mirror_writes=True)
    )


def test_no_profile_asks_for_the_mirrors_on_this_branch():
    """A/B 가지: 미러는 전부 끄고 CCCD 유지만 남긴다."""
    assert get_device_config("HEM-7386T1").session_ack_mirror_writes is False
    assert get_device_config("HEM-7380T1").session_ack_mirror_writes is False
    assert get_device_config("HEM-7376T1").session_ack_mirror_writes is False
    assert get_device_config("HEM-7142T2").session_ack_mirror_writes is False
    # 이 가지가 격리하려는 변경은 그대로 켜져 있어야 한다.
    assert get_device_config("HEM-7386T1").keep_notify_subscriptions is True


@pytest.mark.asyncio
async def test_both_mirrors_go_out_before_the_close_command(session):
    """앱의 순서 그대로여야 한다: 미러 두 번 -> 080f. 뒤집히면 의미가 없다."""
    await session.close_memory_session()

    assert [addr for addr, _ in session.writes] == [0x0058, 0x0088]
    assert session.commands == ["080f000000000007"]


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
    assert session.writes == []
    assert "Session ack mirror writes failed" in caplog.text


@pytest.mark.asyncio
async def test_profiles_that_do_not_ask_write_nothing():
    """켜지 않은 프로필은 프레임 하나도 늘어나면 안 된다."""
    off = _FakeSession(get_device_config("HEM-7376T1"))

    await off.close_memory_session()

    assert off.writes == []
    assert off.reads == []
    assert off.commands == ["080f000000000007"]


@pytest.mark.asyncio
async def test_a_short_read_is_not_written_back(session, caplog):
    """길이가 모자란 응답을 그대로 미러에 쓰면 장부가 깨진다 — 쓰지 말아야 한다."""

    async def _short(address, blocksize):
        session.reads.append((address, blocksize))
        return bytes(blocksize - 1)

    session.read_memory_block = _short

    await session.close_memory_session()

    assert session.writes == []
    assert "Session ack mirror writes failed" in caplog.text
    assert session.commands == ["080f000000000007"]


@pytest.mark.asyncio
async def test_an_unexpected_index_source_byte_is_called_out(session, caplog):
    """캡처와 다른 상태에서 쓰고 있으면 로그로라도 남아야 한다 — 샘플이 하나뿐이다."""
    import logging

    caplog.set_level(logging.WARNING)
    source = bytearray(_INDEX_SRC)
    source[0x1C - 1] = 0x02

    async def _read(address, blocksize):
        session.reads.append((address, blocksize))
        return bytes(source)[:blocksize] if address == 0x0010 else _STATUS_SRC[:blocksize]

    session.read_memory_block = _read

    await session.close_memory_session()

    assert "not the 0x01 the capture showed" in caplog.text
    # 그래도 캡처가 보여준 값을 쓴다 — 추측을 바꾸는 것은 더 위험하다.
    assert session.writes[0][1][-1] == 0x80


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
async def test_reset_releases_the_subscription_on_the_profile_under_test():
    """실패한 세션까지 구독을 붙들고 있으면 재시도가 막힌다 — reset 은 풀어야 한다."""
    import asyncio

    target = _release_target("HEM-7386T1")
    target._secure_session = None
    target._channel_fragments = [None] * 4
    target._expected_reply_packet_type = None
    target._reply_ready = asyncio.Event()
    target._unlocked = True

    await OmronDeviceSession.reset_session_state(target)

    assert target._client.stopped == list(
        get_device_config("HEM-7386T1").rx_channel_uuids
    )
    assert target._unlocked is False


@pytest.mark.asyncio
async def test_a_kept_subscription_is_not_subscribed_again():
    """이미 켜진 CCCD 에 start_notify 를 다시 걸면 백엔드가 거부하고, 복구 경로가
    CCCD 를 0x0000 으로 되돌린다 — 없애려던 바로 그 churn 이다.

    토큰 언락이 진짜 핸들러로 프라임하고 _notify_subscribed 를 세우므로, 메모리
    세션의 구독은 아무 프레임도 내보내지 않아야 한다.
    """
    target = _release_target("HEM-7386T1")
    target._client.started = []
    target._notify_subscribed = True

    await OmronDeviceSession._subscribe_notify_channels(target)

    assert target._client.started == []


def test_the_token_unlock_asks_for_what_the_profile_wants():
    """unlock() 이 프로필 값을 그대로 넘겨야 한다 — 기본값이면 아무것도 안 바뀐다."""
    import inspect

    sig = inspect.signature(OmronDeviceSession._token_unlock)
    assert sig.parameters["keep_notify"].default is False
    assert get_device_config("HEM-7386T1").keep_notify_subscriptions is True
