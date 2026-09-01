"""응답 짝짓기 회귀 테스트 (이슈 #91, beta.15 실행에서 드러난 결함).

벤더 메모리 프로토콜에는 요청-응답 식별자가 없다. 명령을 쓰고, 알림이 오면
그것을 답으로 쓴다. 그래서 어떤 프레임을 받아들일지 고르는 것이 전부다.

예전 필터는 **패킷 타입만** 봤다. 그런데 모든 EEPROM 읽기 응답은 타입이 똑같이
``0x8100`` 이고 주소만 다르다. 직전 명령의 늦은 응답이 그 관문을 통과해 대기를
끝내 버리고, 주소 검사는 ``read_memory_block`` 이 그 뒤에 하므로 이미 늦는다.
대기를 소진한 뒤라 진짜 응답은 기다리는 사람 없이 도착하고, 그것이 다시 다음
명령의 답으로 소비된다 — 한 번 어긋나면 계속 어긋난다.

리포터 로그(2.7.8-beta.15): ``Memory attempt 1/2/3 failed with late/
address-mismatched replies``. 재시도해도 매번 이전 응답을 소비했다.
"""
import asyncio

from custom_components.omron.omron_ble.devices import get_device_config
from custom_components.omron.omron_ble.omron_driver import OmronDeviceSession


def _crc(frame: bytearray) -> bytearray:
    """마지막 바이트를 XOR 체크섬으로 채운다 — 수신부가 이것부터 본다."""
    x = 0
    for b in frame[:-1]:
        x ^= b
    frame[-1] = x
    return frame


def _read_cmd(address: int, length: int) -> bytearray:
    cmd = bytearray([8, 0x01, 0x00]) + address.to_bytes(2, "big") + bytes([length, 0x00, 0x00])
    return _crc(cmd)


def _read_reply(address: int, length: int) -> bytearray:
    frame = bytearray([length + 8, 0x81, 0x00]) + address.to_bytes(2, "big")
    frame += bytes([length]) + bytes(length) + bytes([0x00, 0x00])
    return _crc(frame)


class _Session:
    """수신 경로만 떼어낸 최소 세션."""

    def __init__(self):
        self._config = get_device_config("HEM-7386T1")
        self._channel_fragments = [None] * 4
        self._notify_handle_to_channel = {}
        self._secure_session = None
        self._expected_reply_packet_type = None
        self._expected_reply_memory_address = None
        self._last_reply_packet_type = None
        self._last_reply_memory_address = None
        self._last_reply_payload = None
        self._reply_ready = asyncio.Event()

    def expect(self, command: bytearray) -> None:
        """_write_command_and_wait_reply 가 전송 직전에 세우는 기대값."""
        self._expected_reply_packet_type = bytes([command[1] | 0x80, command[2]])
        self._expected_reply_memory_address = bytes(command[3:5])

    def feed(self, frame: bytearray) -> None:
        OmronDeviceSession._on_notify_channel_data(self, object(), bytearray(frame))

    _on_notify_channel_data = OmronDeviceSession._on_notify_channel_data


def _fed(session, frame):
    session.feed(frame)
    return session._reply_ready.is_set()


def test_the_reply_to_the_command_in_flight_is_accepted():
    s = _Session()
    s.expect(_read_cmd(0x0E3C, 16))

    assert _fed(s, _read_reply(0x0E3C, 16)) is True
    assert s._last_reply_memory_address == (0x0E3C).to_bytes(2, "big")


def test_a_late_reply_for_another_address_does_not_end_the_wait():
    """같은 타입, 다른 주소. 예전에는 이것이 통과해 대기를 끝냈다."""
    s = _Session()
    s.expect(_read_cmd(0x0E3C, 16))

    assert _fed(s, _read_reply(0x0010, 16)) is False, "직전 명령의 응답을 소비하면 안 된다"
    assert s._last_reply_memory_address is None


def test_the_real_reply_still_lands_after_a_late_one():
    """늦은 프레임을 버렸으면 대기는 그대로 살아 있어야 한다 — 그게 회복의 전부다."""
    s = _Session()
    s.expect(_read_cmd(0x0E3C, 16))

    s.feed(_read_reply(0x0010, 16))
    assert s._reply_ready.is_set() is False

    assert _fed(s, _read_reply(0x0E3C, 16)) is True
    assert s._last_reply_memory_address == (0x0E3C).to_bytes(2, "big")


def test_a_reply_with_an_unexpected_length_is_still_accepted():
    """선언 길이는 일부러 비교하지 않는다.

    캡처가 있는 기기 계열은 둘뿐이라 나머지가 그 바이트에 무엇을 싣는지 모른다.
    요청 길이와 다르게 답하는 기기가 있으면 모든 응답이 버려지고 폴이 매번
    캐시로 떨어진다 — 이슈 #45(HEM-7196T1, WLD4.0)의 2.8.0 증상이 그 모양이었다.
    동시에 떠 있을 수 있는 응답을 가르는 데는 주소로 충분하다.
    """
    s = _Session()
    s.expect(_read_cmd(0x0E3C, 16))

    assert _fed(s, _read_reply(0x0E3C, 28)) is True
    assert s._last_reply_memory_address == (0x0E3C).to_bytes(2, "big")


def test_the_desync_cascade_cannot_start():
    """리포터가 본 모양: 늦은 응답 하나가 이후 모든 명령을 한 칸씩 밀어냈다.

    명령 세 개를 연달아 보내면서 매번 '한 명령 뒤처진' 응답만 주면, 예전 필터는
    세 번 다 받아들여 세 번 다 주소 불일치로 실패했다. 이제는 한 건도 받지 않고
    각 명령의 대기가 유지된다.
    """
    addresses = [0x0010, 0x0E3C, 0x0E4C]
    stale = None
    for address in addresses:
        s = _Session()
        s.expect(_read_cmd(address, 16))
        if stale is not None:
            assert _fed(s, stale) is False, hex(address)
        stale = _read_reply(address, 16)


def test_the_end_of_transmission_frame_is_still_accepted():
    """0x8f00 은 세션 종료 ack 이자 거부 응답이다 — 무엇을 물었든 받아야 한다."""
    s = _Session()
    s.expect(_read_cmd(0x0E3C, 16))

    frame = _crc(bytearray([8, 0x8F, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]))
    assert _fed(s, frame) is True
    assert s._last_reply_packet_type == bytes([0x8F, 0x00])


def test_no_expectation_accepts_anything():
    """기대값이 없으면 예전처럼 통과시킨다 — 이 변경은 대기 중일 때만 좁힌다."""
    s = _Session()

    assert _fed(s, _read_reply(0x0010, 16)) is True
