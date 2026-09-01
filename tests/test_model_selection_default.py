"""The model dropdown must not answer itself (issue #45).

Every profile in the catalog is a real EEPROM layout, so a pre-filled default
is indistinguishable from a successful probe: the form looks answered, the
user confirms, and the cuff is read through another model's memory map
forever. In #45 a HEM-7196T1 ran as the HEM-7142T2 fallback -- half the record
size, half the users, no error anywhere. When nothing identifies the device
the field has to be left empty so the choice is deliberate.

conftest replaces homeassistant/voluptuous with MagicMock, so the flow cannot
be instantiated; the schema construction is checked with the AST instead
(same constraint as test_pairing_session_handoff.py).
"""

import ast
from pathlib import Path

from custom_components.omron.omron_ble.devices import infer_model_id_from_local_name

_CONFIG_FLOW = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "omron"
    / "config_flow.py"
)


def _select_model_step() -> ast.AsyncFunctionDef:
    tree = ast.parse(_CONFIG_FLOW.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "async_step_select_model"
        ):
            return node
    raise AssertionError("async_step_select_model not found — was it renamed?")


def test_the_form_never_falls_back_to_the_default_model() -> None:
    step = _select_model_step()
    names = {n.id for n in ast.walk(step) if isinstance(n, ast.Name)}
    assert "DEFAULT_DEVICE_MODEL" not in names, (
        "the model dropdown was given a fallback default again; an unidentified "
        "device must leave the field empty (#45)"
    )


def test_the_dropdown_is_left_empty_when_nothing_identified_the_device() -> None:
    step = _select_model_step()
    branches = [n for n in ast.walk(step) if isinstance(n, ast.IfExp)]
    for branch in branches:
        if not isinstance(branch.orelse, ast.Call):
            continue
        keywords = {kw.arg for kw in branch.orelse.keywords}
        args = [a for a in branch.orelse.args if isinstance(a, ast.Name)]
        if any(a.id == "CONF_DEVICE_MODEL" for a in args):
            assert "default" not in keywords, (
                "the no-inference branch still supplies a default"
            )
            return
    raise AssertionError(
        "no branch leaves CONF_DEVICE_MODEL without a default — an unidentified "
        "device would get a pre-selected model (#45)"
    )


def test_a_failed_identification_is_warned_about() -> None:
    step = _select_model_step()
    warned = any(
        isinstance(n, ast.Attribute)
        and n.attr == "warning"
        and isinstance(n.value, ast.Name)
        and n.value.id == "_LOGGER"
        for n in ast.walk(step)
    )
    assert warned, "an unidentifiable device must leave a log line to diagnose"


def test_catalog_names_still_resolve() -> None:
    assert infer_model_id_from_local_name("HEM-7386T1") == "HEM-7386T1"
    assert infer_model_id_from_local_name("Omron HEM-7382T1-AZAZ") == "HEM-7382T1-AZAZ"


def test_model_number_aliases_resolve_to_a_profile() -> None:
    # The Model Number String is read off the device during setup and is where
    # a carton code shows up; without the alias table these fall through to
    # "unidentified" even though the catalog covers them.
    assert infer_model_id_from_local_name("BP7360") == "HEM-7376T1-Z"
    assert infer_model_id_from_local_name("HEM-7140T1") == "HEM-7140T1-AP"


def test_unknown_names_stay_unknown() -> None:
    for value in ("BLESmart_0000123", "", "Living Room"):
        assert infer_model_id_from_local_name(value) is None


def _config_flow_class() -> ast.ClassDef:
    tree = ast.parse(_CONFIG_FLOW.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "OmronConfigFlow":
            return node
    raise AssertionError("OmronConfigFlow not found — was it renamed?")


def test_no_step_substitutes_a_default_for_the_chosen_model() -> None:
    # The steps after select_model (user aliases, pairing, the pairing call
    # itself) used to read `self._selected_model or DEFAULT_DEVICE_MODEL`,
    # which would have paired and configured the device on the wrong profile
    # without a word. They must fail loudly instead.
    flow = _config_flow_class()
    names = {n.id for n in ast.walk(flow) if isinstance(n, ast.Name)}
    assert "DEFAULT_DEVICE_MODEL" not in names, (
        "a config flow step fell back to the default model again (#45)"
    )
