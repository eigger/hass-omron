"""SECURE_SESSION 프로파일의 자격증명 왕복 (#24).

이 전송 방식의 기기는 BLE 본드와 별개로 **애플리케이션 계층 자격증명**을 갖는다.
첫 세션이 초기화를 마치고 기기가 close 를 수락했을 때만 자격증명을 남기고, 이후
세션은 그걸 그대로 재생하며 아무것도 쓰지 않는다. 자격증명을 잃으면 사용자가
커프의 -P- 창을 다시 거쳐야 하므로, 저장 경로가 끊기지 않는 것이 중요하다.
"""

import asyncio
import contextlib
import dataclasses
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bleak.backends.device import BLEDevice
from homeassistant.config_entries import SOURCE_BLUETOOTH
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from tests.components.omron.bt import service_info
import custom_components.omron.config_flow as config_flow
from custom_components.omron import (
    _persist_transport_credential,
    async_poll_data,
    update_listener,
)
from custom_components.omron.const import (
    CONF_DEVICE_MODEL,
    CONF_TRANSPORT_CREDENTIAL,
    DOMAIN,
)
from custom_components.omron.omron_ble import OmronBluetoothDeviceData
from custom_components.omron.omron_ble import session as session_module
from custom_components.omron.omron_ble.const import OMRON_MANUFACTURER_ID
from custom_components.omron.omron_ble.devices import (
    HostPairingMode,
    UnlockMode,
    get_device_config,
)
from custom_components.omron.omron_ble.session import OmronDeviceSession
from custom_components.omron.omron_ble.settings_mirror import (
    SettingsMirrorLayout,
    clock_block,
)


class TestSettingsMirrorLayout:
    """초기화 주소는 프로파일에서 유도된다 — 한 기종에 박아넣지 않는다."""

    def test_addresses_come_from_the_profile(self):
        cfg = get_device_config("HEM-7188T1-LEO")
        layout = SettingsMirrorLayout(cfg)
        # 읽기/쓰기 영역은 카탈로그의 settings 주소 그대로,
        # 시계 레코드는 그 안의 time-sync 오프셋에 얹힌다.
        assert layout.head_read_address == cfg.settings_read_address
        assert layout.head_write_address == cfg.settings_write_address
        assert layout.clock_read_address == cfg.settings_read_address + 0x2C
        assert layout.clock_write_address == cfg.settings_write_address + 0x2C
        assert layout.clock_write_size == 0x3C - 0x2C

    def test_a_different_profile_shape_moves_every_address(self):
        """주소가 상수로 굳어 있으면 이 테스트가 잡는다."""
        other = SimpleNamespace(
            model="synthetic",
            settings_read_address=0x0100,
            settings_write_address=0x0200,
            settings_time_sync_bytes=[0x10, 0x20],
            index_pointer_layout={"index_region_byte_size": 0x08},
        )
        layout = SettingsMirrorLayout(other)
        assert layout.head_read_address == 0x0100
        assert layout.head_read_size == 0x10
        assert layout.head_write_address == 0x0200
        assert layout.head_write_size == 0x08
        assert layout.clock_read_address == 0x0110
        assert layout.clock_write_address == 0x0210
        assert layout.clock_write_size == 0x10

    def test_the_clock_read_always_covers_what_is_written_back(self):
        """짧게 읽으면 head 쓰기가 끝난 뒤에 ValueError 로 죽는다.

        시계 레코드 크기는 time-sync 범위에서 오고 읽기 길이는 인덱스 영역
        크기에서 왔는데, 후자가 더 작은 배치에서는 되쓸 레코드를 만들 수조차
        없다 — 그것도 기기 메모리를 이미 한 번 쓴 뒤에.
        """
        for index_size, time_sync in (
            (0x08, [0x10, 0x20]),  # 인덱스 영역이 레코드보다 작은 배치
            (0x18, [0x2C, 0x3C]),  # HEM-7188T1-LEO
            (0x40, [0x2C, 0x3C]),  # 인덱스 영역이 훨씬 큰 배치
        ):
            cfg = SimpleNamespace(
                model="synthetic",
                settings_read_address=0x0100,
                settings_write_address=0x0200,
                settings_time_sync_bytes=time_sync,
                index_pointer_layout={"index_region_byte_size": index_size},
            )
            layout = SettingsMirrorLayout(cfg)
            assert layout.clock_read_size >= layout.clock_write_size
            # 실제로 만들어봐야 의미가 있다: 주소만 검사하면 이 버그를 놓친다.
            block = clock_block(
                bytes(layout.clock_read_size),
                datetime(2026, 9, 6, 12, 30, 45),
                layout.clock_write_size,
            )
            assert len(block) == layout.clock_write_size

    def test_the_checksum_sits_at_the_end_of_the_record(self):
        """체크섬 위치를 14 로 박아두면 16바이트 레코드에서만 우연히 맞는다."""
        for size in (16, 20, 24):
            block = clock_block(
                bytes(range(size)), datetime(2026, 9, 6, 12, 30, 45), size
            )
            assert block[size - 2] == sum(block[: size - 2]) & 0xFF
        # #67 캡처의 실제 레코드로 규칙 자체를 고정한다.
        captured = bytes.fromhex("c8a80000010000001a06110f1806cf00")
        assert captured[14] == sum(captured[:14]) & 0xFF

    def test_an_incomplete_profile_is_rejected(self):
        bare = SimpleNamespace(
            model="bare",
            settings_read_address=None,
            settings_write_address=None,
            settings_time_sync_bytes=None,
            index_pointer_layout=None,
        )
        with pytest.raises(ValueError, match="settings mirror"):
            SettingsMirrorLayout(bare)


