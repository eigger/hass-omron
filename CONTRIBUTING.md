# Contributing

## Development setup

```bash
python3 -m venv .venv && . .venv/bin/activate   # Python 3.14 (see .github/workflows/tests.yml)
pip install -r requirements_test.txt
pip install $(python -c "import json; print(' '.join(json.load(open('custom_components/omron/manifest.json'))['requirements']))")
python scripts/ha_component_requirements.py bluetooth usb > requirements_ha.txt && pip install -r requirements_ha.txt
```

## Checks

```bash
python -m ruff check custom_components/
python -m pytest tests/ -v --timeout 120
```

Tests load the real Home Assistant through `pytest-homeassistant-custom-component`; only the Bluetooth/device boundary is mocked.

## Verified environment

CI runs Python 3.14 with `pytest-homeassistant-custom-component` 0.13.366 (Home Assistant 2026.9). The minimum supported Home Assistant (`hacs.json`, 2025.12.0) is **not** exercised by CI.

## Standards

This repository follows [hass-repo-standards](https://github.com/eigger/hass-repo-standards); see [STANDARDS.md](STANDARDS.md) and [ADOPTION_CHECKLIST.md](ADOPTION_CHECKLIST.md). Report bugs via Issues and ask questions in Discussions.
