# Contributing

## Structure and Guidelines

- **Home Assistant Core alignment**: Integration code lives in `custom_components/omron/` and tests live in `tests/components/omron/` ([Home Assistant development rules](https://github.com/eigger/hass-repo-standards/blob/v1.0.0/docs/home-assistant.md)).
- **Testing**: Integration tests load the real Home Assistant runtime via `pytest-homeassistant-custom-component` (`hass` fixture, `MockConfigEntry`, config entry setup/unload, entity/device registry, and service actions), mocking only the external BLE device/GATT boundary ([Testing rules](https://github.com/eigger/hass-repo-standards/blob/v1.0.0/docs/testing.md)). Pure protocol/parser unit tests run in the same `tests/components/omron/` suite alongside the HA integration tests.
- **Dependencies**: Runtime dependencies belong in `custom_components/omron/manifest.json` (`requirements` / `dependencies`), while test and lint dependencies are pinned in `requirements_test.txt`, `requirements_test_min.txt`, `constraints/homeassistant-min.txt`, and `requirements_lint.txt` ([Dependency rules](https://github.com/eigger/hass-repo-standards/blob/v1.0.0/docs/dependencies.md)).

## Development setup

```bash
python3 -m venv .venv && . .venv/bin/activate   # Python 3.14
pip install -r requirements_test.txt -r requirements_lint.txt
pip install $(python -c "import json; print(' '.join(json.load(open('custom_components/omron/manifest.json'))['requirements']))")
pip install $(python -c "import json,pathlib,homeassistant.components as c; d=pathlib.Path(c.__file__).parent; print(' '.join(r for n in ('bluetooth','bluetooth_adapters','usb') for r in json.load(open(d/n/'manifest.json')).get('requirements',[])))")
pip check
```

## Checks

```bash
python -m ruff check . && python -m ruff format --check .
python -m pytest tests/components/omron/ -v --timeout 120
```

## Verified environments

| Environment | Python | Home Assistant | pytest-homeassistant-custom-component | `blesession` | Scope / Unverified |
|---|---|---|---|---|---|
| Local & CI `current` (`current` mode) | 3.14 | 2026.10.2 | 0.13.373 | latest compatible (`>=0.9.0`) | No physical BLE hardware in CI (external BLE transport mocked) |
| CI `current` (`minimum` mode) | 3.14 | 2026.10.2 | 0.13.373 | `0.9.0` (exact floor) | No physical BLE hardware in CI |
| CI `minimum-ha` (`hacs.json` minimum) | 3.13 | 2025.12.0 | 0.13.298 | `>=0.9.0` | Verified in GitHub Actions CI (`constraints/homeassistant-min.txt`) |

## Standards

This repository follows [hass-repo-standards](https://github.com/eigger/hass-repo-standards); see [STANDARDS.md](STANDARDS.md) and [ADOPTION_CHECKLIST.md](ADOPTION_CHECKLIST.md). Report bugs via [Issues](https://github.com/eigger/hass-omron/issues) and ask questions in [Discussions](https://github.com/eigger/hass-omron/discussions).