class TestOneSecurePath:
    """SECURE_SESSION 은 하나의 경로여야 한다 — 기종별 분기를 두지 않는다."""

    async def test_the_driver_has_no_model_whitelist(self, monkeypatch):
        """임의의 모델명이어도 unlock_mode 가 SECURE_SESSION 이면 동일하게 동작한다."""
        called = []

        async def fake_secure_unlock(sess) -> None:
            called.append(sess.config.model)

        monkeypatch.setattr(session_module, "_secure_unlock", fake_secure_unlock)
        cfg = dataclasses.replace(
            get_device_config("HEM-7188T1-LEO"),
            model="SYNTHETIC-SECURE-MODEL",
        )
        client = SimpleNamespace(is_connected=True, address="AA:BB:CC:DD:EE:FF")
        session = OmronDeviceSession.adopt(client, cfg)

        await session.unlock()

        assert called == ["SYNTHETIC-SECURE-MODEL"]

    async def test_the_secure_flow_dispatches_on_the_unlock_mode_alone(
        self, monkeypatch
    ):
        dispatched = []

        async def fake_secure(sess) -> None:
            dispatched.append(("secure", sess.config.unlock_mode))

        async def fake_token(sess, **_kw) -> None:
            dispatched.append(("token", sess.config.unlock_mode))

        monkeypatch.setattr(session_module, "_secure_unlock", fake_secure)
        monkeypatch.setattr(session_module, "_token_unlock", fake_token)
        client = SimpleNamespace(is_connected=True, address="AA:BB:CC:DD:EE:FF")

        base = get_device_config("HEM-7142T2")
        secure_cfg = dataclasses.replace(base, unlock_mode=UnlockMode.SECURE_SESSION)
        token_cfg = dataclasses.replace(base, unlock_mode=UnlockMode.TOKEN_KEY)

        await OmronDeviceSession.adopt(client, secure_cfg).unlock()
        await OmronDeviceSession.adopt(client, token_cfg).unlock()

        assert dispatched == [
            ("secure", UnlockMode.SECURE_SESSION),
            ("token", UnlockMode.TOKEN_KEY),
        ]


