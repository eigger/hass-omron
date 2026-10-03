"""Regression coverage for BLE work tied to Home Assistant entry lifetimes."""
import ast
from pathlib import Path
from types import SimpleNamespace

from custom_components.omron import _session_lock_for

_COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "omron"


def _function(path: str, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse((_COMPONENT / path).read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


def _has_timeout(function: ast.AST) -> bool:
    return any(
        isinstance(node, ast.AsyncWith)
        and any(
            isinstance(item.context_expr, ast.Call)
            and ast.unparse(item.context_expr.func)
            in {"asyncio.timeout", "asyncio.timeout_at"}
            for item in node.items
        )
        for node in ast.walk(function)
    )


def test_session_lock_is_shared_across_runtime_recreation():
    hass = SimpleNamespace(data={})
    first = _session_lock_for(hass, "AA:BB:CC:DD:EE:FF")
    second = _session_lock_for(hass, "AA:BB:CC:DD:EE:FF")
    other = _session_lock_for(hass, "AA:BB:CC:DD:EE:00")

    assert first is second
    assert first is not other


def test_advertisement_ble_session_has_a_deadline():
    assert _has_timeout(_function("__init__.py", "_run_advertisement_session"))


def test_pairing_button_ble_session_has_a_deadline():
    tree = ast.parse((_COMPONENT / "button.py").read_text(encoding="utf-8"))
    methods = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_async_retry_pairing"
    ]
    assert methods and _has_timeout(methods[0])


def test_pairing_button_operation_is_tracked_for_entry_unload():
    methods = [
        node
        for node in ast.walk(ast.parse((_COMPONENT / "button.py").read_text()))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_press"
    ]
    assert any(
        "runtime.background_tasks.add(task)" in ast.unparse(method)
        and "runtime.background_tasks.discard(task)" in ast.unparse(method)
        for method in methods
    )


def test_config_entry_owns_advertisement_background_tasks():
    tree = ast.parse((_COMPONENT / "__init__.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) == "_create_entry_background_task"
    ]
    assert len(calls) == 2
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_create_entry_background_task"
    )
    body = ast.unparse(helper)
    assert "runtime.background_tasks.add(task)" in body
    assert "task.add_done_callback(runtime.background_tasks.discard)" in body


def test_unload_cancels_and_waits_for_tasks_before_discarding_handoffs():
    method = _function("__init__.py", "async_unload_entry")
    lines = {
        ast.unparse(node.func): node.lineno
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
    }
    assert "asyncio.gather" in lines
    assert "discard_handoff_session" in lines
    assert lines["asyncio.gather"] < lines["discard_handoff_session"]


def test_credential_update_listener_is_registered_before_initial_poll():
    setup = next(
        node
        for node in ast.parse((_COMPONENT / "__init__.py").read_text(encoding="utf-8")).body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
    )
    listener_pos = refresh_pos = None
    for node in ast.walk(setup):
        if not isinstance(node, ast.Call):
            continue
        call = ast.unparse(node.func)
        if call == "entry.add_update_listener":
            listener_pos = node.lineno
        elif call == "poll_coordinator.async_refresh":
            refresh_pos = node.lineno

    assert listener_pos is not None and refresh_pos is not None
    assert listener_pos < refresh_pos
