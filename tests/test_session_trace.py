"""BLE 세션의 단계별 소요 시간과 실패 지점을 진단 센서 속성으로 낸다.

폴이 왜 실패했는지는 디버그 로그에만 남았다. ``SessionTrace`` 가 같은 내용을
데이터로 기록하고, ``build_session_report`` 가 HA 만 아는 정보(어느 라디오,
RSSI, 경로 수)와 한 줄 진단(``likely_cause``)을 붙여 Duration / Last Failure
센서의 속성으로 내보낸다.
"""
import asyncio
import sys
from types import SimpleNamespace

import pytest
from blesession import ConnectFailed
from blesession.link import LinkInfo

from custom_components.omron.omron_ble.session_trace import SessionTrace, traced
from custom_components.omron import session_report
from custom_components.omron import session_handoff


# ── SessionTrace ────────────────────────────────────────────────────────────


class TestSessionTrace:
    def test_stages_are_timed_in_run_order_with_a_seconds_suffix(self):
        trace = SessionTrace()
        with trace.timed("connect"):
            pass
        with trace.timed("readout"):
            pass
        assert list(trace.as_dict()) == ["connect_s", "readout_s"]

    def test_the_innermost_stage_an_exception_escapes_is_the_failed_stage(self):
        trace = SessionTrace()
        with pytest.raises(RuntimeError):
            with trace.timed("session"):
                with trace.timed("unlock"):
                    raise RuntimeError("boom")
        assert trace.failed_stage == "unlock"
        assert trace.stage is None, "스택이 예외 뒤에도 비워져야 한다"
        assert trace.as_dict()["failed_stage"] == "unlock"

    def test_the_first_failure_wins_over_a_close_that_also_fails(self):
        """readout 에서 죽은 뒤 memory_close 도 실패하면 원인은 readout 이다."""
        trace = SessionTrace()
        with pytest.raises(RuntimeError):
            with trace.timed("readout"):
                raise RuntimeError("link dropped")
        with pytest.raises(RuntimeError):
            with trace.timed("memory_close"):
                raise RuntimeError("no reply")
        assert trace.failed_stage == "readout"

    def test_a_swallowed_failure_is_forgiven_so_a_later_real_one_is_recorded(self):
        """time_sync 실패는 삼켜지고 폴은 계속된다 — 그 뒤 readout 실패가 원인이다."""
        trace = SessionTrace()
        try:
            with trace.timed("time_sync"):
                raise RuntimeError("clock write failed")
        except RuntimeError:
            trace.forgive("time_sync")
        assert trace.failed_stage is None
        with pytest.raises(RuntimeError):
            with trace.timed("readout"):
                raise RuntimeError("link dropped")
        assert trace.failed_stage == "readout"

    def test_forgive_only_clears_its_own_stage(self):
        trace = SessionTrace()
        with pytest.raises(RuntimeError):
            with trace.timed("unlock"):
                raise RuntimeError()
        trace.forgive("pair")
        assert trace.failed_stage == "unlock"
        trace.forgive()  # 재시도 루프: 무엇이었든 지운다
        assert trace.failed_stage is None

    def test_a_repeated_stage_adds_up(self):
        trace = SessionTrace()
        for _ in range(3):
            with trace.timed("unlock"):
                pass
        assert list(trace.as_dict()) == ["unlock_s"]

    def test_notes_drop_none_and_come_before_timings(self):
        trace = SessionTrace()
        with trace.timed("connect"):
            pass
        trace.note(records=2, time_sync_error=None)
        assert list(trace.as_dict()) == ["records", "connect_s"]
        assert trace.as_dict()["records"] == 2

    def test_traced_times_a_method_on_a_host_with_a_trace(self):
        class Host:
            def __init__(self):
                self.trace = SessionTrace()

            @traced("unlock")
            async def unlock(self):
                raise ConnectionError("pairing key mismatch")

        host = Host()
        with pytest.raises(ConnectionError):
            asyncio.run(host.unlock())
        assert host.trace.failed_stage == "unlock"
        assert "unlock_s" in host.trace.as_dict()

    def test_traced_runs_untimed_on_a_host_without_a_trace(self):
        """테스트용 트랜스포트 스탠드인은 trace 가 없다 — 그래도 동작해야 한다."""

        class Host:
            @traced("unlock")
            async def unlock(self):
                return "ok"

        assert asyncio.run(Host().unlock()) == "ok"


