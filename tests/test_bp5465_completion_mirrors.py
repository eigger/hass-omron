from __future__ import annotations

import asyncio
import datetime as dt
import pathlib

import pytest

from custom_components.omron.omron_ble.devices import get_device_config
from custom_components.omron.omron_ble.driver import OmronDeviceDriver
from custom_components.omron.omron_ble.session import OmronDeviceSession


class _CompletionTransport:
    def __init__(self, model: str, *, active: bool = True) -> None:
        self._config = get_device_config(model)
        self._memory_session_active = active
        self._last_reply_packet_type = bytearray.fromhex("8f00")
        self._last_reply_payload = bytearray([0])
        self.address = "test-device"

        self.reads: list[tuple[int, int]] = []
        self.writes: list[tuple[int, bytes]] = []
        self.commands: list[str] = []
        self.events: list[str] = []

    @property
    def memory_session_active(self) -> bool:
        return self._memory_session_active

    async def read_memory_block(self, address: int, blocksize: int) -> bytes:
        self.reads.append((address, blocksize))
        self.events.append(f"read:{address:04x}:{blocksize}")

        if (address, blocksize) == (0x0010, 0x1C):
            return bytes(range(0x1C))

        if (address, blocksize) == (0x0040, 0x10):
            return bytes(range(0x10))

        raise AssertionError(
            f"unexpected read address=0x{address:04x} length={blocksize}"
        )

    async def write_memory_block(self, address: int, data: bytearray) -> None:
        payload = bytes(data)
        self.writes.append((address, payload))
        self.events.append(f"write:{address:04x}")

    async def _write_command_and_wait_reply(self, command: bytearray) -> None:
        self.commands.append(command.hex())
        self.events.append(f"command:{command.hex()}")
        self._last_reply_packet_type = bytearray.fromhex("8f00")
        self._last_reply_payload = bytearray([0])

    async def _unsubscribe_notify_channels(self, force: bool = False) -> None:
        self.events.append("unsubscribe")


_NOW = dt.datetime(2026, 9, 18, 21, 54, 23)


def _driver(model: str) -> OmronDeviceDriver:
    driver = OmronDeviceDriver(get_device_config(model))
    driver._now_func = lambda: _NOW
    return driver


def _run_completion_and_close(model: str) -> _CompletionTransport:
    target = _CompletionTransport(model)
    driver = _driver(model)

    async def run() -> None:
        await driver.complete_measurement_readout(target)
        await OmronDeviceSession.close_memory_session(target)

    asyncio.run(run())
    return target


def test_bp5465_completion_mirrors_precede_normal_memory_close():
    target = _run_completion_and_close("BP5465")

    assert target.reads == [
        (0x0010, 0x1C),
        (0x0040, 0x10),
    ]

    assert [address for address, _ in target.writes] == [
        0x0058,
        0x0088,
    ]

    assert target.events[:5] == [
        "read:0010:28",
        "write:0058",
        "read:0040:16",
        "write:0088",
        "command:080f000000000007",
    ]

    index_payload = target.writes[0][1]
    assert len(index_payload) == 0x1C
    assert index_payload[0x1B] == 0x80

    status_payload = target.writes[1][1]
    assert len(status_payload) == 0x10
    assert status_payload[4] == 0x04 | 0x01  # the flag bit set, the rest kept
    assert status_payload[8:14] == bytes((26, 9, 18, 21, 54, 23))
    assert status_payload[14] == (sum(status_payload[:14]) & 0xFF)
    assert status_payload[15] == 0x00


def test_hem7382_ack2_checksum_is_recomputed_after_stamping():
    target = _CompletionTransport("HEM-7382T1-AZAZ")
    driver = _driver("HEM-7382T1-AZAZ")

    asyncio.run(driver.complete_measurement_readout(target))

    assert len(target.writes) == 2

    status_payload = target.writes[1][1]
    assert status_payload[4] == 0x04 | 0x01  # the flag bit set, the rest kept
    assert status_payload[8:14] == bytes((26, 9, 18, 21, 54, 23))
    assert status_payload[14] == (sum(status_payload[:14]) & 0xFF)
    assert status_payload[15] == 0x00


