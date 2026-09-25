"""Entity names come from translation keys, including multi-user placeholders."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

# Load the helper by path. Importing the integration package pulls in
# Home Assistant and blesession, which this check does not need.
_UTIL_PATH = (
    Path(__file__).resolve().parents[1]
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

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "omron"
_STRING_FILES = (
    _COMPONENT / "strings.json",
    _COMPONENT / "translations" / "en.json",
    _COMPONENT / "translations" / "ko.json",
)


def test_single_user_key_has_no_placeholder() -> None:
    key, placeholders = entity_name_translation("blood_pressure_systolic", {1: "홍길동"})
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
    loaded = [json.loads(path.read_text(encoding="utf-8"))["entity"] for path in _STRING_FILES]
    for domain in ("sensor", "binary_sensor", "button", "text"):
        key_sets = [set(doc[domain]) for doc in loaded]
        assert key_sets[0] == key_sets[1] == key_sets[2]
    for doc in loaded:
        for key, spec in doc["sensor"].items():
            if key.endswith("_user"):
                assert "{user}" in spec["name"]
            else:
                assert "{user}" not in spec["name"]
        for key, spec in doc["binary_sensor"].items():
            if key.endswith("_user"):
                assert "{user}" in spec["name"]