# ── build_session_report ────────────────────────────────────────────────────


class _Scanner:
    def __init__(self, name, rssi=None):
        self.name = name
        self._rssi = rssi

    def get_discovered_device_advertisement_data(self, address):
        if self._rssi is None:
            return None
        return (object(), SimpleNamespace(rssi=self._rssi))


class _Remote(_Scanner):
    pass


@pytest.fixture
def radio(monkeypatch):
    """``blesession.hass.radio_facts`` 가 부르는 HA bluetooth 헬퍼를 스캐너 표로 대체한다."""
    scanners: dict[str, _Scanner] = {}
    paths: list[object] = []
    # homeassistant itself is a MagicMock, so a submodule import does not
    # reuse sys.modules unless the parent attribute points at it. radio_facts
    # imports the bluetooth helpers at call time.
    components = sys.modules["homeassistant.components"]
    bluetooth = sys.modules["homeassistant.components.bluetooth"]
    monkeypatch.setattr(sys.modules["homeassistant"], "components", components)
    monkeypatch.setattr(components, "bluetooth", bluetooth)
    monkeypatch.setattr(bluetooth, "BaseHaRemoteScanner", _Remote)
    monkeypatch.setattr(bluetooth, "BaseHaScanner", _Scanner)
    monkeypatch.setattr(
        bluetooth, "async_scanner_by_source", lambda hass, source: scanners.get(source)
    )
    monkeypatch.setattr(
        bluetooth,
        "async_scanner_devices_by_address",
        lambda hass, address, connectable=True: paths,
    )
    monkeypatch.setattr(
        bluetooth,
        "async_last_service_info",
        lambda hass, address, connectable=True: None,
    )
    return scanners, paths


ADDRESS = "00:5F:BF:F4:43:D1"


