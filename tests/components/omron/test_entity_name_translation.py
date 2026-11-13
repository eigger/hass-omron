"""Entity names come from translation keys, including multi-user placeholders."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

# Load the helper by path. Importing the integration package pulls in
# Home Assistant and blesession, which this check does not need.
_UTIL_PATH = (
    Path(__file__).resolve().parents[3]
    / "custom_components"
    / "omron"
    / "omron_ble"
    / "util.py"
)
_spec = importlib.util.spec_from_file_location("omron_entity_name_util", _UTIL_PATH)
assert _spec is not None and _spec.loader is not None
_util = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_util)
entity_name_translation = _util.entity_name_translation

_COMPONENT = Path(__file__).resolve().parents[3] / "custom_components" / "omron"
_STRING_FILES = (
    _COMPONENT / "strings.json",
    *sorted((_COMPONENT / "translations").glob("*.json")),
)


def test_single_user_key_has_no_placeholder() -> None:
    key, placeholders = entity_name_translation(
        "blood_pressure_systolic", {1: "홍길동"}
    )
    assert key == "blood_pressure_systolic"
    assert placeholders == {}


def test_second_slot_keeps_the_entered_name_as_a_placeholder() -> None:
    # Non-ASCII labels slugify to userN, but the display placeholder stays the
    # text the user typed.
    key, placeholders = entity_name_translation(
        "blood_pressure_systolic_user1",
        {1: "홍길동", 2: "user2"},
    )
    assert key == "blood_pressure_systolic_user"
    assert placeholders == {"user": "홍길동"}


def test_ascii_alias_matches_its_slug() -> None:
    key, placeholders = entity_name_translation(
        "pulse_pressure_alex",
        {1: "Alex"},
    )
    assert key == "pulse_pressure_user"
    assert placeholders == {"user": "Alex"}


def test_default_slot_label_round_trips() -> None:
    key, placeholders = entity_name_translation("heart_rate_user2", {2: "user2"})
    assert key == "heart_rate_user"
    assert placeholders == {"user": "user2"}


def test_device_level_flag_is_not_a_user_entity() -> None:
    key, placeholders = entity_name_translation("forced_transfer", {1: "홍길동"})
    assert key == "forced_transfer"
    assert placeholders == {}


def test_unknown_key_is_left_alone() -> None:
    assert entity_name_translation("not_a_sensor", None) is None


def test_translation_files_share_entity_keys_and_user_placeholder() -> None:
    loaded = [
        json.loads(path.read_text(encoding="utf-8"))["entity"] for path in _STRING_FILES
    ]
    for domain in ("sensor", "binary_sensor", "button", "number", "text"):
        key_sets = [set(doc[domain]) for doc in loaded]
        assert all(keys == key_sets[0] for keys in key_sets), domain
    for doc in loaded:
        for key, spec in doc["sensor"].items():
            if key.endswith("_user"):
                assert "{user}" in spec["name"]
            else:
                assert "{user}" not in spec["name"]
        for key, spec in doc["binary_sensor"].items():
            if key.endswith("_user"):
                assert "{user}" in spec["name"]


def test_translation_files_share_the_bmi_strings() -> None:
    docs = [json.loads(path.read_text(encoding="utf-8")) for path in _STRING_FILES]
    for doc in docs:
        category = doc["entity"]["sensor"]["bmi_category"]
        assert set(category["state"]) == {
            "underweight",
            "normal",
            "overweight",
            "obesity_class_1",
            "obesity_class_2",
            "obesity_class_3",
        }
        assert set(doc["entity"]["number"]) == {"height"}
        message = doc["exceptions"]["height_out_of_range"]["message"]
        assert "{min}" in message and "{max}" in message


_USER_SENSOR_KEYS = {
    "user_weight",
    "user_bmi",
    "user_systolic",
    "user_diastolic",
    "user_pulse",
    "user_measured_at",
    "user_body_fat",
    "user_skeletal_muscle",
    "user_visceral_fat",
    "user_basal_metabolic_rate",
    "user_body_composition_bmi",
}


def test_translation_files_share_the_multi_user_strings() -> None:
    """Every language carries the same users strings as the source file (#301)."""
    docs = {
        path: json.loads(path.read_text(encoding="utf-8")) for path in _STRING_FILES
    }
    source = docs[_COMPONENT / "strings.json"]

    def shape(node):
        """Keys all the way down, without the translated text."""
        if isinstance(node, dict):
            return {key: shape(value) for key, value in node.items()}
        return None

    for path, doc in docs.items():
        assert _USER_SENSOR_KEYS <= set(doc["entity"]["sensor"]), path.name
        assert shape(doc["config_subentries"]) == shape(source["config_subentries"])
        assert shape(doc["selector"]) == shape(source["selector"])
        init = doc["options"]["step"]["init"]
        for key in ("assignment_strategy", "weight_tolerance_pct"):
            assert init["data"][key], path.name
            assert init["data_description"][key], path.name
        user = doc["config_subentries"]["user"]
        assert set(user["step"]) == {
            "weight",
            "rules",
            "pressure",
            "slot",
            "confirm_no_rules",
            "confirm_overlap",
        }
        assert "{name}" in user["step"]["confirm_no_rules"]["description"]
        assert "{name}" in user["step"]["confirm_overlap"]["description"]
        assert "{others}" in user["step"]["confirm_overlap"]["description"]
        assert set(user["error"]) == {
            "invalid_name",
            "invalid_time_windows",
            "invalid_range",
            "slot_taken",
        }
        for name in _USER_SENSOR_KEYS:
            assert "{user}" not in doc["entity"]["sensor"][name]["name"]


def test_every_language_translates_the_reassign_service() -> None:
    """The service and its errors read the same in every language (#301)."""
    import yaml

    fields = set(
        yaml.safe_load((_COMPONENT / "services.yaml").read_text(encoding="utf-8"))[
            "reassign_measurement"
        ]["fields"]
    )
    assert fields == {"measurement_id", "user"}
    for path in _STRING_FILES:
        doc = json.loads(path.read_text(encoding="utf-8"))
        service = doc["services"]["reassign_measurement"]
        assert service["name"] and service["description"], path.name
        assert set(service["fields"]) == fields, path.name
        for field in service["fields"].values():
            assert field["name"] and field["description"], path.name
        exceptions = doc["exceptions"]
        assert "{measurement_id}" in exceptions["unknown_measurement"]["message"]
        for key in ("not_a_user", "user_other_device"):
            assert exceptions[key]["message"], path.name
