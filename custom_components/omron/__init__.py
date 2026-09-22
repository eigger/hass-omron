"""The Omron Bluetooth integration."""

from __future__ import annotations

from functools import partial
import asyncio
import logging
import time

from blesession import SessionReports
from sensor_state_data import SensorDeviceClass as SSDSensorDeviceClass, SensorUpdate

from .session_handoff import (
    adopt_handoff_session,
    discard_handoff_session,
    discard_probe_session,
    omron_poll_ble_telemetry,
    poll_parked_session,
    run_post_pairing_poll,
)
from .omron_ble import OmronBluetoothDeviceData
from .omron_ble.const import DEFAULT_DEVICE_MODEL
from .omron_ble.devices import get_device_config
from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_last_service_info,
)
from homeassistant.const import Platform, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.util import dt as dt_util
from datetime import timedelta
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from .const import (
    CONF_DEVICE_MODEL,
    CONF_TRANSPORT_CREDENTIAL,
    DOMAIN,
)
from .data import OmronRuntimeData
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

# When a poll fails mid-flight, keep measurement history but drop stale RSSI
# unless this poll refreshed it (avoids showing outdated diagnostics).
_STALE_DROP_SENSOR_DEVICE_CLASSES: frozenset = frozenset({
    SSDSensorDeviceClass.SIGNAL_STRENGTH,
})
_STALE_DROP_BINARY_DEVICE_CLASSES: frozenset = frozenset()


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


def _persist_transport_credential(
    hass: HomeAssistant, entry: OmronConfigEntry, device_data
) -> None:
    """Store a credential a session just established, once.

    The parser keeps it in memory as well, so a poll that runs before the
    entry update lands still authenticates. Written only when it changed:
    an entry update reloads the integration.
    """
    credential = device_data.pending_credential
    if credential is None:
        return
    device_data.pending_credential = None
    encoded = credential.hex()
    if entry.data.get(CONF_TRANSPORT_CREDENTIAL) == encoded:
        return
    _LOGGER.debug("Storing a new transport credential for %s", entry.entry_id)
    # An entry update fires update_listener, which reloads the integration.
    # This one runs inside the coordinator's own update method, so a reload
    # here would tear down the coordinator mid-refresh. Nothing the reload
    # exists for has changed -- the credential is internal, and the parser
    # already holds it -- so mark it and let the listener skip.
    entry.runtime_data.credential_write = True
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_TRANSPORT_CREDENTIAL: encoded}
    )


