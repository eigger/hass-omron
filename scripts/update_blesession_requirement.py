"""Raise the blesession minimum requirement to the latest stable PyPI release.

The GitHub Actions workflow uses this script before opening a dependency PR.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from urllib.request import urlopen

from packaging.requirements import Requirement
from packaging.version import Version


MANIFEST = Path("custom_components/omron/manifest.json")
PYPI_URL = "https://pypi.org/pypi/blesession/json"


def latest_stable_version() -> Version:
    with urlopen(PYPI_URL, timeout=30) as response:
        data = json.load(response)

    stable_versions = []
    for raw_version, files in data["releases"].items():
        version = Version(raw_version)
        if version.is_prerelease or version.is_devrelease:
            continue
        if any(not file.get("yanked", False) for file in files):
            stable_versions.append(version)

    if not stable_versions:
        raise RuntimeError("PyPI returned no non-yanked stable blesession releases")
    return max(stable_versions)


def update_requirement(latest: Version) -> tuple[bool, str]:
    text = MANIFEST.read_text(encoding="utf-8")
    manifest = json.loads(text)
    matches = [
        item
        for item in manifest.get("requirements", [])
        if Requirement(item).name.lower() == "blesession"
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one blesession requirement, found {len(matches)}")

    current = matches[0]
    requirement = Requirement(current)
    lower_bounds = [specifier for specifier in requirement.specifier if specifier.operator == ">="]
    if (
        len(lower_bounds) != 1
        or len(requirement.specifier) != 1
        or requirement.extras
        or requirement.marker
        or requirement.url
    ):
        raise RuntimeError(f"Expected a single plain >= lower bound in {current!r}")

    current_version = Version(lower_bounds[0].version)
    if latest <= current_version:
        return False, str(latest)

    updated = f"blesession>={latest}"
    manifest["requirements"] = [
        updated if item == current else item for item in manifest["requirements"]
    ]
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return True, str(latest)


def main() -> int:
    latest = latest_stable_version()
    updated, latest_string = update_requirement(latest)
    print(f"blesession latest stable: {latest_string}; updated: {updated}")

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as output:
            output.write(f"updated={'true' if updated else 'false'}\n")
            output.write(f"version={latest_string}\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"::error::{error}", file=sys.stderr)
        raise SystemExit(1) from error
