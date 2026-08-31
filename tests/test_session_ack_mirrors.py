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
