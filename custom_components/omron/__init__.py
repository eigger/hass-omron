"""The Omron Bluetooth integration."""

from __future__ import annotations

from functools import partial
import asyncio
import logging
import time
from typing import Any

from sensor_state_data import BinarySensorDeviceClass as SSDBinarySensorDeviceClass
from sensor_state_data import SensorDeviceClass as SSDSensorDeviceClass

from .ble_session import (
    adopt_handoff_session,
    discard_handoff_session,
    has_handoff_session,
    request_poll,
    should_skip_scheduled_poll,
    peek_poll_request,
    omron_poll_ble_telemetry,
    poll_parked_session,
    run_post_pairing_poll,
)
from .omron_ble import OmronBluetoothDeviceData, SensorUpdate
from .omron_ble.const import DEFAULT_DEVICE_MODEL, OMRON_MANUFACTURER_ID
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_register_callback,
)
from homeassistant.const import Platform, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from datetime import timedelta
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from .const import (
    CONF_DEVICE_MODEL,
    DOMAIN,
)
from .util import aliases_dict_from_entry
from .coordinator import OmronBluetoothProcessorCoordinator
from .types import OmronConfigEntry

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.TEXT,
]

_LOGGER = logging.getLogger(__name__)

# Minimum gap between advertisement log lines when only the raw MSD changed.
# Flag and connectability changes ignore this — they are the events the log
# exists for; a rolling byte in the payload is not.
_ADVERT_MSD_LOG_INTERVAL_SEC = 5.0

# BLE advertisement trigger control constants
POLL_COOLDOWN_SECONDS = 60
SETTLE_DELAY_SECONDS = 0.5

# Hard ceiling on one poll. Bleak's BlueZ backend puts no timeout on its
# read/write/notify D-Bus calls (only ``disconnect`` is bounded), so a wedged
# bluetoothd leaves the poll awaiting forever. That poll holds ``session_lock``,
# and from then on every scheduled poll, Refresh Data press and advertisement
# trigger bails out on the held lock — the integration goes silent with no error
# logged until Home Assistant restarts. The deadline gives the lock back.
# Budget: a worst-case connect (~90 s over 4 attempts) plus the memory-session
# retries. Past that the link is stuck, not slow.
POLL_TIMEOUT_SECONDS = 180

# When a poll fails mid-flight, keep measurement history but drop stale RSSI/battery
# unless this poll refreshed those keys (avoids showing outdated diagnostics).
_STALE_DROP_SENSOR_DEVICE_CLASSES: frozenset = frozenset({
    SSDSensorDeviceClass.BATTERY,
    SSDSensorDeviceClass.SIGNAL_STRENGTH,
})
_STALE_DROP_BINARY_DEVICE_CLASSES: frozenset = frozenset({
    SSDBinarySensorDeviceClass.BATTERY,
})