def process_service_info(
    entry: OmronConfigEntry,
    service_info: BluetoothServiceInfoBleak,
) -> SensorUpdate:
    """Process a BluetoothServiceInfoBleak, running side effects and returning sensor data."""
    runtime = entry.runtime_data
    data = runtime.device_data
    update = data.update(service_info)
    hass = runtime.bt_coordinator.hass

    # 1. Only attempt active sessions when the device is connectable
    if not service_info.connectable:
        return update

    is_pairing = getattr(data, "pairing_mode", False)
    is_invalid_time = getattr(data, "invalid_time", False)
    is_forced_transfer = getattr(data, "forced_transfer", False)

    # Trigger sync only for explicit device flags. A poll coordinator being present
    # is not itself a reason to connect on every advertisement.
    is_sync_needed = (
        is_pairing
        or is_invalid_time
        or is_forced_transfer
    )
    if not is_sync_needed:
        return update

    _LOGGER.debug(
        "Advertisement flags for %s: pairing_mode=%s invalid_time=%s forced_transfer=%s",
        service_info.address,
        is_pairing,
        is_invalid_time,
        is_forced_transfer,
    )

    # 2. Never start a second BLE session. A forced-transfer advertisement is
    # different from ordinary pairing/time-sync chatter, though: it represents
    # measurement data the cuff is explicitly asking us to collect. Coalesce one
    # pending forced-transfer while the active session owns the lock and drain it
    # after that session completes.
    session_lock: asyncio.Lock = runtime.session_lock
    if session_lock.locked():
        if is_forced_transfer:
            if not runtime.pending_forced_transfer:
                runtime.pending_forced_transfer_baseline = data.last_readout_at

            runtime.pending_forced_transfer = True
            _LOGGER.debug(
                "BLE session lock held; latched forced-transfer trigger for %s",
                service_info.address,
            )

            pending_task = runtime.pending_forced_transfer_task
            if pending_task is None or pending_task.done():

                async def _drain_pending_forced_transfer() -> None:
                    try:
                        # Wait for exactly the one active BLE owner. Acquire and
                        # immediately release so a later explicit refresh cannot
                        # race the tail of the current session.
                        async with session_lock:
                            pass

                        if not runtime.pending_forced_transfer:
                            return

                        baseline = runtime.pending_forced_transfer_baseline

                        # If the in-flight poll itself published a measurement
                        # after this trigger was latched, that poll consumed the
                        # pending transfer. Do not create a duplicate connection.
                        if (
                            data.last_readout_at is not None
                            and data.last_readout_at != baseline
                        ):
                            runtime.pending_forced_transfer = False
                            runtime.pending_forced_transfer_baseline = None
                            _LOGGER.debug(
                                "Latched forced-transfer for %s was consumed by "
                                "the in-flight poll",
                                service_info.address,
                            )
                            return

                        poll_coordinator = runtime.poll_coordinator
                        if poll_coordinator is None:
                            _LOGGER.debug(
                                "Forced-transfer remains latched for %s; "
                                "poll coordinator is not ready yet",
                                service_info.address,
                            )
                            return

                        runtime.pending_forced_transfer = False
                        runtime.pending_forced_transfer_baseline = None
                        runtime.force_poll_after_lock = True
                        runtime.last_attempt_time = time.time()

                        _LOGGER.debug(
                            "Draining latched forced-transfer trigger for %s",
                            service_info.address,
                        )

                        # async_refresh, not async_request_refresh: the latter
                        # is debounced, so the refresh that consumed the flag
                        # would not have to be the one that set it -- and a
                        # scheduled poll picking it up would wait on the
                        # session lock instead of skipping, which is the
                        # queuing this device family answers with SMP auth
                        # failures. The lock is already free by here, so there
                        # is nothing left to debounce.
                        await poll_coordinator.async_refresh()

                        if not poll_coordinator.last_update_success:
                            runtime.pending_forced_transfer = True
                            runtime.pending_forced_transfer_baseline = (
                                data.last_readout_at
                            )
                            _LOGGER.warning(
                                "Latched forced-transfer poll failed for %s; "
                                "keeping the transfer pending for retry: %s",
                                service_info.address,
                                poll_coordinator.last_exception,
                            )

                    except Exception as err:
                        runtime.pending_forced_transfer = True
                        _LOGGER.error(
                            "Failed to drain latched forced-transfer for %s: %s",
                            service_info.address,
                            err,
                        )
                    finally:
                        runtime.pending_forced_transfer_task = None
                        runtime.force_poll_after_lock = False

                # Tracked on the runtime so async_unload_entry can cancel it:
                # this task blocks on the session lock, so a reload while a
                # poll is in flight would otherwise leave it to wake up and
                # drive a coordinator that no longer exists.
                runtime.pending_forced_transfer_task = hass.async_create_task(
                    _drain_pending_forced_transfer()
                )
        else:
            _LOGGER.debug(
                "BLE session lock held; skipping advertisement trigger for %s",
                service_info.address,
            )

        return update

    # 3. Enforce a shared cooldown between GATT session attempts
    now = time.time()
    last_attempt = runtime.last_attempt_time
    if now - last_attempt < POLL_COOLDOWN_SECONDS:
        _LOGGER.debug(
            "Skipping advertisement trigger for %s (cooldown active, last attempt %ds ago)",
            service_info.address,
            int(now - last_attempt),
        )
        return update

    async def _run_auto_session() -> None:
        # forced_transfer-only path has no direct BLE op here — it just kicks
        # the poll coordinator, which goes through async_poll_data and handles
        # its own lock acquisition. Don't hold the lock during request_refresh,
        # otherwise the child poll would see lock locked and return cached data.
        if is_forced_transfer and not is_pairing and not is_invalid_time:
            runtime.pending_forced_transfer = False
            runtime.pending_forced_transfer_baseline = None
            runtime.force_poll_after_lock = True
            runtime.last_attempt_time = time.time()

            _LOGGER.debug(
                "Triggering scheduled poll via forced-transfer flag for %s",
                service_info.address,
            )

            try:
                # Undebounced for the same reason as the latched path above:
                # force_poll_after_lock has to reach this refresh and no other.
                await runtime.poll_coordinator.async_refresh()

                if not runtime.poll_coordinator.last_update_success:
                    runtime.pending_forced_transfer = True
                    runtime.pending_forced_transfer_baseline = data.last_readout_at
                    _LOGGER.warning(
                        "Forced-transfer poll failed for %s; "
                        "keeping the transfer pending for retry: %s",
                        service_info.address,
                        runtime.poll_coordinator.last_exception,
                    )
            except Exception as err:
                runtime.pending_forced_transfer = True
                runtime.pending_forced_transfer_baseline = data.last_readout_at
                _LOGGER.error("Auto polling failed: %s", err)
            finally:
                runtime.force_poll_after_lock = False

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
        if runtime.poll_coordinator and await poll_parked_session(
            hass, service_info.address, runtime.poll_coordinator
        ):
            # Seed the cooldown as the session paths do, or a run of adverts
            # spawns this task again on every one of them.
            runtime.last_attempt_time = time.time()
            return

        action = "auto-pairing" if is_pairing else "time-sync"
        # Doubles as the "pairing succeeded" flag: set only once the cuff is
        # bonded, and holds the live link for the refresh below to adopt.
        paired_session = None
        try:
            async with session_lock:
                runtime.last_attempt_time = time.time()
                _LOGGER.debug(
                    "Starting %s session for %s (lock acquired)",
                    action,
                    service_info.address,
                )
                await asyncio.sleep(SETTLE_DELAY_SECONDS)
                ble_device = service_info.device
                if is_pairing:
                    async with omron_poll_ble_telemetry(hass, runtime, "pairing"):
                        paired_session = await data.async_retry_pairing(ble_device)
                else:  # is_invalid_time and not is_forced_transfer
                    async with omron_poll_ble_telemetry(hass, runtime, "time_sync"):
                        await data.async_sync_time(ble_device)
        except Exception as err:
            if is_pairing:
                _LOGGER.error("Auto pairing failed: %s", err)
            else:
                _LOGGER.error("Auto time sync failed: %s", err)

        # Lock auto-released by the context manager. The post-pairing poll runs
        # AFTER the release so async_poll_data can acquire it independently,
        # and adopts the link parked for it rather than reconnecting — a
        # PER_SESSION cuff refuses that second connect.
        if paired_session is not None:
            if not runtime.poll_coordinator:
                # Nothing will ever adopt the link, so do not park it.
                await paired_session.aclose()
            else:
                try:
                    await run_post_pairing_poll(
                        hass,
                        service_info.address,
                        paired_session,
                        runtime.poll_coordinator,
                    )
                except Exception as err:
                    _LOGGER.error("Post-pairing refresh failed: %s", err)

    hass.async_create_task(_run_auto_session())

    return update


