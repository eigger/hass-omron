"""BlueZ 페어링 에이전트 회귀 테스트.

dbus_fast 는 런타임 어노테이션에서 D-Bus 시그니처를 만든다.
bluez_agent.py 에 ``from __future__ import annotations`` 가 포함되면
PEP 563 으로 인해 어노테이션이 문자열화되어
``TypeError: Argument 'signature' has incorrect type (expected str, got NoneType)``
크래시가 발생하므로, 모듈 분리 및 어노테이션 보존 불변조건을 검증한다.
"""

import ast
import asyncio
from pathlib import Path
import pytest


def test_bluez_agent_interface_builds():
    """dbus_fast 는 어노테이션에서 시그니처를 만든다 — 이 모듈에
    from __future__ import annotations 가 들어가면 임포트/클래스 생성 시점에 깨진다."""
    pytest.importorskip("dbus_fast")
    from custom_components.omron.omron_ble.bluez_agent import AutoConfirmAgent

    agent = AutoConfirmAgent()
    assert agent is not None
    assert agent.name == "org.bluez.Agent1"


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


def test_bluez_pairing_agent_graceful_fallback_on_agent_init_error(monkeypatch):
    """에이전트 생성 실패 시 None 으로 fallback 해야 한다."""
    pytest.importorskip("dbus_fast")
    from custom_components.omron.omron_ble import omron_driver
    from custom_components.omron.omron_ble.bluez_agent import AutoConfirmAgent

    def _raising_init(self):
        raise TypeError("Argument 'signature' has incorrect type")

    monkeypatch.setattr(AutoConfirmAgent, "__init__", _raising_init)

    async def _test():
        async with omron_driver._bluez_pairing_agent() as bus:
            assert bus is None

    asyncio.run(_test())


def test_bluez_pairing_agent_graceful_fallback_on_bus_error(monkeypatch):
    """시스템 버스 연결 실패 시 None 으로 fallback 해야 한다."""
    pytest.importorskip("dbus_fast")
    from custom_components.omron.omron_ble import omron_driver
    from dbus_fast.aio.message_bus import MessageBus

    async def _failing_connect(*args, **kwargs):
        raise ConnectionRefusedError("Cannot connect to system bus")

    monkeypatch.setattr(MessageBus, "connect", _failing_connect)

    async def _test():
        async with omron_driver._bluez_pairing_agent() as bus:
            assert bus is None

    asyncio.run(_test())
