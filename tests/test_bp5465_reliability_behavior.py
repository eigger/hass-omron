"""Deterministic behavioral regression tests for BP5465 reliability handling.

These tests intentionally exercise the real current integration call paths
without a physical cuff:

- process_service_info() forced-transfer latch/coalescing
- delayed forced-transfer drain after session-lock release
- in-flight consumption without a duplicate BLE poll
- failed delayed refresh re-latching
- forced-transfer device-disappearance fail-closed behavior
- _async_poll_data() hard failure propagation with cached data present
- parser async_poll() connection-failure propagation

No production code is replaced or copied into this test.
"""

from __future__ import annotations

import ast
import asyncio
import copy
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import custom_components.omron as omron_init
import custom_components.omron.omron_ble.parser as parser_module


ADDRESS = "C1:8D:32:97:D5:BB"

INIT_PATH = Path(omron_init.__file__).resolve()
PARSER_PATH = Path(parser_module.__file__).resolve()


def _extract_async_function(path: Path, name: str, base_namespace: dict):
    """Compile one real async function from the current production source.

    This is used for nested _async_poll_data(), which normally exists only as a
    closure inside async_setup_entry(), and for parser async_poll(), which is a
    class method. The AST body is the exact current production implementation.
    """
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)

    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == name
    ]

    assert len(matches) == 1, (
        f"expected exactly one async function named {name!r}, "
        f"found {len(matches)}"
    )

    node = copy.deepcopy(matches[0])

    module = ast.Module(
        body=[node],
        type_ignores=[],
    )

    ast.fix_missing_locations(module)

    namespace = dict(base_namespace)

    exec(
        compile(
            module,
            filename=str(path),
            mode="exec",
        ),
        namespace,
    )

    return namespace[name], namespace


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

    coordinator = SimpleNamespace(
        hass=hass,
        device_data=data,
        poll_coordinator=poll,
    )

    entry_id = "deterministic-test-entry"

    entry = SimpleNamespace(
        entry_id=entry_id,
        runtime_data=coordinator,
    )

    session_lock = asyncio.Lock()

    entry_data = {
        "address": ADDRESS,
        "session_lock": session_lock,
        "last_attempt_time": 0.0,
    }

    hass.data = {
        omron_init.DOMAIN: {
            entry_id: entry_data,
        }
    }

    service_info = SimpleNamespace(
        address=ADDRESS,
        connectable=True,
    )

    return SimpleNamespace(
        hass=hass,
        poll=poll,
        data=data,
        coordinator=coordinator,
        entry=entry,
        entry_data=entry_data,
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

        assert h.entry_data["pending_forced_transfer"] is True
        assert len(h.hass.created_tasks) == 1
        assert h.poll.refresh_calls == 0

        task = h.entry_data["pending_forced_transfer_task"]

        assert task is h.hass.created_tasks[0]
        assert task is not None
        assert not task.done()

        # Let the waiter reach session_lock.
        await asyncio.sleep(0)

        assert h.poll.refresh_calls == 0

        h.session_lock.release()

        await asyncio.wait_for(task, timeout=1.0)

        assert h.poll.refresh_calls == 1
        assert h.entry_data["pending_forced_transfer"] is False
        assert h.entry_data["pending_forced_transfer_task"] is None
        assert "pending_forced_transfer_baseline" not in h.entry_data
        assert "force_poll_after_lock" not in h.entry_data

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

        task = h.entry_data["pending_forced_transfer_task"]

        assert h.entry_data["pending_forced_transfer_baseline"] is baseline

        # Simulate the session already owning the lock publishing a new
        # measurement before it releases the lock.
        h.data.last_readout_at = object()

        h.session_lock.release()

        await asyncio.wait_for(task, timeout=1.0)

        # The latch is consumed, not replayed as a second connection.
        assert h.poll.refresh_calls == 0
        assert h.entry_data["pending_forced_transfer"] is False
        assert h.entry_data["pending_forced_transfer_task"] is None
        assert "pending_forced_transfer_baseline" not in h.entry_data
        assert "force_poll_after_lock" not in h.entry_data

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

        task = h.entry_data["pending_forced_transfer_task"]

        h.session_lock.release()

        await asyncio.wait_for(task, timeout=1.0)

        assert h.poll.refresh_calls == 1

        # Failure must not discard the measurement request.
        assert h.entry_data["pending_forced_transfer"] is True
        assert (
            h.entry_data["pending_forced_transfer_baseline"]
            is current_readout
        )

        assert h.entry_data["pending_forced_transfer_task"] is None
        assert "force_poll_after_lock" not in h.entry_data

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

            assert "pending_forced_transfer" not in h.entry_data
            assert "pending_forced_transfer_task" not in h.entry_data
            assert "pending_forced_transfer_baseline" not in h.entry_data
            assert len(h.hass.created_tasks) == 0
            assert h.poll.refresh_calls == 0
        finally:
            h.session_lock.release()

    asyncio.run(scenario())


def test_explicit_forced_transfer_missing_device_fails_and_consumes_marker():
    async def scenario():
        poll = FakePollCoordinator(
            success=True,
            cached_data=object(),
        )

        readout = SimpleNamespace(
            async_set_updated_data=lambda value: None,
        )

        async_poll_data, namespace = _extract_async_function(
            INIT_PATH,
            "_async_poll_data",
            omron_init.__dict__,
        )

        namespace["poll_coordinator"] = poll
        namespace["readout_coordinator"] = readout
        namespace["_persist_transport_credential"] = (
            lambda hass, entry, device_data: None
        )

        # The key condition for the P2.16B.3 race:
        # the device was present for the Data Pending advertisement but is no
        # longer discoverable when the explicit refresh actually executes.
        namespace["async_ble_device_from_address"] = (
            lambda hass, address: None
        )

        entry_id = "missing-device-entry"

        entry_data = {
            "address": ADDRESS,
            "session_lock": asyncio.Lock(),
            "force_poll_after_lock": True,
        }

        hass = SimpleNamespace(
            data={
                omron_init.DOMAIN: {
                    entry_id: entry_data,
                }
            }
        )

        device_data = SimpleNamespace()

        entry = SimpleNamespace(
            entry_id=entry_id,
            runtime_data=SimpleNamespace(
                device_data=device_data,
            ),
        )

        with pytest.raises(
            ConnectionError,
            match="disappeared before forced-transfer poll",
        ):
            await async_poll_data(
                hass,
                entry,
            )

        # Marker must be consumed before discovery; callers re-latch the
        # measurement request rather than leaving a stale force flag behind.
        assert "force_poll_after_lock" not in entry_data

    asyncio.run(scenario())


def test_hard_poll_error_escapes_even_when_cached_data_exists():
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

        device_data = FailingDeviceData()

        async_poll_data, namespace = _extract_async_function(
            INIT_PATH,
            "_async_poll_data",
            omron_init.__dict__,
        )

        namespace["poll_coordinator"] = poll

        namespace["readout_coordinator"] = SimpleNamespace(
            async_set_updated_data=lambda value: None,
        )

        namespace["_persist_transport_credential"] = (
            lambda hass, entry, parser: None
        )

        namespace["async_ble_device_from_address"] = (
            lambda hass, address: SimpleNamespace(
                address=address,
            )
        )

        namespace["adopt_handoff_session"] = (
            lambda hass, address: None
        )

        @asynccontextmanager
        async def noop_telemetry(entry_data):
            yield

        namespace["omron_poll_ble_telemetry"] = noop_telemetry

        entry_id = "hard-failure-entry"

        session_lock = asyncio.Lock()

        hass = SimpleNamespace(
            data={
                omron_init.DOMAIN: {
                    entry_id: {
                        "address": ADDRESS,
                        "session_lock": session_lock,
                    }
                }
            }
        )

        entry = SimpleNamespace(
            entry_id=entry_id,
            runtime_data=SimpleNamespace(
                device_data=device_data,
            ),
        )

        with pytest.raises(
            RuntimeError,
            match="synthetic hard BLE poll failure",
        ):
            await async_poll_data(
                hass,
                entry,
            )

        # Most important regression check:
        # cached coordinator data must NOT convert a real BLE failure into a
        # successful SensorUpdate return.
        assert poll.data is cached

        # The async-with boundary must release the BLE session lock.
        assert not session_lock.locked()

    asyncio.run(scenario())


def test_parser_connection_error_escapes_instead_of_returning_finish_update():
    async def scenario():
        async_poll, namespace = _extract_async_function(
            PARSER_PATH,
            "async_poll",
            parser_module.__dict__,
        )

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

        target = FakeParserSelf()

        ble_device = SimpleNamespace(
            address=ADDRESS,
        )

        with pytest.raises(
            connection_error_type,
            match="synthetic BLE disconnect",
        ):
            await async_poll(
                target,
                ble_device,
                preconnected_session=None,
            )

        assert target._events_updates.clear_calls == 1
        assert target.finish_calls == 0
        assert not target._poll_guard.locked()

    asyncio.run(scenario())