"""omron_ble is the vendored library: verify at runtime it imports standalone without homeassistant, blesession.hass, or parent package."""

import importlib.abc
import pkgutil
import subprocess
import sys
from pathlib import Path

import custom_components.omron.omron_ble as omron_ble

OMRON_BLE_DIR = Path(omron_ble.__file__).resolve().parent
MODULE_NAMES = sorted(info.name for info in pkgutil.iter_modules([str(OMRON_BLE_DIR)]))


class _ForbiddenImportBlocker(importlib.abc.MetaPathFinder):
    """Raise ImportError if homeassistant or blesession.hass is imported."""

    def find_spec(self, fullname: str, path, target=None):
        if (
            fullname == "homeassistant"
            or fullname.startswith("homeassistant.")
            or fullname == "blesession.hass"
            or fullname.startswith("blesession.hass.")
        ):
            raise ImportError(f"Forbidden import inside omron_ble: {fullname}")
        return None


def test_omron_ble_imports_standalone_without_homeassistant_or_parent() -> None:
    """Import every omron_ble.* module as a top-level package with homeassistant blocked.

    Importing as top-level ``omron_ble.<name>`` makes any ``level > 1`` relative
    import fail with ``attempted relative import beyond top-level package``, and
    the meta_path blocker fails any ``homeassistant`` or ``blesession.hass``
    import at runtime.
    """
    code = f"""
import importlib
import importlib.abc
import sys

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if (
            fullname == "homeassistant"
            or fullname.startswith("homeassistant.")
            or fullname == "blesession.hass"
            or fullname.startswith("blesession.hass.")
        ):
            raise ImportError(f"Forbidden import: {{fullname}}")
        return None

sys.meta_path.insert(0, Blocker())
sys.path.insert(0, {str(OMRON_BLE_DIR.parent)!r})

for name in {MODULE_NAMES!r}:
    importlib.import_module(f"omron_ble.{{name}}")
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