class TestBuildSessionReport:
    def test_a_successful_poll_reports_outcome_radio_then_trace(self, radio):
        scanners, paths = radio
        scanners["AA:BB"] = _Remote("proxy-bedroom (AA:BB)", rssi=-70)
        paths.extend([object(), object()])
        trace = SessionTrace()
        trace.link = LinkInfo(via="AA:BB", source="AA:BB")
        trace.record("connect", 2.1)
        trace.record("readout", 4.0)
        trace.note(connect_attempts=1, records=1)

        report = session_report.build_session_report(
            None, ADDRESS, operation="poll", trace=trace, exc=None
        )

        assert list(report) == [
            "operation", "success", "via", "via_type", "rssi", "paths",
            "connect_s", "readout_s", "connect_attempts", "records",
        ]
        assert report["success"] is True
        assert report["via"] == "proxy-bedroom (AA:BB)"
        assert report["via_type"] == "proxy"
        assert report["rssi"] == -70
        assert report["paths"] == 2
        assert "failed_stage" not in report and "error" not in report

    def test_a_failed_poll_names_the_stage_the_error_and_a_cause(self, radio):
        scanners, paths = radio
        scanners["hci0-src"] = _Scanner("hci0 (DC:A6)", rssi=-90)
        paths.append(object())
        trace = SessionTrace()
        trace.link = LinkInfo(via="/org/bluez/hci0/dev_00_5F", source="hci0-src")
        trace.fail("connect")
        trace.record("connect", 31.0)
        trace.note(connect_attempts=3)

        report = session_report.build_session_report(
            None,
            ADDRESS,
            operation="poll",
            trace=trace,
            exc=ConnectFailed(
                "dropped during the post-connect settle on all 3 attempt(s)",
                detail="settle",
            ),
        )

        assert report["success"] is False
        assert report["failed_stage"] == "connect"
        assert report["error"].startswith("dropped during")
        assert "bond" in report["likely_cause"]
        # 어댑터 소스로 폴백해 이름과 RSSI 를 얻는다.
        assert report["via"] == "hci0 (DC:A6)"
        assert report["via_type"] == "adapter"
        assert report["rssi"] == -90
        assert report["paths"] == 1
        assert report["connect_attempts"] == 3

    def test_a_link_over_another_radio_than_the_advertised_one_shows_both(self, radio):
        """#91: 광고는 A 가 가장 세게 봤지만 본드는 B 가 들고 있어 B 로 연결된 경우."""
        scanners, _ = radio
        scanners["A"] = _Remote("proxy-a", rssi=-60)
        scanners["B"] = _Remote("proxy-b", rssi=-75)

        trace = SessionTrace()
        trace.link = LinkInfo(via="B", source="A")
        report = session_report.build_session_report(
            None, ADDRESS, operation="poll", trace=trace, exc=None
        )
        assert report["via"] == "proxy-b"
        assert report["advertised_via"] == "proxy-a"
        assert report["rssi"] == -75, "RSSI 는 실제 링크가 탄 라디오 기준"

    def test_the_same_radio_is_not_reported_twice(self, radio):
        scanners, _ = radio
        scanners["A"] = _Remote("proxy-a", rssi=-60)
        trace = SessionTrace()
        trace.link = LinkInfo(via="A", source="A")
        report = session_report.build_session_report(
            None, ADDRESS, operation="poll", trace=trace, exc=None
        )
        assert "advertised_via" not in report

    def test_an_unresolvable_link_keeps_the_raw_path(self, radio):
        trace = SessionTrace()
        trace.link = LinkInfo(via="some-proxy")
        report = session_report.build_session_report(
            None, ADDRESS, operation="poll", trace=trace, exc=None
        )
        assert report["via"] == "some-proxy"
        assert "via_type" not in report

    def test_a_deadline_timeout_is_named_even_without_a_message(self, radio):
        trace = SessionTrace()
        trace.fail("readout")
        report = session_report.build_session_report(
            None, ADDRESS, operation="poll", trace=trace, exc=TimeoutError()
        )
        assert "deadline" in report["error"]
        assert "deadline" in report["likely_cause"]
        assert report["failed_stage"] == "transfer"
        assert report["failed_detail"] == "readout"

    def test_a_timeout_with_a_message_is_an_ordinary_error(self, radio):
        trace = SessionTrace()
        trace.fail("connect")
        report = session_report.build_session_report(
            None, ADDRESS, operation="poll", trace=trace,
            exc=TimeoutError("Timeout waiting for connect response"),
        )
        assert report["error"] == "Timeout waiting for connect response"
        assert "normal state" in report["likely_cause"]

    def test_a_session_that_died_before_the_parser_has_no_stages(self, radio):
        report = session_report.build_session_report(
            None, ADDRESS, operation="pairing", trace=None, exc=RuntimeError("x")
        )
        assert "failed_stage" not in report
        assert "-P-" in report["likely_cause"]

    def test_a_settle_drop_reaches_the_report_as_failed_detail(self, radio):
        trace = SessionTrace()
        trace.fail("connect")
        exc = ConnectFailed("dropped during settle", detail="settle")
        report = session_report.build_session_report(
            None, ADDRESS, operation="poll", trace=trace, exc=exc
        )
        assert report["failed_stage"] == "connect"
        assert report["failed_detail"] == "settle"
        assert "bond" in report["likely_cause"]


class TestLikelyCause:
    def test_a_sleeping_cuff_is_called_normal(self):
        cause = session_report.likely_cause(
            "connect", None, "Failed to connect after 4 attempt(s): timed out", {}, "poll"
        )
        assert "normal state" in cause

    def test_weak_signal_adds_placement_advice_only_when_weak(self):
        strong = session_report.likely_cause(
            "connect", None, "x", {"rssi": -60, "via": "p", "paths": 1}, "poll"
        )
        weak = session_report.likely_cause(
            "connect", None, "x", {"rssi": -90, "via": "p", "paths": 1}, "poll"
        )
        assert "weak" not in strong
        assert "-90 dBm via p" in weak and "no other radio" in weak

    @pytest.mark.parametrize(
        ("stage", "detail", "error", "needle"),
        [
            ("connect", None, "no free slot on proxy", "slot"),
            ("session", "services", "Required service not found", "model"),
            ("auth", "pair", "Could not enter key programming mode", "-P-"),
            ("auth", "unlock", "Unlock failed: pairing key mismatch", "phone app"),
            ("auth", "unlock", "No stored transport credential", "re-add"),
            ("auth", "unlock", "PIN or Key Missing", "bond"),
            ("auth", "memory_open", "Device rejected memory session open", "readout session"),
            ("transfer", "readout", "disconnected", "record memory"),
            ("finish", "memory_close", "no reply", "records were read"),
        ],
    )
    def test_each_stage_reads_differently(self, stage, detail, error, needle):
        assert needle in session_report.likely_cause(stage, detail, error, {}, "poll")


