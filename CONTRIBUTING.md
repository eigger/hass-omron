# Contributing

## Development setup

```bash
python3 -m venv .venv && . .venv/bin/activate   # Python 3.14
pip install -r requirements_test.txt -r requirements_lint.txt
pip install $(python -c "import json; print(' '.join(json.load(open('custom_components/omron/manifest.json'))['requirements']))")
pip install $(python -c "import json,pathlib,homeassistant.components as c; d=pathlib.Path(c.__file__).parent; print(' '.join(r for n in ('bluetooth','bluetooth_adapters','usb') for r in json.load(open(d/n/'manifest.json')).get('requirements',[])))")
```

## Checks

```bash
python -m ruff check . && python -m ruff format --check .
python -m pytest tests/components/omron/ -v --timeout 120
```

Tests load the real Home Assistant through `pytest-homeassistant-custom-component`; only the Bluetooth/device boundary is mocked. CI uses the shared workflows from hass-repo-standards (`Tests`, `Validate`, `Update dependencies`).

## Verified environment

CI runs the `current` job on Python 3.14 (pytest-homeassistant-custom-component 0.13.366, Home Assistant 2026.9.3) in both current and minimum-`blesession` modes, and a separate `minimum-ha` job on Python 3.13 with Home Assistant 2025.12.0 (`hacs.json` minimum, plugin 0.13.298, constraints in `constraints/homeassistant-min.txt`).

## Standards

This repository follows [hass-repo-standards](https://github.com/eigger/hass-repo-standards); see [STANDARDS.md](STANDARDS.md) and [ADOPTION_CHECKLIST.md](ADOPTION_CHECKLIST.md). Report bugs via Issues and ask questions in Discussions.
