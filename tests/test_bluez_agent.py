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
import sys
import types
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


def test_bluez_pairing_agent_graceful_fallback_on_error(monkeypatch):
    """에이전트 임포트/설정 중 예외 발생 시 크래시 없이 None 으로 안전하게 fallback 해야 한다."""
    from custom_components.omron.omron_ble import omron_driver

    # 1. 기본 환경(D-Bus 미연결 또는 미설치)에서 None fallback 검증
    async def _test_base():
        async with omron_driver._bluez_pairing_agent() as bus:
            assert bus is None

    asyncio.run(_test_base())

    # 2. 에이전트 생성/등록 시 임의의 예외(TypeError 등) 발생 시뮬레이션
    fake_module = types.ModuleType("custom_components.omron.omron_ble.bluez_agent")

    class _BrokenAgent:
        def __init__(self, *args, **kwargs):
            raise TypeError("Argument 'signature' has incorrect type")

    fake_module.AutoConfirmAgent = _BrokenAgent

    monkeypatch.setitem(
        sys.modules,
        "custom_components.omron.omron_ble.bluez_agent",
        fake_module,
    )

    async def _test_exception():
        async with omron_driver._bluez_pairing_agent() as bus:
            assert bus is None

    asyncio.run(_test_exception())