def test_completion_clock_carries_the_current_time_not_the_cuff_clock():
    """#190: the completion clock write is the last one of the session and the
    one with the flag set, so it must carry the current time. Copying the read
    bytes back re-sent the cuff its own stale clock and undid the time sync."""
    target = _CompletionTransport("BP5465")
    driver = _driver("BP5465")

    asyncio.run(driver.complete_measurement_readout(target))

    # The read returns bytes(range(16)): a "cuff clock" of 08 09 0a 0b 0c 0d.
    stale = bytes(range(0x10))[8:14]
    status_payload = target.writes[1][1]
    assert status_payload[8:14] != stale
    assert status_payload[8:14] == bytes((26, 9, 18, 21, 54, 23))
    # Everything ahead of the time is still the cuff's own bytes, flag aside.
    assert status_payload[:4] == bytes(range(4))
    assert status_payload[5:8] == bytes(range(5, 8))


def test_all_catalog_variants_resolving_to_hem7386_use_same_completion():
    models = (
        "BP5465",
        "HEM-7382T1",
        "HEM-7382T1-AZAZ",
        "HEM-7381T1-AZ",
        "HEM-7386T1",
        "HEM-7386T1-AJF3",
        "HEM-7388T1-AJF3",
    )

    for model in models:
        target = _CompletionTransport(model)
        driver = _driver(model)

        asyncio.run(driver.complete_measurement_readout(target))

        assert target.reads == [
            (0x0010, 0x1C),
            (0x0040, 0x10),
        ], model

        assert [address for address, _ in target.writes] == [
            0x0058,
            0x0088,
        ], model

        status_payload = target.writes[1][1]
        assert status_payload[4] == 0x04 | 0x01, model
        assert status_payload[8:14] == bytes((26, 9, 18, 21, 54, 23)), model
        assert status_payload[14] == (sum(status_payload[:14]) & 0xFF), model


def test_unrelated_profile_has_no_completion_mirrors():
    target = _CompletionTransport("HEM-7142T2")
    driver = _driver("HEM-7142T2")

    asyncio.run(driver.complete_measurement_readout(target))

    assert target.reads == []
    assert target.writes == []
    assert target.events == []


def test_completion_requires_an_active_memory_session():
    target = _CompletionTransport("BP5465", active=False)
    driver = _driver("BP5465")

    with pytest.raises(
        ConnectionError,
        match="active memory session",
    ):
        asyncio.run(driver.complete_measurement_readout(target))

    assert target.reads == []
    assert target.writes == []


def test_cleanup_close_alone_never_writes_completion_mirrors():
    target = _CompletionTransport("BP5465")

    asyncio.run(
        OmronDeviceSession.close_memory_session(target)
    )

    assert target.reads == []
    assert target.writes == []
    assert target.commands == [
        "080f000000000007",
    ]


def test_poll_calls_completion_only_at_successful_eeprom_readout_boundary():
    parser_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "custom_components"
        / "omron"
        / "omron_ble"
        / "parser.py"
    )

    source = parser_path.read_text(encoding="utf-8")

    start = source.index(
        "    async def _poll_device_readout("
    )

    end = source.index(
        "    async def async_poll(",
        start,
    )

    poll = source[start:end]

    assert poll.count(
        "await self._driver.complete_measurement_readout(session)"
    ) == 1

    # 조건은 두 가지를 함께 요구한다: 메모리 세션이 열려 있고, 실제로 EEPROM
    # 레코드가 디코드됐을 것. 여기에 페어링 세션 제외가 더해진다 — 그 세션은
    # 끝에서 등록 쓰기가 같은 두 주소에 상위집합을 쓰므로 둘 다 돌면 쓰기가
    # 네 번 나가고 앞의 두 번은 덮인다.
    for required in (
        "memory_session_active",
        "eeprom_record_decoded",
        "session._pairing_session",
        "self._device_config.pairing_registration is not None",
    ):
        assert required in poll, required

    completion_at = poll.index(
        "await self._driver.complete_measurement_readout(session)"
    )

    publication_at = poll.index(
        "self._update_measurement_sensors("
    )

    firmware_read_at = poll.index(
        "char_fw = client.services.get_characteristic(FIRMWARE_REVISION_UUID)"
    )

    model_read_at = poll.index(
        "char_model = client.services.get_characteristic(MODEL_NUMBER_UUID)"
    )

    assert publication_at < completion_at
    assert completion_at < firmware_read_at
    assert completion_at < model_read_at