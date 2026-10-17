"""Regression coverage for BLE work tied to Home Assistant entry lifetimes."""

import ast
from pathlib import Path
from types import SimpleNamespace

from custom_components.omron import _session_lock_for

_COMPONENT = Path(__file__).resolve().parents[3] / "custom_components" / "omron"


def _function(path: str, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse((_COMPONENT / path).read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


def test_session_lock_is_shared_across_runtime_recreation():
    hass = SimpleNamespace(data={})
    first = _session_lock_for(hass, "AA:BB:CC:DD:EE:FF")
    second = _session_lock_for(hass, "AA:BB:CC:DD:EE:FF")
    other = _session_lock_for(hass, "AA:BB:CC:DD:EE:00")

    assert first is second
    assert first is not other


def test_initial_poll_is_scheduled_after_platform_setup_without_blocking_setup():
    setup = next(
        node
        for node in ast.parse(
            (_COMPONENT / "__init__.py").read_text(encoding="utf-8")
        ).body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
    )
    listener_pos = forward_pos = schedule_pos = None
    for node in ast.walk(setup):
        if not isinstance(node, ast.Call):
            continue
        call = ast.unparse(node.func)
        if call == "entry.add_update_listener":
            listener_pos = node.lineno
        elif call == "hass.config_entries.async_forward_entry_setups":
            forward_pos = node.lineno
        elif call == "_create_entry_background_task" and any(
            "_run_initial_poll" in ast.unparse(arg) for arg in node.args
        ):
            schedule_pos = node.lineno

    assert listener_pos is not None
    assert forward_pos is not None
    assert schedule_pos is not None
    assert listener_pos < schedule_pos
    assert forward_pos < schedule_pos
    assert not any(
        isinstance(node, ast.Await)
        and "poll_coordinator.async_refresh" in ast.unparse(node)
        for node in ast.walk(setup)
    )

    initial_poll = _function("__init__.py", "_run_initial_poll")
    assert any(
        isinstance(node, ast.Await)
        and "poll_coordinator.async_refresh" in ast.unparse(node)
        for node in ast.walk(initial_poll)
    )
