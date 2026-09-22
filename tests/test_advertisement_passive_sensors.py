"""Advertisement flag sensors update via PassiveBluetooth, not the poll coordinator.

Data Pending / Pairing Mode / Time Sync Required are MSD advertisement
flags. A seed of ``False`` must not be published: it would overwrite a
restored ``on`` and would show a cuff that has never advertised as off.
RSSI follows processor availability so a stale dBm is not recorded.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

_COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "omron"


def _parse(relative_path: str) -> ast.Module:
    return ast.parse((_COMPONENT / relative_path).read_text(encoding="utf-8"))


def _find_function(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


class TestAdvertisementBinarySensors:
    def test_parser_seeds_advertisement_flags_as_unknown(self):
        tree = _parse("omron_ble/parser.py")
        seed = _find_function(tree, "_seed_advertisement_binary_sensors")
        calls = [
            node
            for node in ast.walk(seed)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update_binary_sensor"
        ]
        assert len(calls) == 3
        published = {ast.unparse(call.args[0]): ast.unparse(call.args[1]) for call in calls}
        assert published == {
            "'forced_transfer'": "None",
            "'invalid_time'": "None",
            "'pairing_mode'": "None",
        }

    def test_binary_available_requires_a_real_bool(self):
        tree = _parse("binary_sensor.py")
        entity = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "OmronAdvertisementBinarySensorEntity"
        )
        available = next(node for node in entity.body if isinstance(node, ast.FunctionDef) and node.name == "available")
        body = ast.unparse(available)
        assert "isinstance" in body
        assert "bool" in body

    def test_rssi_does_not_hold_availability_while_asleep(self):
        tree = _parse("sensor.py")
        entity = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "OmronAdvertisementSensorEntity"
        )
        methods = {
            node.name
            for node in entity.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert "available" not in methods

    def test_rssi_converter_keeps_int(self):
        tree = _parse("sensor.py")
        fn = _find_function(tree, "advertisement_sensor_update_to_bluetooth_data_update")
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "float"
            for node in ast.walk(fn)
        )

    def test_setup_primes_nonconnectable_and_does_not_push_seed_alone(self):
        tree = _parse("__init__.py")
        setup = _find_function(tree, "async_setup_entry")
        last_info_calls = [
            node
            for node in ast.walk(setup)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "async_last_service_info"
        ]
        assert len(last_info_calls) == 1
        keywords = {kw.arg: ast.unparse(kw.value) for kw in last_info_calls[0].keywords}
        assert keywords["connectable"] == "False"

        pushes = [
            node
            for node in ast.walk(setup)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "async_set_updated_data"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "bt_coordinator"
        ]
        assert len(pushes) == 1
        guarded = False
        for node in ast.walk(setup):
            if (
                isinstance(node, ast.If)
                and ast.unparse(node.test) == "last_service_info is not None"
                and any(child is pushes[0] for child in ast.walk(node))
            ):
                guarded = True
        assert guarded, "cached-advertisement push must be inside `if last_service_info is not None`"

    def test_preserved_unique_id_matches_the_poll_entity_formula(self):
        """device_id is None for MSD flags and RSSI, so the slug is none_<key>."""
        entity_key = SimpleNamespace(device_id=None, key="pairing_mode")
        tree = _parse("entity_helpers.py")
        fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "preserved_passive_unique_id"
        )
        namespace: dict = {}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "entity_helpers.py", "exec"), namespace)
        preserved_passive_unique_id = namespace["preserved_passive_unique_id"]
        assert (
            preserved_passive_unique_id(
                model="HEM-7155T",
                address="AA:BB:CC:DD:EE:FF",
                entity_key=entity_key,
            )
            == "hem_7155t_eeff_none_pairing_mode"
        )
        rssi_key = SimpleNamespace(device_id=None, key="signal_strength")
        assert (
            preserved_passive_unique_id(
                model="HEM-7155T",
                address="AA:BB:CC:DD:EE:FF",
                entity_key=rssi_key,
            )
            == "hem_7155t_eeff_none_signal_strength"
        )
