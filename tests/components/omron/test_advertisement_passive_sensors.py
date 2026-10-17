"""Advertisement flag sensors update via PassiveBluetooth, not the poll coordinator.

Data Pending / Pairing Mode / Time Sync Required are MSD advertisement
flags. They are not seeded: a startup ``False`` would overwrite a restored
``on`` and would show a cuff that has never advertised as off.
RSSI follows processor availability so a stale dBm is not recorded.

A real config entry is set up against a real Home Assistant; only the
advertisement itself is delivered by hand (to the entry's passive
coordinator, exactly as the Bluetooth manager would) and, for the startup
prime, the manager's cached sighting is replaced.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.omron.const import CONF_DEVICE_MODEL, DOMAIN
from custom_components.omron.entity_helpers import preserved_passive_unique_id
from custom_components.omron.omron_ble.const import OMRON_MANUFACTURER_ID

from bt import service_info

ADDRESS = "AA:BB:CC:DD:EE:FF"
MODEL = "HEM-7155T"
# Format 0x03 status byte: 0x04 = invalid time, 0x08 = pairing mode.
_PAIRING_MODE = bytes([0x03, 0x08, 0x00, 0x00, 0x00])
_NO_FLAGS = bytes([0x03, 0x00, 0x00, 0x00, 0x00])

_PAIRING_UID = "hem_7155t_eeff_none_pairing_mode"
_INVALID_TIME_UID = "hem_7155t_eeff_none_invalid_time"
_FORCED_UID = "hem_7155t_eeff_none_forced_transfer"
_RSSI_UID = "hem_7155t_eeff_none_signal_strength"


def _advert(payload: bytes, rssi: int = -61):
    # Not connectable: the flags must be readable without a session starting.
    return service_info(
        ADDRESS,
        name=MODEL,
        manufacturer_data={OMRON_MANUFACTURER_ID: payload},
        rssi=rssi,
        connectable=False,
    )


async def _setup(hass: HomeAssistant, *, enable_rssi: bool = False) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title=f"{MODEL} EEFF",
        data={CONF_DEVICE_MODEL: MODEL},
    )
    entry.add_to_hass(hass)
    if enable_rssi:
        # RSSI is a diagnostic that ships disabled; enable it as a user would.
        er.async_get(hass).async_get_or_create(
            "sensor", DOMAIN, _RSSI_UID, config_entry=entry, disabled_by=None
        )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)


def _state(hass: HomeAssistant, domain: str, unique_id: str):
    entity_id = _entity_id(hass, domain, unique_id)
    assert entity_id is not None, f"{domain} {unique_id} is not registered"
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} has no state"
    return state


async def _deliver(hass: HomeAssistant, entry: MockConfigEntry, info) -> None:
    entry.runtime_data.bt_coordinator._async_handle_bluetooth_event(
        info, BluetoothChange.ADVERTISEMENT
    )
    await hass.async_block_till_done()


async def _go_quiet(hass: HomeAssistant, entry: MockConfigEntry, info) -> None:
    """The Bluetooth manager's 'no advertisement for a while' callback."""
    entry.runtime_data.bt_coordinator._async_handle_unavailable(info)
    await hass.async_block_till_done()


async def test_advertisement_flags_come_from_msd_not_a_seed(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = await _setup(hass)
    # Nothing has advertised: no flag may exist, least of all as ``off``.
    for domain, uid in (
        ("binary_sensor", _PAIRING_UID),
        ("binary_sensor", _INVALID_TIME_UID),
        ("binary_sensor", _FORCED_UID),
    ):
        assert _entity_id(hass, domain, uid) is None, uid

    await _deliver(hass, entry, _advert(_PAIRING_MODE))
    assert _state(hass, "binary_sensor", _PAIRING_UID).state == "on"
    assert _state(hass, "binary_sensor", _INVALID_TIME_UID).state == "off"
    assert _state(hass, "binary_sensor", _FORCED_UID).state == "off"

    await _deliver(hass, entry, _advert(_NO_FLAGS))
    assert _state(hass, "binary_sensor", _PAIRING_UID).state == "off"

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_flag_keeps_its_value_while_the_cuff_is_asleep_but_rssi_does_not(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = await _setup(hass, enable_rssi=True)
    info = _advert(_PAIRING_MODE, rssi=-61)
    await _deliver(hass, entry, info)
    # The converter keeps the dBm an integer: "-61", never "-61.0".
    assert _state(hass, "sensor", _RSSI_UID).state == "-61"

    await _go_quiet(hass, entry, info)

    # A real on/off survives the cuff going quiet; a measurement does not.
    assert _state(hass, "binary_sensor", _PAIRING_UID).state == "on"
    assert _state(hass, "sensor", _RSSI_UID).state == STATE_UNAVAILABLE

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_binary_flag_without_a_real_bool_stays_unavailable(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    entry = await _setup(hass)
    info = _advert(_PAIRING_MODE)
    await _deliver(hass, entry, info)
    await _go_quiet(hass, entry, info)
    assert _state(hass, "binary_sensor", _PAIRING_UID).state == "on"

    # An unknown (None) flag must read unavailable, not 'off'.
    (processor,) = [
        p
        for p in entry.runtime_data.bt_coordinator._processors
        if any(k.key == "pairing_mode" for k in p.entity_data)
    ]
    key = next(k for k in processor.entity_data if k.key == "pairing_mode")
    processor.entity_data[key] = None
    processor.async_update_listeners(None)
    await hass.async_block_till_done()

    assert _state(hass, "binary_sensor", _PAIRING_UID).state == STATE_UNAVAILABLE

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_primes_from_a_nonconnectable_sighting(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    """A proxy sighting only exists in the non-connectable history."""
    seen: list[bool] = []

    def _last_info(hass_, address, connectable=True):
        seen.append(connectable)
        return _advert(_PAIRING_MODE) if connectable is False else None

    with patch("custom_components.omron.async_last_service_info", _last_info):
        entry = await _setup(hass)

    assert seen == [False]
    assert _state(hass, "binary_sensor", _PAIRING_UID).state == "on"

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_without_a_cached_sighting_pushes_nothing(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    with patch("custom_components.omron.async_last_service_info", return_value=None):
        entry = await _setup(hass)

    assert _entity_id(hass, "binary_sensor", _PAIRING_UID) is None

    assert await hass.config_entries.async_unload(entry.entry_id)


def test_preserved_unique_id_matches_the_poll_entity_formula() -> None:
    """device_id is None for MSD flags and RSSI, so the slug is none_<key>."""
    for key, expected in (
        ("pairing_mode", "hem_7155t_eeff_none_pairing_mode"),
        ("signal_strength", "hem_7155t_eeff_none_signal_strength"),
    ):
        assert (
            preserved_passive_unique_id(
                model="HEM-7155T",
                address="AA:BB:CC:DD:EE:FF",
                entity_key=SimpleNamespace(device_id=None, key=key),
            )
            == expected
        )
