"""omron_ble is the vendored library: no homeassistant, blesession.hass, or parent imports."""
import ast
from pathlib import Path

import pytest

OMRON_BLE = Path(__file__).resolve().parent.parent / "custom_components" / "omron" / "omron_ble"


def _imports(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, 0
        elif isinstance(node, ast.ImportFrom):
            yield node.module or "", node.level


@pytest.mark.parametrize("path", sorted(OMRON_BLE.glob("*.py")), ids=lambda p: p.name)
def test_omron_ble_has_no_homeassistant_or_parent_imports(path: Path) -> None:
    offending = [
        (module, level)
        for module, level in _imports(path)
        if (
            module == "homeassistant"
            or module.startswith("homeassistant.")
            or module == "blesession.hass"
            or module.startswith("blesession.hass.")
            or level > 1
        )
    ]
    assert not offending, f"{path.name} reaches outside omron_ble: {offending}"
