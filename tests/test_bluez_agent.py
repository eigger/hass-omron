"""BlueZ 페어링 에이전트 회귀 테스트.

dbus_fast 는 런타임 어노테이션에서 D-Bus 시그니처를 만든다.
bluez_agent.py 에 ``from __future__ import annotations`` 가 포함되면
PEP 563 으로 인해 어노테이션이 문자열화되어
``TypeError: Argument 'signature' has incorrect type (expected str, got NoneType)``
크래시가 발생하므로, 모듈 분리 및 어노테이션 보존 불변조건을 검증한다.
"""

import ast
import asyncio
import logging
from pathlib import Path
import sys
import pytest


def test_bluez_agent_interface_builds():
    """dbus_fast 는 어노테이션에서 시그니처를 만든다 — 이 모듈에
    from __future__ import annotations 가 들어가면 임포트/클래스 생성 시점에 깨진다."""
    pytest.importorskip("dbus_fast")
    from custom_components.omron.omron_ble.bluez_agent import AutoConfirmAgent

    agent = AutoConfirmAgent()
    assert agent is not None
    assert agent.name == "org.bluez.Agent1"

    node = agent.introspect()
    method_names = {method.name for method in node.methods}
    expected_methods = {
        "Release",
        "RequestPinCode",
        "DisplayPinCode",
        "RequestPasskey",
        "DisplayPasskey",
        "RequestConfirmation",
        "RequestAuthorization",
        "AuthorizeService",
        "Cancel",
    }
    assert expected_methods.issubset(method_names)


def test_bluez_agent_no_future_annotations():
    """bluez_agent.py 에는 from __future__ import annotations 가 포함되지 않아야 한다."""
    agent_file = (
        Path(__file__).resolve().parent.parent
        / "custom_components"
        / "omron"
        / "omron_ble"
        / "bluez_agent.py"
    )
    tree = ast.parse(agent_file.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            for alias in node.names:
                assert alias.name != "annotations", (
                    "bluez_agent.py 에 from __future__ import annotations 가 포함되면 안 됩니다. "
                    "dbus_fast 의 런타임 시그니처 분석이 깨집니다."
                )


def test_bluez_pairing_agent_falls_back_when_agent_module_broken(monkeypatch, caplog):
    """새 except Exception(임포트 실패) 경로를 검증한다 — 기존 '시스템 버스 없음' 경로와 구분."""
    from custom_components.omron.omron_ble import omron_driver

    # sys.modules 에 None 을 넣으면 from .bluez_agent import ... 가 ImportError 를 낸다
    monkeypatch.setitem(
        sys.modules, "custom_components.omron.omron_ble.bluez_agent", None
    )

    async def _run():
        with caplog.at_level(logging.DEBUG, logger=omron_driver.__name__):
            async with omron_driver._bluez_pairing_agent() as bus:
                assert bus is None
        assert "BlueZ agent unavailable" in caplog.text

    asyncio.run(_run())


def test_bluez_pairing_agent_falls_back_on_system_bus_failure(monkeypatch, caplog):
    """시스템 버스 연결 실패 시 기존의 'cannot connect to system bus' 경로를 검증한다."""
    pytest.importorskip("dbus_fast")
    from custom_components.omron.omron_ble import omron_driver
    from dbus_fast.aio.message_bus import MessageBus

    async def _failing_connect(*args, **kwargs):
        raise ConnectionRefusedError("Simulated system bus failure")

    monkeypatch.setattr(MessageBus, "connect", _failing_connect)

    async def _run():
        with caplog.at_level(logging.DEBUG, logger=omron_driver.__name__):
            async with omron_driver._bluez_pairing_agent() as bus:
                assert bus is None
        assert "cannot connect to system bus" in caplog.text

    asyncio.run(_run())


def test_bluez_pairing_agent_propagates_caller_exception(monkeypatch):
    """``async with`` 블록 본문(예: connect 타임아웃)에서 발생한 예외가 그대로
    전파돼야 한다. 예전에는 등록 try/except 가 이 예외까지 삼켜서
    except 블록 안에서 ``yield None`` 을 다시 실행했고, 제너레이터는 athrow() 를
    두 번 받을 수 없어 ``RuntimeError: generator didn't stop after athrow()`` 로
    원인이 뒤바뀌었다. establish_connection_with_bond_settle 의 BleakError 폴백은
    이 RuntimeError 를 잡지 못해 pair=False 재시도가 동작하지 않았다."""
    pytest.importorskip("dbus_fast")
    from custom_components.omron.omron_ble import omron_driver
    from dbus_fast.aio.message_bus import MessageBus

    class FakeBus:
        def __init__(self):
            self.calls: list[str] = []
            self.disconnected = False

        def export(self, path, agent):
            pass

        async def call(self, message):
            self.calls.append(message.member)
            return None

        def disconnect(self):
            self.disconnected = True

    fake_bus = FakeBus()

    async def _fake_connect(*args, **kwargs):
        return fake_bus

    monkeypatch.setattr(MessageBus, "connect", _fake_connect)

    class SimulatedConnectTimeout(Exception):
        pass

    async def _run():
        with pytest.raises(SimulatedConnectTimeout):
            async with omron_driver._bluez_pairing_agent() as bus:
                assert bus is fake_bus
                raise SimulatedConnectTimeout("simulated establish_connection failure")

    asyncio.run(_run())

    # Registration still happened and cleanup still ran despite the exception.
    assert fake_bus.calls == ["RegisterAgent", "RequestDefaultAgent", "UnregisterAgent"]
    assert fake_bus.disconnected
