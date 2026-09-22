"""Deterministic behavioral regression tests for BP5465 reliability handling.

These tests intentionally exercise the real current integration call paths
without a physical cuff:

- process_service_info() forced-transfer latch/coalescing
- delayed forced-transfer drain after session-lock release
- in-flight consumption without a duplicate BLE poll
- failed delayed refresh re-latching
- forced-transfer device-disappearance fail-closed behavior
- async_poll_data() hard failure propagation with cached data present
- parser async_poll() connection-failure propagation
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from homeassistant.helpers.update_coordinator import UpdateFailed

import custom_components.omron as omron_init
from custom_components.omron.omron_ble.parser import OmronBluetoothDeviceData


ADDRESS = "C1:8D:32:97:D5:BB"


class FakeHass:
    def __init__(self):
        self.data = {}
        self.created_tasks: list[asyncio.Task] = []

    def async_create_task(self, coro):
        task = asyncio.create_task(coro)
        self.created_tasks.append(task)
        return task


class FakePollCoordinator:
    def __init__(
        self,
        *,
        success: bool = True,
        last_exception: Exception | None = None,
        cached_data=None,
    ):
        self.refresh_calls = 0
        self.last_update_success = success
        self.last_exception = last_exception
        self.data = cached_data

    async def async_request_refresh(self):
        raise AssertionError(
            "디바운스되는 async_request_refresh 는 force_poll_after_lock 을 세운 "
            "refresh 와 실제로 도는 refresh 를 다르게 만든다 — async_refresh 를 쓸 것"
        )

    async def async_refresh(self):
        self.refresh_calls += 1


class FakeAdvertisementData:
    def __init__(self):
        self.pairing_mode = False
        self.invalid_time = False
        self.forced_transfer = True

        self.last_readout_at = object()

        self.update_result = object()
        self.update_calls = 0

    def update(self, service_info):
        self.update_calls += 1
        return self.update_result


def _make_latch_harness(
    *,
    poll_success: bool = True,
    poll_exception: Exception | None = None,
):
    hass = FakeHass()

    poll = FakePollCoordinator(
        success=poll_success,
        last_exception=poll_exception,
    )

    data = FakeAdvertisementData()
    session_lock = asyncio.Lock()
    entry_id = "deterministic-test-entry"
    service_info = SimpleNamespace(address=ADDRESS, connectable=True)

    runtime = SimpleNamespace(
        address=ADDRESS,
        device_data=data,
        bt_coordinator=SimpleNamespace(hass=hass),
        poll_coordinator=poll,
        session_lock=session_lock,
        last_attempt_time=0.0,
        pending_forced_transfer=False,
        pending_forced_transfer_baseline=None,
        pending_forced_transfer_task=None,
        force_poll_after_lock=False,
    )

    entry = SimpleNamespace(
        entry_id=entry_id,
        runtime_data=runtime,
    )

    return SimpleNamespace(
        hass=hass,
        poll=poll,
        data=data,
        runtime=runtime,
        entry=entry,
        session_lock=session_lock,
        service_info=service_info,
    )


def test_locked_forced_transfer_coalesces_and_drains_exactly_once():
    async def scenario():
        h = _make_latch_harness()

        await h.session_lock.acquire()

        # A burst of repeated 0x41/Data Pending advertisements must collapse
        # into one pending latch and one waiter task.
        for _ in range(5):
            result = omron_init.process_service_info(
                h.entry,
                h.service_info,
            )
            assert result is h.data.update_result

        assert h.runtime.pending_forced_transfer is True
        assert len(h.hass.created_tasks) == 1
        assert h.poll.refresh_calls == 0

        task = h.runtime.pending_forced_transfer_task

        assert task is h.hass.created_tasks[0]
        assert task is not None
        assert not task.done()

        # Let the waiter reach session_lock.
        await asyncio.sleep(0)

        assert h.poll.refresh_calls == 0

        h.session_lock.release()

        await asyncio.wait_for(task, timeout=1.0)

        assert h.poll.refresh_calls == 1
        assert h.runtime.pending_forced_transfer is False
        assert h.runtime.pending_forced_transfer_task is None
        assert h.runtime.pending_forced_transfer_baseline is None
        assert h.runtime.force_poll_after_lock is False

    asyncio.run(scenario())


def test_locked_forced_transfer_consumed_by_inflight_poll_avoids_duplicate():
    async def scenario():
        h = _make_latch_harness()

        await h.session_lock.acquire()

        baseline = h.data.last_readout_at

        omron_init.process_service_info(
            h.entry,
            h.service_info,
        )

        task = h.runtime.pending_forced_transfer_task

        assert h.runtime.pending_forced_transfer_baseline is baseline

        # Simulate the session already owning the lock publishing a new
        # measurement before it releases the lock.
        h.data.last_readout_at = object()

        h.session_lock.release()

        await asyncio.wait_for(task, timeout=1.0)

        # The latch is consumed, not replayed as a second connection.
        assert h.poll.refresh_calls == 0
        assert h.runtime.pending_forced_transfer is False
        assert h.runtime.pending_forced_transfer_task is None
        assert h.runtime.pending_forced_transfer_baseline is None
        assert h.runtime.force_poll_after_lock is False

    asyncio.run(scenario())


def test_failed_delayed_forced_transfer_refresh_is_relatched():
    async def scenario():
        synthetic_failure = RuntimeError("synthetic delayed poll failure")

        h = _make_latch_harness(
            poll_success=False,
            poll_exception=synthetic_failure,
        )

        await h.session_lock.acquire()

        current_readout = h.data.last_readout_at

        omron_init.process_service_info(
            h.entry,
            h.service_info,
        )

        task = h.runtime.pending_forced_transfer_task

        h.session_lock.release()

        await asyncio.wait_for(task, timeout=1.0)

        assert h.poll.refresh_calls == 1

        # Failure must not discard the measurement request.
        assert h.runtime.pending_forced_transfer is True
        assert (
            h.runtime.pending_forced_transfer_baseline
            is current_readout
        )

        assert h.runtime.pending_forced_transfer_task is None
        assert h.runtime.force_poll_after_lock is False

    asyncio.run(scenario())


def test_non_forced_trigger_is_not_put_into_forced_transfer_latch():
    async def scenario():
        h = _make_latch_harness()

        h.data.forced_transfer = False
        h.data.pairing_mode = True

        await h.session_lock.acquire()

        try:
            omron_init.process_service_info(
                h.entry,
                h.service_info,
            )

            assert h.runtime.pending_forced_transfer is False
            assert h.runtime.pending_forced_transfer_task is None
            assert h.runtime.pending_forced_transfer_baseline is None
            assert len(h.hass.created_tasks) == 0
            assert h.poll.refresh_calls == 0
        finally:
            h.session_lock.release()

    asyncio.run(scenario())


def test_explicit_forced_transfer_missing_device_fails_and_consumes_marker(monkeypatch):
    async def scenario():
        poll = FakePollCoordinator(
            success=True,
            cached_data=object(),
        )
        runtime = SimpleNamespace(
            address=ADDRESS,
            session_lock=asyncio.Lock(),
            force_poll_after_lock=True,
            device_data=SimpleNamespace(),
            poll_coordinator=poll,
        )
        entry = SimpleNamespace(runtime_data=runtime)
        # The device was present for the Data Pending advertisement but is no
        # longer discoverable when the explicit refresh actually executes.
        monkeypatch.setattr(
            omron_init, "async_ble_device_from_address", lambda hass, address: None
        )

        # UpdateFailed: an expected BLE miss is a failed refresh, not an
        # integration bug. The cause stays on __cause__.
        with pytest.raises(
            UpdateFailed,
            match="disappeared before forced-transfer poll",
        ) as raised:
            await omron_init.async_poll_data(SimpleNamespace(), entry)
        assert isinstance(raised.value.__cause__, ConnectionError)

        # Marker must be consumed before discovery; callers re-latch the
        # measurement request rather than leaving a stale force flag behind.
        assert runtime.force_poll_after_lock is False

    asyncio.run(scenario())


def test_hard_poll_error_escapes_even_when_cached_data_exists(monkeypatch):
    async def scenario():
        cached = object()

        poll = FakePollCoordinator(
            success=True,
            cached_data=cached,
        )

        class FailingDeviceData:
            pending_credential = None
            last_readout_at = None

            async def async_poll(
                self,
                device,
                preconnected_session=None,
            ):
                raise RuntimeError(
                    "synthetic hard BLE poll failure"
                )

        session_lock = asyncio.Lock()
        runtime = SimpleNamespace(
            address=ADDRESS,
            session_lock=session_lock,
            force_poll_after_lock=False,
            device_data=FailingDeviceData(),
            poll_coordinator=poll,
            readout_coordinator=SimpleNamespace(
                async_set_updated_data=lambda value: None
            ),
        )
        entry = SimpleNamespace(runtime_data=runtime)

        monkeypatch.setattr(
            omron_init,
            "async_ble_device_from_address",
            lambda hass, address: SimpleNamespace(address=address),
        )
        monkeypatch.setattr(
            omron_init, "adopt_handoff_session", lambda hass, address: None
        )

        @asynccontextmanager
        async def noop_telemetry(hass, runtime, operation="poll"):
            yield

        monkeypatch.setattr(omron_init, "omron_poll_ble_telemetry", noop_telemetry)

        with pytest.raises(
            UpdateFailed,
            match="synthetic hard BLE poll failure",
        ) as raised:
            await omron_init.async_poll_data(SimpleNamespace(), entry)
        # The original failure has to stay attached so logs can name it.
        assert isinstance(raised.value.__cause__, RuntimeError)

        # Cached coordinator data must not turn a real BLE failure into a
        # successful SensorUpdate return.
        assert poll.data is cached

        # The async-with boundary must release the BLE session lock.
        assert not session_lock.locked()

    asyncio.run(scenario())


def test_parser_connection_error_escapes_instead_of_returning_finish_update():
    async def scenario():
        connection_error_type = ConnectionError

        class Events:
            def __init__(self):
                self.clear_calls = 0

            def clear(self):
                self.clear_calls += 1

        class FakeParserSelf:
            def __init__(self):
                self._poll_guard = asyncio.Lock()
                self._events_updates = Events()
                self._device_model = "HEM-7382T1-AZAZ"
                self.finish_calls = 0

            def _open_session(
                self,
                ble_device,
                pairing_session=False,
            ):
                raise connection_error_type(
                    "synthetic BLE disconnect"
                )

            def _finish_update(self):
                self.finish_calls += 1
                raise AssertionError(
                    "_finish_update must not run after hard connection failure"
                )

            def _record_session_trace(self, session, trace):
                self.last_session_trace = trace

        target = FakeParserSelf()

        ble_device = SimpleNamespace(
            address=ADDRESS,
        )

        with pytest.raises(
            connection_error_type,
            match="synthetic BLE disconnect",
        ):
            await OmronBluetoothDeviceData.async_poll(
                target,
                ble_device,
                preconnected_session=None,
            )

        assert target._events_updates.clear_calls == 1
        assert target.finish_calls == 0
        assert not target._poll_guard.locked()

    asyncio.run(scenario())

def test_the_completion_offset_is_validated_when_the_profile_is_built():
    """커프에 닿기 전, 임포트 시점에 걸려야 한다."""
    from custom_components.omron.omron_ble.devices import (
        DeviceConfig,
        MeasurementCompletion,
        TimeSyncLayout,
    )

    def _profile(**over):
        base = dict(
            model="synthetic",
            settings_read_address=0x0010,
            settings_write_address=0x0058,
            settings_time_sync_bytes=[0x30, 0x40],
            time_sync_layout=TimeSyncLayout.AT_8,
            index_pointer_layout={
                "index_region_byte_size": 0x1C,
                "users": [{"write_cursor_offset": 0, "unread_counter_offset": 4}],
            },
            measurement_completion=MeasurementCompletion(index_flag_offset=0x1B),
        )
        base.update(over)
        return DeviceConfig(**base)

    _profile()  # 정상 조합은 통과해야 의미가 있다
    with pytest.raises(ValueError, match="outside the .* index region"):
        _profile(measurement_completion=MeasurementCompletion(index_flag_offset=0x1C))
    # 완료 clock 쓰기는 clock_block()을 거친다: 16바이트, 플래그 byte 4, 시각
    # [8:14] 순서대로. 10바이트 classic 레코드나 swapped 레이아웃은 인덱스
    # 검사는 통과하고 쓰기에서 터진다 — 인덱스 미러를 이미 쓴 뒤에.
    with pytest.raises(ValueError, match="16-byte eeprom_time_at_8"):
        _profile(settings_time_sync_bytes=[0x14, 0x1E])
    with pytest.raises(ValueError, match="16-byte eeprom_time_at_8"):
        _profile(time_sync_layout=TimeSyncLayout.AT_8_SWAPPED)