class TestPairingIsOptional:
    """커프가 스스로 보안을 주도하는 프로파일에서는 Pair() 를 부르지 않는다."""

    def test_the_profile_holds_an_agent_instead_of_pairing(self):
        cfg = get_device_config("HEM-7188T1-LEO")
        assert cfg.host_pairing_mode is HostPairingMode.NONE
        assert cfg.unlock_mode is UnlockMode.SECURE_SESSION
        # 에이전트가 없으면 BlueZ 5.72+ 는 Just Works 확인을 방치한다.
        assert cfg.register_pairing_agent is True

    async def test_the_agent_is_held_for_the_session_not_just_the_connect(
        self, monkeypatch
    ):
        """핸드셰이크 시점에 커프가 Security Request 를 올리면 답할 주체가 있어야
        한다. 하드웨어로 검증된 순서도 세션 내내 에이전트를 붙잡고 있었다."""
        agent_events: list[str] = []

        @contextlib.asynccontextmanager
        async def fake_agent(*_args):
            agent_events.append("enter")
            try:
                yield
            finally:
                agent_events.append("exit")

        fake_client = SimpleNamespace(
            is_connected=True,
            address="AA:BB:CC:DD:EE:FF",
            disconnect=AsyncMock(),
        )

        async def fake_establish(*_args, **_kwargs):
            assert agent_events == ["enter"], "연결 중에도 에이전트가 잡혀 있어야 한다"
            return fake_client

        monkeypatch.setattr(session_module, "is_local_adapter", lambda _dev: True)
        monkeypatch.setattr(session_module, "_bluez_pairing_agent", fake_agent)
        monkeypatch.setattr(
            session_module, "establish_connection_with_bond_settle", fake_establish
        )

        ble_dev = BLEDevice("AA:BB:CC:DD:EE:FF", "HEM-7188T1-LEO", {})
        cfg = dataclasses.replace(
            get_device_config("HEM-7188T1-LEO"), keep_notify_subscriptions=False
        )
        session = OmronDeviceSession(ble_dev, cfg)

        await session.connect()
        assert agent_events == ["enter"], (
            "connect() 가 끝난 뒤에도 세션이 닫힐 때까지 에이전트를 유지해야 한다"
        )

        await session.aclose()
        assert agent_events == ["enter", "exit"], (
            "세션이 끝나면 에이전트를 해제해야 한다"
        )

    async def test_pair_is_a_no_op_rather_than_an_error(self):
        """예외를 던지면 평범한 setup/재시도 경로가 전부 특수 분기를 져야 한다."""
        client = SimpleNamespace(
            is_connected=True,
            address="AA:BB:CC:DD:EE:FF",
            pair=AsyncMock(side_effect=AssertionError("pair() must not be called")),
        )
        session = OmronDeviceSession.adopt(client, get_device_config("HEM-7188T1-LEO"))
        await session.pair()
        client.pair.assert_not_awaited()