# ── omron_poll_ble_telemetry ────────────────────────────────────────────────


class _Coordinator:
    def __init__(self):
        self.values: list[object] = []
        self.data = None

    def async_set_updated_data(self, value):
        self.values.append(value)
        self.data = value


def _entry_data():
    return {
        "address": ADDRESS,
        "data": SimpleNamespace(last_session_trace=None),
        "connection_coordinator": _Coordinator(),
        "duration_coordinator": _Coordinator(),
        "failure_coordinator": _Coordinator(),
        "failure_count_coordinator": _Coordinator(),
    }


@pytest.fixture
def plain_report(monkeypatch):
    """HA 라디오 조회 없이 outcome + trace 만으로 리포트를 만든다."""
    monkeypatch.setattr(
        session_handoff,
        "build_session_report",
        lambda hass, address, *, operation, trace, exc: {
            "operation": operation,
            "success": exc is None,
            **({"error": str(exc)} if exc else {}),
            **(trace or {}),
        },
    )


class TestTelemetry:
    def test_a_failed_session_stamps_last_failure_with_its_own_copy(self, plain_report):
        entry_data = _entry_data()

        async def scenario():
            with pytest.raises(ConnectionError):
                async with session_handoff.omron_poll_ble_telemetry(None, entry_data, "poll"):
                    entry_data["data"].last_session_trace = {"failed_stage": "connect"}
                    raise ConnectionError("cuff asleep")

        asyncio.run(scenario())

        timing = entry_data["last_session_timing"]
        assert timing["success"] is False
        assert timing["failed_stage"] == "connect"
        assert timing["error"] == "cuff asleep"
        assert entry_data["last_failure_timing"] == timing
        assert entry_data["last_failure_timing"] is not timing, "복사본이어야 한다"
        assert len(entry_data["failure_coordinator"].values) == 1
        assert entry_data["failure_count_coordinator"].data == 1
        assert entry_data["connection_coordinator"].values[-1] is False

    def test_a_later_success_updates_duration_but_keeps_the_failure(self, plain_report):
        entry_data = _entry_data()

        async def scenario():
            with pytest.raises(ConnectionError):
                async with session_handoff.omron_poll_ble_telemetry(None, entry_data, "poll"):
                    raise ConnectionError("first")
            async with session_handoff.omron_poll_ble_telemetry(None, entry_data, "poll"):
                entry_data["data"].last_session_trace = {"records": 1}

        asyncio.run(scenario())

        assert entry_data["last_session_timing"]["success"] is True
        assert entry_data["last_session_timing"]["records"] == 1
        assert entry_data["last_failure_timing"]["error"] == "first"
        assert len(entry_data["failure_coordinator"].values) == 1
        assert entry_data["failure_count_coordinator"].data == 1

    def test_the_report_lands_before_the_final_duration_update(self, monkeypatch, plain_report):
        """Duration 의 마지막 갱신이 엔티티 상태(속성 포함)를 쓴다 — 그 전에 있어야 한다."""
        entry_data = _entry_data()
        seen: list[object] = []

        class Duration(_Coordinator):
            def async_set_updated_data(self, value):
                seen.append(entry_data.get("last_session_timing"))
                super().async_set_updated_data(value)

        entry_data["duration_coordinator"] = Duration()

        async def scenario():
            async with session_handoff.omron_poll_ble_telemetry(None, entry_data, "poll"):
                pass

        asyncio.run(scenario())
        assert seen[-1] is not None and seen[-1]["success"] is True

    def test_no_attributes_while_the_session_runs(self, plain_report):
        """1초 티커가 이전 세션의 분해를 이번 세션의 시간에 붙여 기록하면 안 된다."""
        entry_data = _entry_data()
        entry_data["last_session_timing"] = {"success": False, "failed_stage": "connect"}
        during: list[object] = []

        async def scenario():
            async with session_handoff.omron_poll_ble_telemetry(None, entry_data, "poll"):
                during.append(entry_data["last_session_timing"])

        asyncio.run(scenario())
        assert during == [None]
        assert entry_data["last_session_timing"]["success"] is True

    def test_the_previous_trace_is_cleared_on_entry(self, plain_report):
        """파서에 닿기 전에 죽은 세션이 이전 세션의 단계를 제 것처럼 내면 안 된다."""
        entry_data = _entry_data()
        entry_data["data"].last_session_trace = {"connect_s": 9.9}

        async def scenario():
            with pytest.raises(RuntimeError):
                async with session_handoff.omron_poll_ble_telemetry(None, entry_data, "pairing"):
                    raise RuntimeError("no device")

        asyncio.run(scenario())
        assert "connect_s" not in entry_data["last_session_timing"]
        assert entry_data["last_session_timing"]["operation"] == "pairing"

    def test_an_outer_cancellation_is_not_a_failure(self, plain_report):
        entry_data = _entry_data()

        async def scenario():
            with pytest.raises(asyncio.CancelledError):
                async with session_handoff.omron_poll_ble_telemetry(None, entry_data, "poll"):
                    raise asyncio.CancelledError()

        asyncio.run(scenario())
        assert entry_data["last_session_timing"] is None
        assert entry_data["failure_coordinator"].values == []
        assert entry_data["failure_count_coordinator"].values == []
        assert entry_data["connection_coordinator"].values[-1] is False


