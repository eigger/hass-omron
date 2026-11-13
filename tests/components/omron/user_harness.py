"""Helpers for the tests that drive users through the real poll path.

Only the BLE boundary is replaced: ``async_poll`` of the parser is swapped for
a function that leaves what a readout would have left behind (the newest
record, the synced events), and everything after it is the integration's own.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from bleak.backends.device import BLEDevice
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.omron import async_poll_data
from custom_components.omron.const import (
    CONF_DEVICE_MODEL,
    DOMAIN,
    SUBENTRY_USER,
)
from custom_components.omron.omron_ble.const import ExtendedSensorDeviceClass

ADDRESS = "AA:BB:CC:DD:EE:FF"


def user(subentry_id: str, name: str, **data: Any) -> dict[str, Any]:
    return {
        "subentry_id": subentry_id,
        "subentry_type": SUBENTRY_USER,
        "title": name,
        "unique_id": None,
        "data": {"name": name, **data},
    }


def scale_event(
    time: str | None,
    weight: float,
    *,
    seq: int | None = None,
    slot: int = 1,
    **body: Any,
) -> dict[str, Any]:
    """What a synced or latest record of a scale looks like to the hook."""
    return {
        "user": slot,
        "user_alias": None,
        "sequence": seq,
        "measured_at": f"{time}+00:00" if time else None,
        "device_time": time,
        "weight": weight,
        "body_fat": body.get("body_fat"),
        "skeletal_muscle": body.get("skeletal_muscle"),
        "visceral_fat": body.get("visceral_fat"),
        "basal_metabolic_rate": body.get("basal_metabolic_rate"),
        "bmi": body.get("bmi"),
    }


def bp_event(
    time: str | None,
    sys: int = 120,
    dia: int = 80,
    *,
    seq: int | None = None,
) -> dict[str, Any]:
    return {
        "user": 1,
        "user_alias": None,
        "sequence": seq,
        "measured_at": f"{time}+00:00" if time else None,
        "device_time": time,
        "sys": sys,
        "dia": dia,
        "bpm": 60,
        "irregular_pulse": False,
        "body_movement": True,
        "cuff_loose": False,
        "improper_position": False,
    }


def add_entry(
    hass: HomeAssistant,
    model: str,
    users: list[dict[str, Any]],
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title=f"{model} EEFF",
        data={CONF_DEVICE_MODEL: model},
        options=options or {},
        subentries_data=users,
    )
    entry.add_to_hass(hass)
    return entry


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _as_map(value: Any) -> dict[int, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return {1: value}


async def poll(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    record: dict | None = None,
    *,
    records: dict[int, dict] | None = None,
    events: list[dict] | None = None,
    next_seq: int | dict[int, int] | None = None,
    rebased: bool | dict[int, bool] = False,
    cursor: dict[int, dict] | None = None,
) -> Any:
    """One poll. ``record`` is the single-slot newest, ``records`` per slot."""
    data = entry.runtime_data.device_data

    async def fake_poll(device, preconnected_session=None):
        data.last_record = record
        if records is not None:
            data.last_records = records
        if record is not None and record.get("weight") is not None:
            data.update_sensor(
                "weight",
                "kg",
                record["weight"],
                device_class=ExtendedSensorDeviceClass.WEIGHT,
                name="Weight",
            )
        rebase_map = {slot: bool(flag) for slot, flag in _as_map(rebased).items()} or {
            1: False
        }
        if events is not None or any(rebase_map.values()):
            pending: dict[str, Any] = {
                "next_seq": _as_map(next_seq),
                "events": events or [],
                "shown": {},
                "rebased": rebase_map,
            }
            if cursor is not None:
                pending["cursor"] = cursor
            data.pending_history = pending
        return data._finish_update()

    with (
        patch(
            "custom_components.omron.async_ble_device_from_address",
            return_value=BLEDevice(ADDRESS, "device", {}),
        ),
        patch.object(data, "async_poll", fake_poll),
    ):
        result = await async_poll_data(hass, entry)
    await hass.async_block_till_done()
    return result


def sensor_state(hass: HomeAssistant, entry: MockConfigEntry, user_id: str, key: str):
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{user_id}_{key}"
    )
    assert entity_id is not None, key
    return hass.states.get(entity_id)


def stored(hass_storage: dict[str, Any], entry: MockConfigEntry) -> dict[str, Any]:
    return hass_storage[f"omron.users_{entry.entry_id}"]["data"]