async def async_poll_data(hass: HomeAssistant, entry: OmronConfigEntry) -> SensorUpdate:
    """One poll of the cuff. The scheduled coordinator and forced-transfer refreshes both call this."""
    runtime = entry.runtime_data
    address = runtime.address
    poll_coordinator = runtime.poll_coordinator
    preconnected_session = None
    handed_off = False
    try:
        # Consume the explicit forced-transfer marker before BLE discovery.
        # If the device disappears between its advertisement and this refresh,
        # the request must fail and be re-latched by the caller rather than
        # returning cached data as a successful poll or leaving a stale flag.
        force_poll_after_lock = runtime.force_poll_after_lock
        runtime.force_poll_after_lock = False

        device = async_ble_device_from_address(hass, address)
        if not device:
            if force_poll_after_lock:
                raise ConnectionError(
                    f"BLE device {address} disappeared before forced-transfer poll"
                )

            _LOGGER.debug("BLE device not found; keeping last successful poll data")
            if poll_coordinator.data is not None:
                return poll_coordinator.data
            _LOGGER.debug(
                "BLE device not found and no cached poll data exists yet; "
                "returning empty update until device is discovered again"
            )
            return runtime.device_data._finish_update()

        session_lock: asyncio.Lock = runtime.session_lock

        # Ordinary scheduled polls remain try-acquire-only. An explicit
        # forced-transfer refresh is different: a measurement is pending,
        # so it may wait behind exactly one active BLE owner. This still
        # guarantees only one cuff connection at a time.
        if session_lock.locked():
            if force_poll_after_lock:
                _LOGGER.debug(
                    "Forced-transfer poll waiting for active BLE session "
                    "to release lock for %s",
                    address,
                )
            else:
                _LOGGER.debug(
                    "Skipping scheduled poll: BLE session lock held for %s",
                    address,
                )
                if poll_coordinator.data is not None:
                    return poll_coordinator.data
                return runtime.device_data._finish_update()

        async with session_lock:
            # Adopt a parked pairing/setup session (memory readout still
            # open) so pairing, time sync, and the first EEPROM read share
            # one connection. Taken here rather than at the top of the
            # function so the skip paths above leave it parked for the
            # retry instead of closing a link they never used.
            preconnected_session = adopt_handoff_session(hass, address)
            async with omron_poll_ble_telemetry(hass, runtime, "poll"):
                handed_off = True
                async with asyncio.timeout(POLL_TIMEOUT_SECONDS):
                    result = await runtime.device_data.async_poll(
                        device, preconnected_session=preconnected_session
                    )
            _persist_transport_credential(hass, entry, runtime.device_data)
            runtime.readout_coordinator.async_set_updated_data(
                runtime.device_data.last_readout_at
            )
            prev_data = poll_coordinator.data
            if prev_data is not None:
                result = _merge_poll_sensor_update(prev_data, result)
            return result
    except TimeoutError:
        # DataUpdateCoordinator retains the previous data when its update
        # method raises. Propagate the timeout so HA records this refresh as
        # failed instead of reporting stale cached data as a successful poll.
        _LOGGER.warning(
            "Poll for %s exceeded %d s and was cancelled; the BLE stack "
            "stopped responding mid-poll. The coordinator will retain the "
            "last data and mark this refresh failed",
            address,
            POLL_TIMEOUT_SECONDS,
        )
        raise
    except Exception as err:
        # Preserve the previous coordinator data by letting HA do what the
        # DataUpdateCoordinator is designed to do on update failure. Returning
        # the cached SensorUpdate here incorrectly sets last_update_success=True.
        #
        # UpdateFailed rather than the raw error: a cuff that is asleep, out
        # of range, or refusing a connection outside its window is an
        # expected BLE failure, and the coordinator logs UpdateFailed once
        # and stays quiet afterwards. A bare exception reaches its generic
        # branch instead, which logs a traceback on every refresh and Home
        # Assistant renders as "this error originated from a custom
        # integration" -- for a device that is simply off (#133).
        _LOGGER.debug(
            "polling error; coordinator will retain last successful data: %s",
            err,
        )
        raise UpdateFailed(str(err) or type(err).__name__) from err
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


