"""The model step is driven through Home Assistant's flow manager.

An unidentified cuff must not arrive with a model already filled in. The
fallback profile is a real EEPROM map, so confirming it reads the cuff
through the wrong layout and reports no error (#45). The same form has
three step ids, and each description may only name a placeholder the
flow actually puts on the form.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from bleak.backends.device import BLEDevice
from homeassistant.config_entries import SOURCE_BLUETOOTH
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
import voluptuous as vol

import custom_components.omron.config_flow as config_flow
from custom_components.omron.const import CONF_DEVICE_MODEL, DOMAIN
from custom_components.omron.omron_ble.const import OMRON_MANUFACTURER_ID
from custom_components.omron.omron_ble.model_aliases import AMBIGUOUS_MODEL_NAMES

from bt import service_info

ADDRESS = "AA:BB:CC:DD:EE:11"
_COMPONENT = Path(config_flow.__file__).resolve().parent
_STRING_FILES = (
    _COMPONENT / "strings.json",
    _COMPONENT / "translations" / "en.json",
    _COMPONENT / "translations" / "ko.json",
)
# A carton name that covers two profiles. The probe reports it; nothing in
# the advertisement identifies which of the two it is.
_SHARED_NAME = "BP5350"
# Format 0x01, no status flags, one user slot of sequence bytes. Enough for
# ``supported()``; the name is what the model step reads.
_QUIET_MSD = bytes([0x01, 0x00, 0x00, 0x00])


def _description_tokens(step_id: str) -> set[str]:
    """Every ``{name}`` the three translation files use for this step."""
    tokens: set[str] = set()
    for path in _STRING_FILES:
        description = json.loads(path.read_text(encoding="utf-8"))["config"]["step"][
            step_id
        ]["description"]
        tokens.update(re.findall(r"\{(\w+)\}", description))
    return tokens


def _assert_supplied(result: dict, step_id: str) -> None:
    """Home Assistant raises when a description names a placeholder the flow omits."""
    supplied = result["description_placeholders"]
    missing = _description_tokens(step_id) - set(supplied)
    assert not missing, f"{step_id} omits {sorted(missing)}"


async def _open_model_step(
    hass: HomeAssistant,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    probe: str | None = None,
    step_id: str = "select_model",
):
    if probe is not None:
        # The model-number read is a GATT session. Stub it so the branch runs
        # without a cuff; the advertisement name itself identifies nothing.
        def _device(hass, address, *args, **kwargs):
            return BLEDevice(address=address, name=name, details={})

        async def _fetch(ble_device, *, keep_session_open: bool = False):
            return probe, None

        monkeypatch.setattr(config_flow, "async_ble_device_from_address", _device)
        monkeypatch.setattr(config_flow, "async_fetch_device_model_number", _fetch)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=service_info(
            ADDRESS,
            name=name,
            manufacturer_data={OMRON_MANUFACTURER_ID: _QUIET_MSD},
        ),
    )
    # An abort has no step_id. Reading it first would raise KeyError.
    assert result["type"] is FlowResultType.FORM
    if result["step_id"] == "bluetooth_confirm":
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )
        assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == step_id
    _assert_supplied(result, step_id)
    return result


def _field(schema: vol.Schema, key: str) -> vol.Marker:
    assert key in schema.schema
    return next(marker for marker in schema.schema if marker == key)


def _default(marker: vol.Marker):
    """Home Assistant wraps a provided default in a zero-arg factory."""
    value = marker.default
    return value() if callable(value) else value


async def test_an_unidentified_cuff_leaves_the_model_empty(
    hass: HomeAssistant,
    enable_bluetooth: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing in the name is a catalog id, and no radio is present to probe."""
    with caplog.at_level(logging.WARNING):
        result = await _open_model_step(hass, "BLESmart_0000123", monkeypatch)

    model = _field(result["data_schema"], CONF_DEVICE_MODEL)
    assert model.default is vol.UNDEFINED
    assert "has to be picked by hand" in caplog.text

    interval = _field(result["data_schema"], CONF_SCAN_INTERVAL)
    assert _default(interval) == 300


async def test_a_named_cuff_preselects_that_model(
    hass: HomeAssistant, enable_bluetooth: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await _open_model_step(hass, "HEM-7155T", monkeypatch)
    model = _field(result["data_schema"], CONF_DEVICE_MODEL)
    assert _default(model) == "HEM-7155T"


async def test_a_probe_outside_the_catalog_leaves_the_model_empty(
    hass: HomeAssistant,
    enable_bluetooth: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The cuff answered, and the answer is in no table."""
    with caplog.at_level(logging.WARNING):
        result = await _open_model_step(
            hass,
            "BLESmart_0000123",
            monkeypatch,
            probe="NOT-A-MODEL",
            step_id="select_model_unknown",
        )

    model = _field(result["data_schema"], CONF_DEVICE_MODEL)
    assert model.default is vol.UNDEFINED
    placeholders = result["description_placeholders"]
    assert placeholders["probed_model"] == "NOT-A-MODEL"
    assert placeholders["candidate_count"] == "0"
    assert placeholders["candidates"] == ""
    assert "NOT-A-MODEL" in caplog.text


async def test_a_shared_name_lists_the_models_it_covers(
    hass: HomeAssistant,
    enable_bluetooth: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Several profiles answer to one carton name, and they do not read alike."""
    with caplog.at_level(logging.WARNING):
        result = await _open_model_step(
            hass,
            "BLESmart_0000123",
            monkeypatch,
            probe=_SHARED_NAME,
            step_id="select_model_ambiguous",
        )

    model = _field(result["data_schema"], CONF_DEVICE_MODEL)
    assert model.default is vol.UNDEFINED
    placeholders = result["description_placeholders"]
    covered = AMBIGUOUS_MODEL_NAMES[_SHARED_NAME]
    assert placeholders["probed_model"] == _SHARED_NAME
    assert placeholders["candidate_count"] == str(len(covered))
    assert placeholders["candidates"] == "\n".join(f"- **{model_id}**" for model_id in covered)
    assert "do not read alike" in caplog.text