# ── async_poll records its trace ────────────────────────────────────────────
#
# 파서 인스턴스는 conftest 의 MagicMock 베이스 때문에 만들 수 없어, 다른
# 테스트들처럼 async_poll 본문을 AST 로 꺼내 가짜 self 위에서 돌린다.


def _async_poll():
    from test_bp5465_reliability_behavior import _extract_async_function, PARSER_PATH
    from custom_components.omron.omron_ble import parser as parser_module

    fn, _ = _extract_async_function(PARSER_PATH, "async_poll", parser_module.__dict__)
    return fn


class _ParserSelf:
    def __init__(self, session):
        self._poll_guard = asyncio.Lock()
        self._events_updates = SimpleNamespace(clear=lambda: None)
        self._device_model = "HEM-7155T"
        self._device_config = session.config
        self.last_service_info = None
        self.last_session_trace = None
        self._session = session

    def _open_session(self, ble_device, pairing_session=False):
        return self._session

    def _record_session_trace(self, session, trace):
        if session is not None:
            session.publish_link_to(trace)
        self.last_session_trace = trace


class TestPollTrace:
    def test_a_connect_failure_is_recorded_as_the_connect_stage(self, monkeypatch):
        from custom_components.omron.omron_ble import session as session_module
        from custom_components.omron.omron_ble.devices import get_device_config
        from custom_components.omron.omron_ble.session import OmronDeviceSession

        async def _refuse(ble_device, name, **kwargs):
            raise ConnectionError("dropped during the post-connect settle on all 3 attempt(s)")

        monkeypatch.setattr(session_module, "establish_connection_with_bond_settle", _refuse)
        monkeypatch.setattr(session_module, "is_local_adapter", lambda device: False)
        ble_device = SimpleNamespace(address=ADDRESS, details={"source": "AA:BB"})
        session = OmronDeviceSession(ble_device, get_device_config("HEM-7155T"))
        target = _ParserSelf(session)

        with pytest.raises(ConnectionError):
            asyncio.run(_async_poll()(target, ble_device))

        trace = target.last_session_trace
        assert trace.failed_stage == "connect"
        assert "connect" in trace.timings
        assert "adopted_link" not in trace.facts

    def test_a_connect_that_fails_every_attempt_still_names_the_radio(self, monkeypatch):
        """settle 드롭으로 세 번 다 실패해도 어느 라디오를 몇 번 시도했는지는 남아야 한다."""
        from custom_components.omron.omron_ble import connection as connection_module
        from custom_components.omron.omron_ble.devices import get_device_config
        from custom_components.omron.omron_ble.session import OmronDeviceSession

        class _Client:
            is_connected = False

            async def disconnect(self):
                pass

        async def _connect(cls, ble_device, name, **kwargs):
            return _Client()  # 연결 직후 끊긴 링크

        async def _no_sleep(_):
            pass

        monkeypatch.setattr(connection_module, "establish_connection", _connect)
        monkeypatch.setattr(connection_module.asyncio, "sleep", _no_sleep)
        monkeypatch.setattr(connection_module, "is_local_adapter", lambda device: False)
        ble_device = SimpleNamespace(address=ADDRESS, details={"source": "AA:BB"})
        session = OmronDeviceSession(ble_device, get_device_config("HEM-7155T"))

        with pytest.raises(ConnectionError, match="settle") as caught:
            asyncio.run(session.connect())

        assert session.link_info == {"source": "AA:BB", "connect_attempts": 3}
        assert session.trace.failed_stage == "connect"
        assert session.trace.failure(caught.value) == ("connect", "settle")
        assert session.trace.facts["connect_attempts"] == 3
        assert session.trace.link is not None and session.trace.link.source == "AA:BB"

    def test_unknown_advertising_source_is_not_stored_on_the_trace(self, monkeypatch):
        from custom_components.omron.omron_ble import connection as connection_module
        from custom_components.omron.omron_ble.devices import get_device_config
        from custom_components.omron.omron_ble.session import OmronDeviceSession

        class _Client:
            is_connected = True

        async def _connect(cls, ble_device, name, **kwargs):
            return _Client()

        monkeypatch.setattr(connection_module, "establish_connection", _connect)
        monkeypatch.setattr(connection_module, "_connection_source", lambda _d: "unknown")
        monkeypatch.setattr(connection_module, "is_local_adapter", lambda device: False)
        ble_device = SimpleNamespace(address=ADDRESS, details={})
        session = OmronDeviceSession(ble_device, get_device_config("HEM-7155T"))
        asyncio.run(session.connect())
        assert session.trace.link is not None
        assert session.trace.link.source is None

    def test_a_missing_parent_service_is_the_services_stage(self, monkeypatch):
        """verify_parent_service 는 False 를 돌려주고 raise 는 호출자가 한다 — 그래도 services 다."""
        from custom_components.omron.omron_ble.devices import get_device_config
        from custom_components.omron.omron_ble.session import OmronDeviceSession

        session = OmronDeviceSession(SimpleNamespace(address=ADDRESS), get_device_config("HEM-7155T"))
        session._client = SimpleNamespace(is_connected=True, services=[])

        async def _no_service():
            return False

        async def _aclose():
            session._client = None

        monkeypatch.setattr(session, "verify_parent_service", _no_service)
        monkeypatch.setattr(session, "aclose", _aclose)
        target = _ParserSelf(session)

        with pytest.raises(ConnectionError, match="Required service"):
            asyncio.run(_async_poll()(target, SimpleNamespace(address=ADDRESS), preconnected_session=session))

        assert target.last_session_trace.failed_stage == "services"
        cause = session_report.likely_cause(
            "session", "services", "Required service x not found", {}, "poll"
        )
        assert "model" in cause

    def test_an_adopted_link_is_marked_and_keeps_its_link_facts(self, monkeypatch):
        """핸드오프된 세션은 페어링 세션의 trace 를 폴의 것으로 바꾸되 링크 정보는 남긴다."""
        from custom_components.omron.omron_ble.devices import get_device_config
        from custom_components.omron.omron_ble.session import OmronDeviceSession

        session = OmronDeviceSession(SimpleNamespace(address=ADDRESS), get_device_config("HEM-7155T"))
        session._client = SimpleNamespace(is_connected=True, services=[])
        session.trace.link = LinkInfo(via="AA:BB", source="AA:BB")
        session.trace.note(connect_attempts=2)
        with session.trace.timed("pair"):
            pass

        async def _no_service():
            return False

        async def _aclose():
            session._client = None

        monkeypatch.setattr(session, "verify_parent_service", _no_service)
        monkeypatch.setattr(session, "aclose", _aclose)
        target = _ParserSelf(session)

        with pytest.raises(ConnectionError, match="Required service"):
            asyncio.run(
                _async_poll()(target, SimpleNamespace(address=ADDRESS), preconnected_session=session)
            )

        trace = target.last_session_trace
        assert trace.facts["adopted_link"] is True
        assert trace.failed_stage == "services"
        assert trace.link is not None and trace.link.via == "AA:BB"
        assert trace.facts["connect_attempts"] == 2
        assert "pair" not in trace.timings, "페어링 세션의 단계가 폴의 것으로 나오면 안 된다"