class TestCredentialRoundTrip:
    """저장 -> 세션 -> 갱신 -> 저장 고리가 어느 지점에서도 끊기면 안 된다."""

    def test_sessions_are_opened_with_the_stored_credential(self):
        data = OmronBluetoothDeviceData(device_model="HEM-7188T1-LEO")
        data.transport_credential = b"\x11\x22\x33\x44"
        ble_dev = BLEDevice("AA:BB:CC:DD:EE:FF", "HEM-7188T1-LEO", {})

        session = data._open_session(ble_dev)

        assert session._credential == b"\x11\x22\x33\x44"

    async def test_every_session_construction_goes_through_the_helper(
        self, monkeypatch
    ):
        """폴, 시간 동기화, 페어링 재시도 모두 저장된 자격증명을 세션에 전달한다."""
        from custom_components.omron.omron_ble import parser as parser_module

        seen_credentials: list[bytes | None] = []

        class _StopSession(Exception):
            pass

        def spy_session(
            ble_device,
            device_config,
            *,
            pairing_session: bool = False,
            credential: bytes | None = None,
        ):
            seen_credentials.append(credential)
            raise _StopSession

        monkeypatch.setattr(parser_module, "OmronDeviceSession", spy_session)
        data = OmronBluetoothDeviceData(device_model="HEM-7188T1-LEO")
        data.transport_credential = b"\xde\xad\xbe\xef"
        ble_dev = BLEDevice("AA:BB:CC:DD:EE:FF", "HEM-7188T1-LEO", {})

        for call in (
            lambda: data.async_poll(ble_dev),
            lambda: data.async_sync_time(ble_dev),
            lambda: data.async_retry_pairing(ble_dev),
        ):
            with pytest.raises(_StopSession):
                await call()

        assert seen_credentials == [b"\xde\xad\xbe\xef"] * 3

    async def test_the_entry_stores_what_a_pairing_established(
        self, hass: HomeAssistant, enable_bluetooth: None
    ) -> None:
        address = "AA:BB:CC:DD:EE:FF"

        class _CredentialSession:
            def __init__(self, *args, **kwargs) -> None:
                self.new_credential = bytes.fromhex("0102030405060708")
                self.is_connected = True

            async def connect(self):
                return self

            def release_for_handoff(self):
                return self

            def reclaim_ownership(self) -> None:
                return None

            async def aclose(self) -> None:
                return None

        with (
            patch.object(config_flow, "OmronDeviceSession", _CredentialSession),
            patch.object(config_flow, "async_pair_and_sync_device", AsyncMock()),
            patch.object(
                config_flow,
                "async_ble_device_from_address",
                return_value=BLEDevice(address, "HEM-7188T1-LEO", {}),
            ),
            patch("custom_components.omron.async_poll_data", AsyncMock()),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_BLUETOOTH},
                data=service_info(
                    address,
                    name="HEM-7188T1-LEO",
                    manufacturer_data={OMRON_MANUFACTURER_ID: bytes([0x01, 0, 0, 0])},
                ),
            )
            while result["type"] is FlowResultType.FORM:
                step = result["step_id"]
                user_input = (
                    {CONF_DEVICE_MODEL: "HEM-7188T1-LEO"}
                    if step == "select_model"
                    else {}
                )
                result = await hass.config_entries.flow.async_configure(
                    result["flow_id"], user_input=user_input
                )
            assert result["type"] is FlowResultType.CREATE_ENTRY
            await hass.async_block_till_done()

            (entry,) = hass.config_entries.async_entries(DOMAIN)
            assert entry.data[CONF_TRANSPORT_CREDENTIAL] == "0102030405060708"
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()

    async def test_storing_it_does_not_reload_the_integration(
        self, hass: HomeAssistant, enable_bluetooth: None
    ) -> None:
        """자격증명 쓰기는 코디네이터 갱신 도중 일어나므로 리로드를 유발하면 안 된다."""
        address = "AA:BB:CC:DD:EE:FF"
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id=address,
            title="HEM-7188T1-LEO EEFF",
            data={CONF_DEVICE_MODEL: "HEM-7188T1-LEO"},
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
        async with asyncio.timeout(5):
            while entry.runtime_data.background_tasks:
                await asyncio.sleep(0.01)

        entry.runtime_data.device_data.pending_credential = bytes.fromhex(
            "cafebabe12345678"
        )
        with patch.object(
            hass.config_entries, "async_reload", AsyncMock()
        ) as reload_mock:
            _persist_transport_credential(hass, entry, entry.runtime_data.device_data)
            await hass.async_block_till_done()

        assert entry.data[CONF_TRANSPORT_CREDENTIAL] == "cafebabe12345678"
        reload_mock.assert_not_awaited()
        assert entry.runtime_data.credential_write is False

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    def test_an_unloaded_entry_does_not_raise_from_the_listener(self):
        """Unload drops runtime_data after scheduling this listener. The task
        must return instead of raising AttributeError."""
        asyncio.run(update_listener(SimpleNamespace(), SimpleNamespace()))

    async def test_setup_loads_it_and_a_poll_writes_it_back(
        self, hass: HomeAssistant, enable_bluetooth: None
    ) -> None:
        address = "AA:BB:CC:DD:EE:FF"
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id=address,
            title="HEM-7188T1-LEO EEFF",
            data={
                CONF_DEVICE_MODEL: "HEM-7188T1-LEO",
                CONF_TRANSPORT_CREDENTIAL: "deadbeef00112233",
            },
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
        async with asyncio.timeout(5):
            while entry.runtime_data.background_tasks:
                await asyncio.sleep(0.01)

        data = entry.runtime_data.device_data
        assert data.transport_credential == bytes.fromhex("deadbeef00112233")

        async def fake_poll(ble_device, preconnected_session=None):
            data.pending_credential = bytes.fromhex("aabbccddeeff0011")
            return data._finish_update()

        with (
            patch(
                "custom_components.omron.async_ble_device_from_address",
                return_value=BLEDevice(address, "HEM-7188T1-LEO", {}),
            ),
            patch.object(data, "async_poll", side_effect=fake_poll),
        ):
            await async_poll_data(hass, entry)
            await hass.async_block_till_done()

        assert entry.data[CONF_TRANSPORT_CREDENTIAL] == "aabbccddeeff0011"

        # Writing the same credential again is a no-op (no config entry update).
        data.pending_credential = bytes.fromhex("aabbccddeeff0011")
        with patch.object(
            hass.config_entries, "async_update_entry", MagicMock()
        ) as update_mock:
            _persist_transport_credential(hass, entry, data)
        update_mock.assert_not_called()

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