def _register_advertisement_observer(
    hass: HomeAssistant, entry: OmronConfigEntry, address: str
) -> None:
    """Log every advertisement from this cuff, connectable or not.

    The processor coordinator registers with ``connectable=True``, so Home
    Assistant only ever hands it advertisements it could connect on. A cuff
    that announces "measurement waiting" in a non-connectable advertisement is
    therefore invisible to ``process_service_info`` — not because of the
    connectable check inside it, but because the advertisement was filtered
    one level up, before any of our code ran.

    That is the difference between "this hardware cannot do buttonless
    collection" and "we are not listening on the channel it uses", and issue
    #92 currently cannot tell them apart: two real measurements produced no
    trace at all. This observer registers with ``connectable=False``, which in
    Home Assistant means "give me everything", purely to find out.

    Diagnostic only — it never touches device state or starts a session.
    """

    @callback
    def _observe(
        service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        md = getattr(service_info, "manufacturer_data", None) or {}
        payload = md.get(OMRON_MANUFACTURER_ID)
        msd_hex = payload.hex() if payload else "none"

        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if entry_data is None:
            return
        seen = (msd_hex, service_info.connectable)
        if entry_data.get("last_observed_advert") == seen:
            return
        entry_data["last_observed_advert"] = seen

        decoded: Any = None
        if payload:
            try:
                decoded = OmronBluetoothDeviceData._decode_omron_msd_fields(
                    bytes(payload)
                )
            except Exception:  # noqa: BLE001 - diagnostic must never raise
                decoded = None
        if decoded is None:
            summary = "undecodable" if payload else "no Omron MSD"
        else:
            summary = (
                f"pairing_mode={decoded['pairing_mode']} "
                f"invalid_time={decoded['invalid_time']} "
                f"forced_transfer={decoded['forced_transfer']}"
            )
        _LOGGER.debug(
            "Observed advertisement from %s: connectable=%s rssi=%s msd=%s (%s)",
            service_info.address,
            service_info.connectable,
            getattr(service_info, "rssi", "?"),
            msd_hex,
            summary,
        )

    entry.async_on_unload(
        async_register_callback(
            hass,
            _observe,
            BluetoothCallbackMatcher(address=address, connectable=False),
            BluetoothScanningMode.PASSIVE,
        )
    )


def _merge_poll_sensor_update(prev: SensorUpdate, new: SensorUpdate) -> SensorUpdate:
    """Overlay the latest poll delta on the previous coordinator snapshot.

    ``SensorData._finish_update`` returns only keys touched during that poll. The
    poll ``DataUpdateCoordinator`` assigns ``data`` from that return value alone,
    so a failed or partial poll would otherwise erase measurements still valid
    on the device.
    """
    merged_descriptions = {**prev.entity_descriptions, **new.entity_descriptions}
    merged_values = {**prev.entity_values, **new.entity_values}
    merged_b_descriptions = {
        **prev.binary_entity_descriptions,
        **new.binary_entity_descriptions,
    }
    merged_b_values = {**prev.binary_entity_values, **new.binary_entity_values}
    merged_events = {**prev.events, **new.events}

    for device_key in list(merged_values.keys()):
        desc = merged_descriptions.get(device_key)
        if desc is None or desc.device_class is None:
            continue
        if (
            desc.device_class in _STALE_DROP_SENSOR_DEVICE_CLASSES
            and device_key not in new.entity_values
        ):
            merged_values.pop(device_key, None)
            merged_descriptions.pop(device_key, None)

    for device_key in list(merged_b_values.keys()):
        desc = merged_b_descriptions.get(device_key)
        if desc is None or desc.device_class is None:
            continue
        if (
            desc.device_class in _STALE_DROP_BINARY_DEVICE_CLASSES
            and device_key not in new.binary_entity_values
        ):
            merged_b_values.pop(device_key, None)
            merged_b_descriptions.pop(device_key, None)

    return SensorUpdate(
        title=new.title if new.title is not None else prev.title,
        devices=new.devices or prev.devices,
        entity_descriptions=merged_descriptions,
        entity_values=merged_values,
        binary_entity_descriptions=merged_b_descriptions,
        binary_entity_values=merged_b_values,
        events=merged_events,
    )


def process_service_info(
    entry: OmronConfigEntry,
    service_info: BluetoothServiceInfoBleak,
) -> SensorUpdate:
    """Process a BluetoothServiceInfoBleak, running side effects and returning sensor data."""
    coordinator = entry.runtime_data
    data = coordinator.device_data
    update = data.update(service_info)

    entry_data = coordinator.hass.data[DOMAIN][entry.entry_id]

    is_pairing = getattr(data, "pairing_mode", False)
    is_invalid_time = getattr(data, "invalid_time", False)
    is_forced_transfer = getattr(data, "forced_transfer", False)

    # Logged before the returns below, and on every transition rather than only
    # when we act. Both early returns used to swallow the flags silently, so a
    # missing log line could equally mean "the cuff raised nothing", "it raised
    # something on a non-connectable advertisement", or "it raised a flag we
    # don't trigger on" — three very different answers to whether automatic
    # collection is possible, and no way to tell them apart from a debug log
    # (issue #92). Only transitions are logged: these cuffs advertise about
    # once a second, and a line per advertisement would bury what it is for.
    # The raw MSD rides along because the decoded flags cannot answer the
    # question on their own: forced_transfer=False means "the cuff did not
    # raise it", "this MSD format has no bit for it" (0x03 carries none), or
    # "the payload failed its length contract and was dropped" — and only the
    # first is a hardware limit. The format byte separates them.
    msd = getattr(data, "last_msd", None)
    msd_hex = msd.hex() if msd else "none"
    msd_format = f"0x{msd[0]:02X}" if msd else "-"
    msd_decoded = getattr(data, "last_msd_decoded", False)

    # Keyed on the MSD too, so a payload that changes while the decoded flags
    # stay False is still visible — which is exactly what a measurement that
    # we are failing to recognise would look like. A per-advertisement field
    # in the payload (a sequence byte, say) would otherwise put this back to a
    # line a second, so MSD-only changes are throttled; a change in the
    # decoded flags or in connectability always prints.
    decoded_key = (
        is_pairing, is_invalid_time, is_forced_transfer, service_info.connectable
    )
    flags = decoded_key + (msd_hex,)
    previous = entry_data.get("last_advert_flags")
    changed = previous != flags
    decoded_changed = previous is None or previous[:4] != decoded_key
    now = time.monotonic()
    throttled = (
        not decoded_changed
        and now - entry_data.get("last_advert_log", 0.0) < _ADVERT_MSD_LOG_INTERVAL_SEC
    )
    if changed and not throttled:
        entry_data["last_advert_flags"] = flags
        entry_data["last_advert_log"] = now
        _LOGGER.debug(
            "Advertisement flags for %s: pairing_mode=%s invalid_time=%s "
            "forced_transfer=%s connectable=%s msd=%s (format=%s decoded=%s)",
            service_info.address,
            is_pairing,
            is_invalid_time,
            is_forced_transfer,
            service_info.connectable,
            msd_hex,
            msd_format,
            msd_decoded,
        )

    # 1. Only attempt active sessions when the device is connectable
    if not service_info.connectable:
        return update

    # Latched here, before the session-lock and cooldown returns below, because
    # those drop the advertisement entirely. A cuff that announced a waiting
    # measurement while another session held the lock would otherwise be
    # forgotten: the next scheduled poll is up to a scan interval away, by
    # which time the flag has aged past ADVERT_FLAG_FRESHNESS_SECONDS and the
    # gate skips it. The measurement then sits unread with nothing left that
    # knows to go and get it. Re-latching while the flag stays up just
    # refreshes the request's TTL, which is what we want.
    if is_forced_transfer:
        request_poll(entry_data, "forced-transfer advertisement")

    # Trigger sync only for explicit device flags. A poll coordinator being present
    # is not itself a reason to connect on every advertisement.
    is_sync_needed = (
        is_pairing
        or is_invalid_time
        or is_forced_transfer
    )
    if not is_sync_needed:
        return update

    # 2. Fail fast if a GATT session is already running — try-acquire only, no queueing.
    # The device rejects a second concurrent BLE connection with SMP auth fail
    # (reasons 97/102 on ESP32 proxies), so we drop the trigger and rely on the
    # next advertisement (devices keep emitting the flag bits for several seconds)
    # to retry once the session lock is free.
    session_lock: asyncio.Lock = entry_data["session_lock"]
    if session_lock.locked():
        _LOGGER.debug(
            "BLE session lock held; skipping advertisement trigger for %s",
            service_info.address,
        )
        return update

    # 3. Enforce a shared cooldown between GATT session attempts
    now = time.time()
    last_attempt = entry_data.get("last_attempt_time", 0.0)
    if now - last_attempt < POLL_COOLDOWN_SECONDS:
        _LOGGER.debug(
            "Skipping advertisement trigger for %s (cooldown active, last attempt %ds ago)",
            service_info.address,
            int(now - last_attempt),
        )
        return update

    async def _run_auto_session() -> None:
        # forced_transfer-only path has no direct BLE op here — it just kicks
        # the poll coordinator, which goes through _async_poll_data and handles
        # its own lock acquisition. Don't hold the lock during request_refresh,
        # otherwise the child poll would see lock locked and return cached data.
        if is_forced_transfer and not is_pairing and not is_invalid_time:
            entry_data["last_attempt_time"] = time.time()
            _LOGGER.debug(
                "Triggering scheduled poll via forced-transfer flag for %s",
                service_info.address,
            )
            try:
                await coordinator.poll_coordinator.async_request_refresh()
            except Exception as err:
                _LOGGER.error("Auto polling failed: %s", err)
            return

        # Pair / time-sync paths own a direct BLE op — hold the lock for that.
        if session_lock.locked():
            _LOGGER.debug(
                "BLE session lock held when auto-session task started; aborting for %s",
                service_info.address,
            )
            return

        # An earlier attempt may have left a session parked and still
        # connected because its poll skipped. Both branches below open a BLE
        # link, so either would make it a second one on the same cuff — not
        # just the pairing branch. Checked before taking the lock: the poll
        # needs it to adopt the parked session.
        #
        # The poll does not time-sync, so an invalid_time advert loses that
        # this round; the device keeps the flag set and the next advert syncs
        # it once the parked session has been consumed.
        if coordinator.poll_coordinator and await poll_parked_session(
            coordinator.hass, service_info.address, coordinator.poll_coordinator
        ):
            # Seed the cooldown as the session paths do, or a run of adverts
            # spawns this task again on every one of them.
            entry_data["last_attempt_time"] = time.time()
            return

        action = "auto-pairing" if is_pairing else "time-sync"
        # Doubles as the "pairing succeeded" flag: set only once the cuff is
        # bonded, and holds the live link for the refresh below to adopt.
        paired_session = None
        try:
            async with session_lock:
                entry_data["last_attempt_time"] = time.time()
                _LOGGER.debug(
                    "Starting %s session for %s (lock acquired)",
                    action,
                    service_info.address,
                )
                await asyncio.sleep(SETTLE_DELAY_SECONDS)
                ble_device = service_info.device
                if is_pairing:
                    async with omron_poll_ble_telemetry(entry_data):
                        paired_session = await data.async_retry_pairing(ble_device)
                else:  # is_invalid_time and not is_forced_transfer
                    async with omron_poll_ble_telemetry(entry_data):
                        await data.async_sync_time(ble_device)
        except Exception as err:
            if is_pairing:
                _LOGGER.error("Auto pairing failed: %s", err)
            else:
                _LOGGER.error("Auto time sync failed: %s", err)

        # Lock auto-released by the context manager. The post-pairing poll runs
        # AFTER the release so _async_poll_data can acquire it independently,
        # and adopts the link parked for it rather than reconnecting — a
        # PER_SESSION cuff refuses that second connect.
        if paired_session is not None:
            if not coordinator.poll_coordinator:
                # Nothing will ever adopt the link, so do not park it.
                await paired_session.aclose()
            else:
                try:
                    await run_post_pairing_poll(
                        coordinator.hass,
                        service_info.address,
                        paired_session,
                        coordinator.poll_coordinator,
                    )
                except Exception as err:
                    _LOGGER.error("Post-pairing refresh failed: %s", err)

    coordinator.hass.async_create_task(_run_auto_session())

    return update


async def async_setup_entry(hass: HomeAssistant, entry: OmronConfigEntry) -> bool:
    """Set up Omron Bluetooth from a config entry."""
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    address = entry.unique_id
    assert address is not None
    if not async_ble_device_from_address(hass, address):
        _LOGGER.debug(
            "Could not find Omron device with address %s during setup; continuing without initial data",
            address,
        )

    # Get device model from config entry data (see DEFAULT_DEVICE_MODEL for fallback)
    device_model = entry.data.get(CONF_DEVICE_MODEL, DEFAULT_DEVICE_MODEL)

    slot_aliases = aliases_dict_from_entry(entry)
    data = OmronBluetoothDeviceData(
        device_model=device_model,
        user_aliases=slot_aliases,
    )
    hass.data[DOMAIN][entry.entry_id] = {}
    hass.data[DOMAIN][entry.entry_id]['address'] = address
    hass.data[DOMAIN][entry.entry_id]['data'] = data
    # Seed the advertisement-trigger cooldown so a lingering pairing-mode
    # advertisement arriving moments after the config-flow finishes does not
    # cause process_service_info to fire another auto-pairing session against
    # a device that was just paired.
    hass.data[DOMAIN][entry.entry_id]['last_attempt_time'] = time.time()
    # Per-entry serialization lock for BLE GATT sessions. All paths that open
    # a BLE link (scheduled poll, advertisement-triggered auto-session, deferred
    # pairing) try-acquire this lock and bail out immediately if it is held —
    # never queue. Two concurrent BLE connections to the same Omron device
    # cause SMP auth failures (proxy log: "auth fail reason=97/102").
    hass.data[DOMAIN][entry.entry_id]['session_lock'] = asyncio.Lock()

    # Ensure device registry entry exists even before first successful poll.
    device_registry = dr.async_get(hass)
    identifier = address.replace(":", "")[-4:].upper()
    device_name = f"{device_model} {identifier}"
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(CONNECTION_BLUETOOTH, address)},
        manufacturer="Omron",
        model=device_model,
        name=device_name,
    )

    bt_coordinator = OmronBluetoothProcessorCoordinator(
        hass,
        _LOGGER,
        address=address,
        mode=BluetoothScanningMode.PASSIVE,
        update_method=partial(process_service_info, entry),
        device_data=data,
        connectable=True,
    )
    connection_coordinator = DataUpdateCoordinator[bool](
        hass,
        _LOGGER,
        name=f"{DOMAIN}_connection_{address}",
    )
    duration_coordinator = DataUpdateCoordinator[float | None](
        hass,
        _LOGGER,
        name=f"{DOMAIN}_duration_{address}",
    )
    connection_coordinator.async_set_updated_data(False)
    duration_coordinator.async_set_updated_data(None)
    hass.data[DOMAIN][entry.entry_id]["connection_coordinator"] = connection_coordinator
    hass.data[DOMAIN][entry.entry_id]["duration_coordinator"] = duration_coordinator

    async def _async_poll_data(hass: HomeAssistant, entry: OmronConfigEntry) -> SensorUpdate:
        entry_data = hass.data[DOMAIN][entry.entry_id]
        address = entry_data["address"]
        preconnected_session = None
        handed_off = False
        try:
            device = async_ble_device_from_address(hass, address)
            if not device:
                _LOGGER.debug("BLE device not found; keeping last successful poll data")
                if poll_coordinator.data is not None:
                    return poll_coordinator.data
                _LOGGER.debug(
                    "BLE device not found and no cached poll data exists yet; "
                    "returning empty update until device is discovered again"
                )
                return entry.runtime_data.device_data._finish_update()
            coordinator = entry.runtime_data

            # Profiles that drop their bond every session cannot read on a timer:
            # the cuff refuses a new bond once it leaves pairing mode, so a
            # poll fired only because the interval elapsed spends a connect,
            # a bond attempt and the session lock to reach a failure that was
            # certain before it started. Let the device say when a read can
            # work — pairing_mode (a bond can be made now) or forced_transfer
            # (a measurement is waiting) — instead of guessing on the clock.
            #
            # A parked pairing session is exempt: that link is already open
            # and bonded, and skipping would strand it.
            device_data = coordinator.device_data
            # Peeked, not consumed: a request must survive every path that
            # bails out before a connect is actually attempted, or pressing
            # the button while a session is in flight throws the press away.
            poll_request = peek_poll_request(entry_data)
            flags_at = getattr(device_data, "last_msd_monotonic", None)
            if should_skip_scheduled_poll(
                device_data.device_config,
                pairing_mode=device_data.pairing_mode,
                forced_transfer=device_data.forced_transfer,
                flags_age=None if flags_at is None else time.monotonic() - flags_at,
                poll_request=poll_request,
                handoff_parked=has_handoff_session(hass, address),
            ):
                # First skip of a run says so at INFO: from the outside this
                # looks like a sensor that quietly stopped updating, and at
                # DEBUG the reason is invisible in a default log. Subsequent
                # skips stay at DEBUG so a cuff left idle does not repeat it
                # every scan interval.
                log = (
                    _LOGGER.debug
                    if entry_data.get("poll_gate_waiting")
                    else _LOGGER.info
                )
                entry_data["poll_gate_waiting"] = True
                log(
                    "Skipping scheduled poll for %s (%s): the cuff is not "
                    "advertising pairing mode or pending data, and this "
                    "profile needs a fresh bond for every session. Put it in "
                    "pairing mode (blinking -P-) to read now",
                    address,
                    device_data.device_config.model,
                )
                if poll_coordinator.data is not None:
                    return poll_coordinator.data
                return entry.runtime_data.device_data._finish_update()

            if entry_data.pop("poll_gate_waiting", False):
                _LOGGER.info(
                    "Polling %s again: the cuff is offering a window", address
                )

            session_lock: asyncio.Lock = entry_data["session_lock"]

            # Try-acquire only — if another BLE session is in flight (e.g. an
            # advertisement-triggered auto-pairing started moments ago), skip
            # this scheduled poll and serve cached data. The next interval (or
            # a request_refresh from the active session) will retry once the
            # lock frees. Two concurrent connections to the same Omron device
            # provoke SMP auth failures, so we never queue here.
            if session_lock.locked():
                _LOGGER.debug("Skipping scheduled poll: BLE session lock held for %s", address)
                if poll_coordinator.data is not None:
                    return poll_coordinator.data
                return entry.runtime_data.device_data._finish_update()

            async with session_lock:
                # Committed to connecting, so the request has now been served.
                # Consuming any earlier would discard it on a path that never
                # touched the radio — the session-lock skip above being the
                # one a user hits by pressing the button twice.
                if poll_request is not None:
                    entry_data.pop("poll_request", None)
                    _LOGGER.debug(
                        "Polling %s on a %s request", address, poll_request
                    )
                # Adopt a parked pairing/setup session (memory readout still
                # open) so pairing, time sync, and the first EEPROM read share
                # one connection. Taken here rather than at the top of the
                # function so the skip paths above leave it parked for the
                # retry instead of closing a link they never used.
                preconnected_session = adopt_handoff_session(hass, address)
                async with omron_poll_ble_telemetry(entry_data):
                    handed_off = True
                    async with asyncio.timeout(POLL_TIMEOUT_SECONDS):
                        result = await coordinator.device_data.async_poll(
                            device, preconnected_session=preconnected_session
                        )
                prev_data = poll_coordinator.data
                if prev_data is not None:
                    result = _merge_poll_sensor_update(prev_data, result)
                return result
        except TimeoutError:
            # Only our own deadline surfaces here: async_poll handles every
            # Exception internally, so nothing else escapes it as a timeout.
            # Warn rather than debug — this is the one symptom a user sees when
            # the BLE stack stops responding, and it used to be invisible.
            _LOGGER.warning(
                "Poll for %s exceeded %d s and was cancelled; the BLE stack "
                "stopped responding mid-poll. Serving cached data — the session "
                "lock is released, so the next poll can retry",
                address,
                POLL_TIMEOUT_SECONDS,
            )
            if poll_coordinator.data is not None:
                return poll_coordinator.data
            return entry.runtime_data.device_data._finish_update()
        except Exception as err:
            _LOGGER.debug("polling error; keeping last successful poll data: %s", err)
            if poll_coordinator.data is not None:
                return poll_coordinator.data
            return entry.runtime_data.device_data._finish_update()
        finally:
            if not handed_off and preconnected_session is not None:
                try:
                    # release_for_handoff() cleared the disconnect
                    # responsibility, so take it back or aclose() leaves the
                    # link up.
                    preconnected_session.reclaim_ownership()
                    await preconnected_session.aclose()
                except Exception:
                    pass

    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, 300)
    )

    poll_coordinator = DataUpdateCoordinator[SensorUpdate](
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=partial(_async_poll_data, hass, entry),
        update_interval=timedelta(seconds=scan_interval),
    )
    
    entry.runtime_data = bt_coordinator
    entry.runtime_data.poll_coordinator = poll_coordinator
    # Give the radio a moment in case a setup-flow BLE link was just torn down
    # — initial registration triggers async_setup_entry within ~20 ms of the
    # config-flow disconnect, before the device is ready to accept a new
    # connection. 0.5 s is cheap insurance on reloads/restarts too.
    await asyncio.sleep(0.5)
    await poll_coordinator.async_refresh()
    if not poll_coordinator.last_update_success:
        _LOGGER.warning(
            "Initial poll update failed for %s; entities will use cached/empty state: %s",
            address,
            poll_coordinator.last_exception,
        )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # only start after all platforms have had a chance to subscribe
    _register_advertisement_observer(hass, entry, address)
    entry.async_on_unload(bt_coordinator.async_start())
    entry.async_on_unload(entry.add_update_listener(update_listener))
    return True

async def update_listener(hass: HomeAssistant, entry: OmronConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: OmronConfigEntry) -> bool:
    """Unload a config entry."""
    # A pairing session parked for a poll that never came would otherwise keep
    # its BLE link past the unload, with nothing left to adopt or close it.
    address = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("address")
    if address:
        await discard_handoff_session(hass, address)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
