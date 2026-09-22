"""Advertisement flag sensors update via PassiveBluetooth, not the poll coordinator.

Data Pending / Pairing Mode / Time Sync Required are MSD advertisement
flags. They used to ride ``poll_coordinator``, so after a Home Assistant
restart they stayed unavailable until a GATT poll succeeded. The ble-esl
pattern — ``PassiveBluetoothDataProcessor`` plus a seed at parser init —
is what this contract locks in.
"""
from __future__ import annotations

import ast
from pathlib import Path

_COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "omron"


def _parse(relative_path: str) -> ast.Module:
    return ast.parse((_COMPONENT / relative_path).read_text(encoding="utf-8"))


def _find_function(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


class TestAdvertisementBinarySensors:
    def test_parser_seeds_advertisement_flags(self):
        tree = _parse("omron_ble/parser.py")
        seed = _find_function(tree, "_seed_advertisement_binary_sensors")
        body = ast.unparse(seed)
        for key in ("forced_transfer", "invalid_time", "pairing_mode"):
            assert key in body, f"seed must publish {key}"
        assert "Data Pending" in body
        assert "Pairing Mode" in body

    def test_binary_platform_registers_passive_processor(self):
        source = (_COMPONENT / "binary_sensor.py").read_text(encoding="utf-8")
        assert "OmronPassiveBluetoothDataProcessor" in source
        assert "async_register_processor" in source
        assert "OmronAdvertisementBinarySensorEntity" in source
        assert "ADVERTISEMENT_BINARY_DEVICE_CLASSES" in source

    def test_poll_path_skips_advertisement_classes(self):
        tree = _parse("binary_sensor.py")
        fn = _find_function(tree, "_poll_binary_description_for_update")
        body = ast.unparse(fn)
        assert "ADVERTISEMENT_BINARY_DEVICE_CLASSES" in body

    def test_setup_pushes_seeded_update_to_passive_coordinator(self):
        source = (_COMPONENT / "__init__.py").read_text(encoding="utf-8")
        assert "async_last_service_info" in source
        assert "async_set_updated_data" in source
        assert "_finish_update()" in source

    def test_rssi_also_uses_passive_processor(self):
        source = (_COMPONENT / "sensor.py").read_text(encoding="utf-8")
        assert "ADVERTISEMENT_SENSOR_DESCRIPTIONS" in source
        assert "OmronAdvertisementSensorEntity" in source
        assert "async_register_processor" in source
