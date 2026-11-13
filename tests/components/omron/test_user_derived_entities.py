"""Derived metrics and status flags of a cuff user (#301).

The metrics are the cuff's own, computed by the same functions; the flags are
only the ones the model's records carry.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.omron.omron_ble.bls import _parse_bp_measurement

from .test_user_measurements import (
    CUFF,
    SCALE,
    ALICE_BOB,
    MORNING_EVENING,
    _bp,
    _entry,
    _poll,
    _setup,
    _stored,
)

CUFF_TWO_FLAGS = "HEM-6401T"  # its records carry no cuff-fit or position bits
DERIVED = (
    "pulse_pressure",
    "mean_arterial_pressure_estimated",
    "blood_pressure_category",
    "shock_index",
    "rate_pressure_product",
)
ALL_FLAGS = ("irregular_pulse", "body_movement", "cuff_fit", "improper_position")


def _state(hass: HomeAssistant, entry, user: str, domain: str, key: str):
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        domain, "omron", f"{entry.entry_id}_{user}_{key}"
    )
    assert entity_id is not None, key
    return hass.states.get(entity_id)


def _own(hass: HomeAssistant, entry, user: str) -> dict[str, er.RegistryEntry]:
    prefix = f"{entry.entry_id}_{user}_"
    return {
        e.unique_id.removeprefix(prefix): e
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.unique_id.startswith(prefix)
    }


def _record(time: str, sys: int, dia: int, bpm: int | None, **flags: Any) -> dict:
    record = {**_bp(time, sys, dia), "bpm": bpm}
    record.update(flags)
    return record


async def _assign(hass, entry, record: dict, baseline: str = "2026-09-20T06:00:00"):
    """Baseline poll, then one record that goes to Bob (morning)."""
    await _poll(hass, entry, _bp(baseline, 100, 70))
    await _poll(hass, entry, record)


async def test_a_cuff_user_gets_the_derived_sensors_with_the_devices_attributes(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)

    own = _own(hass, entry, "bob")
    assert set(DERIVED) <= set(own)
    expected = {
        "pulse_pressure": ("mmHg", "mdi:chart-bell-curve-cumulative", "measurement"),
        "mean_arterial_pressure_estimated": (
            "mmHg",
            "mdi:waves-arrow-right",
            "measurement",
        ),
        "shock_index": ("ratio", "mdi:heart-flash", "measurement"),
        "rate_pressure_product": ("mmHg*bpm", "mdi:multiplication", "measurement"),
        "blood_pressure_category": (None, "mdi:clipboard-pulse-outline", None),
    }
    for key, (unit, icon, state_class) in expected.items():
        reg = own[key]
        assert reg.translation_key == f"user_{key}", key
        assert reg.unit_of_measurement == unit, key
        assert reg.original_icon == icon, key
        assert reg.capabilities.get("state_class") == state_class, key
        assert reg.disabled_by is None, key
        assert reg.config_subentry_id == "bob"
    assert own["blood_pressure_category"].original_device_class == "enum"
    assert own["blood_pressure_category"].capabilities["options"] == [
        "normal",
        "elevated",
        "hypertension_stage_1",
        "hypertension_stage_2",
        "hypertensive_crisis",
    ]
    # On the user's own device, one per user.
    devices = {own[key].device_id for key in own}
    assert len(devices) == 1
    assert (
        own["pulse_pressure"].device_id
        != _own(hass, entry, "eve")["pulse_pressure"].device_id
    )


async def test_flags_follow_what_the_models_records_carry(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    full = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, full)
    for user in ("bob", "eve"):
        own = _own(hass, full, user)
        assert {k for k in own if k in ALL_FLAGS} == set(ALL_FLAGS)
        for key in ALL_FLAGS:
            assert own[key].domain == "binary_sensor"
            assert own[key].original_device_class == "problem"
            assert own[key].translation_key == f"user_{key}"
            assert own[key].config_subentry_id == user
    assert await hass.config_entries.async_unload(full.entry_id)
    await hass.async_block_till_done()


async def test_a_model_without_cuff_and_position_bits_gets_two_flags(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF_TWO_FLAGS, MORNING_EVENING)
    await _setup(hass, entry)

    own = _own(hass, entry, "bob")
    assert {k for k in own if k in ALL_FLAGS} == {"irregular_pulse", "body_movement"}
    assert set(DERIVED) <= set(own)


async def test_everything_is_unknown_until_the_user_has_a_measurement(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)

    for key in DERIVED:
        assert _state(hass, entry, "bob", "sensor", key).state == "unknown", key
    for key in ALL_FLAGS:
        assert _state(hass, entry, "bob", "binary_sensor", key).state == "unknown"
    # The baseline poll assigns nothing either.
    await _poll(hass, entry, _bp("2026-09-20T07:00:00"))
    assert _state(hass, entry, "bob", "sensor", "shock_index").state == "unknown"


async def test_an_assigned_measurement_sets_metrics_and_flags(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    record = _record(
        "2026-09-21T07:30:00",
        131,
        84,
        70,
        irregular_pulse=True,
        body_movement=False,
        cuff_loose=True,
        improper_position=False,
    )
    await _assign(hass, entry, record)

    def value(key):
        return _state(hass, entry, "bob", "sensor", key).state

    assert value("pulse_pressure") == "47.0"
    assert value("mean_arterial_pressure_estimated") == "99.7"
    assert value("blood_pressure_category") == "hypertension_stage_1"
    assert value("shock_index") == "0.53"
    assert value("rate_pressure_product") == "9170.0"
    on = {
        key: _state(hass, entry, "bob", "binary_sensor", key).state for key in ALL_FLAGS
    }
    # Cuff fit is a problem sensor: a loose cuff is "on", as on the cuff's own.
    assert on == {
        "irregular_pulse": "on",
        "body_movement": "off",
        "cuff_fit": "on",
        "improper_position": "off",
    }
    # Eve was not given it.
    assert _state(hass, entry, "eve", "sensor", "pulse_pressure").state == "unknown"
    assert _state(hass, entry, "eve", "binary_sensor", "cuff_fit").state == "unknown"
    # The existing attributes are still there.
    systolic = _state(hass, entry, "bob", "sensor", "systolic")
    assert systolic.attributes["irregular_pulse"] is True
    assert systolic.attributes["assignment"] == "rule"
    stored = _stored(hass_storage, entry)["users"]["bob"]["last"]
    assert stored["cuff_loose"] is True
    assert stored["improper_position"] is False


async def test_a_measurement_without_a_pulse_leaves_the_pulse_metrics_unknown(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    await _assign(hass, entry, _record("2026-09-21T07:30:00", 131, 84, 70))
    assert _state(hass, entry, "bob", "sensor", "shock_index").state == "0.53"

    await _poll(hass, entry, _record("2026-09-22T07:30:00", 125, 82, None))

    assert _state(hass, entry, "bob", "sensor", "shock_index").state == "unknown"
    assert (
        _state(hass, entry, "bob", "sensor", "rate_pressure_product").state == "unknown"
    )
    assert _state(hass, entry, "bob", "sensor", "pulse_pressure").state == "43.0"
    assert _state(hass, entry, "bob", "sensor", "pulse").state == "unknown"


async def test_a_flag_the_record_lacks_is_unknown_not_stale(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    await _assign(
        hass, entry, _record("2026-09-21T07:30:00", 131, 84, 70, cuff_loose=True)
    )
    assert _state(hass, entry, "bob", "binary_sensor", "cuff_fit").state == "on"

    await _poll(
        hass,
        entry,
        _record("2026-09-22T07:30:00", 125, 82, 70, cuff_loose=None),
    )

    assert _state(hass, entry, "bob", "binary_sensor", "cuff_fit").state == "unknown"


async def test_the_flags_and_metrics_survive_a_restart(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    await _assign(
        hass,
        entry,
        _record(
            "2026-09-21T07:30:00",
            131,
            84,
            70,
            irregular_pulse=True,
            body_movement=False,
            cuff_loose=False,
            improper_position=True,
        ),
    )
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert _state(hass, entry, "bob", "sensor", "blood_pressure_category").state == (
        "hypertension_stage_1"
    )
    assert _state(hass, entry, "bob", "sensor", "shock_index").state == "0.53"
    flags = {
        key: _state(hass, entry, "bob", "binary_sensor", key).state for key in ALL_FLAGS
    }
    assert flags == {
        "irregular_pulse": "on",
        "body_movement": "off",
        "cuff_fit": "off",
        "improper_position": "on",
    }


async def test_a_store_from_before_the_flags_shows_them_unknown(
    hass: HomeAssistant, enable_bluetooth: None, hass_storage: dict
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    hass_storage[f"omron.users_{entry.entry_id}"] = {
        "version": 1,
        "key": f"omron.users_{entry.entry_id}",
        "data": {
            "strategy": "rules",
            "fingerprint": ["2026-09-21T07:30:00", 131, 84, 70],
            "last_seq": None,
            "users": {
                "bob": {
                    "recent": [],
                    "last": {
                        "sys": 131,
                        "dia": 84,
                        "bpm": 70,
                        "irregular_pulse": True,
                        "body_movement": False,
                        "measured_at": "2026-09-21T07:30:00+00:00",
                        "assignment": "rule",
                    },
                }
            },
        },
    }
    await _setup(hass, entry)

    assert _state(hass, entry, "bob", "sensor", "pulse_pressure").state == "47.0"
    assert _state(hass, entry, "bob", "binary_sensor", "irregular_pulse").state == "on"
    assert _state(hass, entry, "bob", "binary_sensor", "cuff_fit").state == "unknown"


async def test_the_user_and_the_cuff_agree_on_the_same_record(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    data = entry.runtime_data.device_data
    pairs = [
        (131, 84, 70),
        (118, 76, 61),
        (181, 121, 99),
        (125, 80, 0),
        (90, 60, 133),
        (140, 90, 55),
    ]
    day = 20
    await _poll(hass, entry, _bp("2026-09-19T06:00:00", 100, 70))
    for sys, dia, bpm in pairs:
        day += 1
        time = f"2026-09-{day}T07:30:00"
        # The cuff's own sensors, from the record's decoded values.
        data._update_measurement_sensors(
            {
                "sys": sys,
                "dia": dia,
                "bpm": bpm,
                "datetime": dt.datetime.fromisoformat(time),
            }
        )
        update = data._finish_update()
        device = {
            str(key.key): value.native_value
            for key, value in update.entity_values.items()
        }
        await _poll(hass, entry, _record(time, sys, dia, bpm))
        for key in DERIVED:
            user_state = _state(hass, entry, "bob", "sensor", key).state
            assert user_state == str(device[key]), (key, sys, dia, bpm)


async def test_a_pulse_pressure_guard_is_the_same_on_the_user(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)
    await _assign(hass, entry, _record("2026-09-21T07:30:00", 80, 80, 70))

    for key in ("pulse_pressure", "mean_arterial_pressure_estimated"):
        assert _state(hass, entry, "bob", "sensor", key).state == "unknown"
    assert _state(hass, entry, "bob", "sensor", "blood_pressure_category").state == (
        "unknown"
    )
    assert _state(hass, entry, "bob", "sensor", "shock_index").state == "0.88"


async def test_scale_users_are_unchanged(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = _entry(hass, SCALE, ALICE_BOB)
    await _setup(hass, entry)

    assert set(_own(hass, entry, "alice")) == {"weight", "bmi"}
    registry = er.async_get(hass)
    assert not [
        e
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.domain == "binary_sensor" and e.config_subentry_id
    ]


# --- one polarity for "cuff too loose", on every path ---------------------------------


def _bls_cuff_fit(loose: bool) -> bool:
    """The BLS path: Measurement Status bit 1, 1 = cuff too loose."""
    # Flags: kPa unit off, timestamp off, pulse off, user off, status present.
    payload = (
        bytes([0x10])
        + b"\x78\xf0\x50\xf0\x64\xf0"
        + (0x02 if loose else 0x00).to_bytes(2, "little")
    )
    parsed = _parse_bp_measurement(payload)
    assert parsed is not None
    return parsed["status_flags"]["cuff_fit"]


def _device_cuff_fit(entry, record: dict) -> bool:
    """The device's binary sensor value for an EEPROM record (``cuff`` bit)."""
    data = entry.runtime_data.device_data
    data._update_measurement_sensors(record)
    update = data._finish_update()
    values = [
        value.native_value
        for key, value in update.binary_entity_values.items()
        if str(key.key).endswith("cuff_fit")
    ]
    assert len(values) == 1
    return values[0]


@pytest.mark.parametrize(("loose", "state"), [(True, "on"), (False, "off")])
async def test_every_path_reads_a_loose_cuff_as_on(
    hass: HomeAssistant, enable_bluetooth: None, loose: bool, state: str
) -> None:
    """BLS, EEPROM and the user's sensor agree: loose is a problem, a fit is not."""
    entry = _entry(hass, CUFF, MORNING_EVENING)
    await _setup(hass, entry)

    assert _bls_cuff_fit(loose) is loose
    eeprom = _device_cuff_fit(
        entry,
        {
            "sys": 131,
            "dia": 84,
            "bpm": 70,
            "datetime": dt.datetime(2026, 9, 21, 7, 30),
            "cuff": int(loose),
        },
    )
    assert eeprom is loose

    await _assign(
        hass, entry, _record("2026-09-21T07:30:00", 131, 84, 70, cuff_loose=loose)
    )
    assert _state(hass, entry, "bob", "binary_sensor", "cuff_fit").state == state