async def async_setup_entry(hass: HomeAssistant, entry: OmronConfigEntry) -> bool:
    """Set up Omron Bluetooth from a config entry."""
    # Domain dict is only the config-flow handoff buckets. Do not replace it:
    # a session parked during pairing has to still be here for the first poll.
    hass.data.setdefault(DOMAIN, {})
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
        get_tz=lambda: dt_util.DEFAULT_TIME_ZONE,
    )
    # Prime from the last cached advertisement, including a non-connectable
    # proxy sighting. MSD flags do not need a connection; connectable=True
    # drops the ESPHome proxy history this integration usually has, and the
    # prime becomes a silent no-op. With no cached advert, do not push:
    # the PassiveBluetooth restore keeps the last on/off.
    last_service_info = async_last_service_info(hass, address, connectable=False)
    if last_service_info is not None:
        data.update(last_service_info)
    # Transport credential for profiles whose unlock keeps its own key. Stored
    # hex; a malformed value is dropped rather than failing setup, which would
    # leave the user with no way back other than deleting the entry.
    stored_credential = entry.data.get(CONF_TRANSPORT_CREDENTIAL)
    if stored_credential:
        try:
            data.transport_credential = bytes.fromhex(stored_credential)
        except ValueError:
            _LOGGER.warning(
                "Ignoring an unreadable stored transport credential for %s; "
                "re-add the device while it is in pairing mode",
                address,
            )

    # Ensure device registry entry exists even before first successful poll.
    device_registry = dr.async_get(hass)
    identifier = address.replace(":", "")[-4:].upper()
    # display_model, to match what the advertisement path names the device
    # (parser._setup_device_info). A cuff configured as BP5465 was showing that
    # here and HEM-7382T1-AZAZ there (#91).
    display_model = get_device_config(device_model).display_model
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(CONNECTION_BLUETOOTH, address)},
        manufacturer="Omron",
        model=display_model,
        name=f"{display_model} {identifier}",
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

    def coordinator(purpose: str, initial):
        """A push-only coordinator seeded with `initial`, named for the log."""
        coord = DataUpdateCoordinator(
            hass, _LOGGER, name=f"{DOMAIN} {identifier} {purpose}"
        )
        coord.async_set_updated_data(initial)
        return coord

    connection_coordinator = coordinator("connection", False)
    duration_coordinator = coordinator("duration", None)
    readout_coordinator = coordinator("readout", None)
    failure_coordinator = coordinator("failure", None)
    failure_count_coordinator = coordinator("failure_count", 0)

    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, 300)
    )
    poll_coordinator = DataUpdateCoordinator[SensorUpdate](
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=partial(async_poll_data, hass, entry),
        update_interval=timedelta(seconds=scan_interval),
    )

    # Assigned before the first refresh and before advertisements start, so
    # both call into a runtime that already exists.
    entry.runtime_data = OmronRuntimeData(
        address=address,
        device_data=data,
        bt_coordinator=bt_coordinator,
        poll_coordinator=poll_coordinator,
        connection_coordinator=connection_coordinator,
        duration_coordinator=duration_coordinator,
        readout_coordinator=readout_coordinator,
        failure_coordinator=failure_coordinator,
        failure_count_coordinator=failure_count_coordinator,
        session_reports=SessionReports(),
        # Seed the advertisement-trigger cooldown so a lingering pairing-mode
        # advertisement arriving moments after the config-flow finishes does not
        # fire another auto-pairing session against a device that was just paired.
        last_attempt_time=time.time(),
    )

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

    # Processors are registered, so restore has already landed. Push only a
    # cached advertisement. With no sighting, leave the restored on/off.
    if last_service_info is not None:
        bt_coordinator.async_set_updated_data(data._finish_update())

    # only start after all platforms have had a chance to subscribe
    entry.async_on_unload(bt_coordinator.async_start())
    entry.async_on_unload(entry.add_update_listener(update_listener))
    return True


