"""The model dropdown must not answer itself (issue #45).

Every profile in the catalog is a real EEPROM layout, so a pre-filled default
is indistinguishable from a successful probe: the form looks answered, the
user confirms, and the cuff is read through another model's memory map
forever. In #45 a HEM-7196T1 ran as the HEM-7142T2 fallback -- half the record
size, half the users, no error anywhere. When nothing identifies the device
the field has to be left empty so the choice is deliberate.

The dropdown, including the unknown and shared-name steps, is exercised
through the flow manager (``test_config_flow.py``), which also checks that
each description's placeholders are ones that form supplies. What remains
here is the catalog, the translations, and the rule that no later step
substitutes the fallback model for a choice the user never made. That each
model step has a handler is covered by ``test_config_flow.py``, which opens
every step through the flow manager.
"""

import json
from pathlib import Path

import pytest
from homeassistant.config_entries import SOURCE_BLUETOOTH
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow, FlowResultType

from custom_components.omron.const import DOMAIN
from custom_components.omron.omron_ble.const import OMRON_MANUFACTURER_ID
from custom_components.omron.omron_ble.devices import infer_model_id_from_local_name

from tests.components.omron.bt import service_info

_CONFIG_FLOW = (
    Path(__file__).resolve().parents[3]
    / "custom_components"
    / "omron"
    / "config_flow.py"
)


def test_catalog_names_still_resolve() -> None:
    assert infer_model_id_from_local_name("HEM-7386T1") == "HEM-7386T1"
    assert infer_model_id_from_local_name("Omron HEM-7382T1-AZAZ") == "HEM-7382T1-AZAZ"


def test_model_number_aliases_resolve_to_a_profile() -> None:
    # The Model Number String is read off the device during setup and is where
    # a carton code shows up; without the alias table these fall through to
    # "unidentified" even though the catalog covers them.
    assert infer_model_id_from_local_name("BP7360") == "HEM-7376T1-Z"
    assert infer_model_id_from_local_name("HEM-7140T1") == "HEM-7140T1-AP"


def test_unknown_names_stay_unknown() -> None:
    for value in ("BLESmart_0000123", "", "Living Room"):
        assert infer_model_id_from_local_name(value) is None


async def test_no_step_substitutes_a_default_for_the_chosen_model(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    # The steps after select_model (user aliases, pairing, the pairing call
    # itself) used to read `self._selected_model or DEFAULT_DEVICE_MODEL`,
    # which would have paired and configured the device on the wrong profile
    # without a word. Entered without a choice, they must fail loudly instead.
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=service_info(
            "AA:BB:CC:DD:EE:33",
            name="BLESmart_0000123",
            manufacturer_data={OMRON_MANUFACTURER_ID: bytes([0x01, 0, 0, 0])},
        ),
    )
    assert result["type"] is FlowResultType.FORM
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert flow._selected_model is None

    for step in (
        flow.async_step_user_aliases,
        flow.async_step_pairing,
        flow.async_step_pairing_os,
    ):
        for user_input in (None, {}):
            with pytest.raises(AbortFlow) as err:
                await step(user_input)
            assert err.value.reason == "model_not_selected"


_MODEL_STEPS = ("select_model", "select_model_unknown", "select_model_ambiguous")


def _string_files() -> list[Path]:
    """strings.json plus every language file. A new translations/*.json is included."""
    component = _CONFIG_FLOW.parent
    return [
        component / "strings.json",
        *sorted((component / "translations").glob("*.json")),
    ]


def test_every_model_step_is_translated_everywhere() -> None:
    # The wording lives in the string files rather than being assembled in
    # Python, which is the only way it reaches a non-English UI.
    for path in _string_files():
        steps = json.loads(path.read_text(encoding="utf-8"))["config"]["step"]
        for step_id in _MODEL_STEPS:
            assert step_id in steps, f"{path.name} is missing {step_id}"
            assert steps[step_id].get("description"), f"{path.name}:{step_id}"
