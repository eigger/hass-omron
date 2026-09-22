"""Print the pinned requirements of Home Assistant components the tests load.

pytest-homeassistant-custom-component installs Home Assistant core but not the
requirements of individual components. Reading them from the installed HA keeps
the pins in step with whatever HA version the test package brings.

    python scripts/ha_component_requirements.py bluetooth usb
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import homeassistant.components

components_dir = Path(homeassistant.components.__file__).parent
requirements: set[str] = set()
for name in sys.argv[1:]:
    manifest = json.loads((components_dir / name / "manifest.json").read_text())
    requirements.update(manifest.get("requirements", []))
print("\n".join(sorted(requirements)))
