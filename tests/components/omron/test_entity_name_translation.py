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
            "confirm_no_rules",
            "confirm_no_rules_shared",
            "confirm_overlap",
        }
        assert "{name}" in user["step"]["confirm_no_rules"]["description"]
        assert "{name}" in user["step"]["confirm_overlap"]["description"]
        assert "{others}" in user["step"]["confirm_overlap"]["description"]
        assert "{name}" in user["step"]["confirm_no_rules_shared"]["description"]
        assert "{others}" in user["step"]["confirm_no_rules_shared"]["description"]
        for step in ("rules", "pressure"):
            assert "{extra_windows}" not in user["step"][step]["description"]
            when = user["step"][step]["sections"]["when"]
            assert set(when["data"]) == {"windows"}
            assert set(when["data_description"]) == set(when["data"])
        # The labels of the fields in the dialog of an added time window.
        fields = doc["selector"]["time_window"]["fields"]
        assert fields["start"]["name"] and fields["end"]["name"], path.name
        assert set(user["error"]) == {
            "invalid_name",
            "time_window_incomplete",
            "time_window_empty",
            "invalid_range",
        }
        for name in _USER_SENSOR_KEYS:
            assert "{user}" not in doc["entity"]["sensor"][name]["name"]


def _string_values(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from _string_values(value)
    elif isinstance(node, str):
        yield node


def test_no_text_uses_an_ascii_tilde() -> None:
    """Home Assistant renders flow texts as Markdown, where ``~a~`` is struck out.

    A range such as ``06:00~08:00 ... 21:00~23:00`` in one text came out with
    everything between the two tildes crossed out. Write ranges with a dash or a
    full-width tilde instead.
    """
    files = [
        _COMPONENT / "strings.json",
        *sorted(_COMPONENT.glob("translations/*.json")),
    ]
    offending = {
        path.name: [
            text
            for text in _string_values(json.loads(path.read_text("utf-8")))
            if "~" in text
        ]
        for path in files
    }
    assert {name: texts for name, texts in offending.items() if texts} == {}
