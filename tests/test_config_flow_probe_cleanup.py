"""Config flow probe links must have a single, explicit owner."""
import ast
from pathlib import Path

_FLOW = Path(__file__).resolve().parent.parent / "custom_components" / "omron" / "config_flow.py"


def _method(name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    tree = ast.parse(_FLOW.read_text(encoding="utf-8"))
    config_flow = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OmronConfigFlow"
    )
    return next(
        node
        for node in config_flow.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == name
    )


def test_removing_a_flow_discards_its_probe_session():
    method = ast.unparse(_method("async_remove"))
    assert "discard_probe_session" in method
    assert "async_create_task" in method
    assert "_discovery_info.address" in method


def test_device_selection_does_not_bypass_another_flow_in_progress():
    method = ast.unparse(_method("async_step_user"))
    assert "async_set_unique_id(address)" in method
    assert "raise_on_progress=False" not in method