async def update_listener(hass: HomeAssistant, entry: OmronConfigEntry) -> None:
    """Handle options update."""
    # Scheduled before it runs. Unload deletes runtime_data only after
    # async_unload_entry returns, so a credential write that races a reload
    # or shutdown must not touch a runtime that is already gone.
    if not hasattr(entry, "runtime_data"):
        return
    runtime = entry.runtime_data
    if runtime.credential_write:
        # A credential a poll just stored. Reloading for it would drop the
        # coordinator that is still running the poll that produced it.
        runtime.credential_write = False
        _LOGGER.debug(
            "Skipping reload for %s: only the transport credential changed",
            entry.entry_id,
        )
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: OmronConfigEntry) -> bool:
    """Unload a config entry."""
    # A pairing session parked for a poll that never came would otherwise keep
    # its BLE link past the unload, with nothing left to adopt or close it.
    runtime = entry.runtime_data
    # Blocked on the session lock, so it can wake long after the unload and
    # drive a coordinator that is gone.
    pending_task = runtime.pending_forced_transfer_task
    if pending_task is not None and not pending_task.done():
        pending_task.cancel()
    await discard_handoff_session(hass, runtime.address)
    # Same for a model-number probe whose flow never reached pairing.
    await discard_probe_session(hass, runtime.address)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
