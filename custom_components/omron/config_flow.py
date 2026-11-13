"""Config flow for Omron Bluetooth integration."""

from __future__ import annotations

from collections.abc import Mapping
import dataclasses
import datetime as dt
import logging
import traceback
from typing import Any

from .omron_ble import OmronBluetoothDeviceData as DeviceData
from .omron_ble.devices import (
    DeviceConfig,
    HostPairingMode,
    get_device_config,
    get_supported_model_stats,
    get_supported_models,
    ambiguous_model_candidates,
    infer_model_id_from_local_name,
    resolve_profile_model_id,
)
from .omron_ble.util import slugify_for_entity_key
import voluptuous as vol

from homeassistant.components import onboarding
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
    async_ble_device_from_address,
)
from homeassistant.core import callback
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_ADDRESS, CONF_SCAN_INTERVAL
from homeassistant.data_entry_flow import AbortFlow, section
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    ObjectSelector,
    ObjectSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TimeSelector,
)

from .alias_migration import async_migrate_alias_unique_ids
from .util import aliases_dict_from_entry
from .session_handoff import (
    stash_handoff_session,
    close_probe_session,
    discard_probe_session,
    stash_probe_session,
    take_probe_session,
)
from .assignment import format_windows, parse_windows, windows_overlap
from .const import (
    CONF_ASSIGNMENT_STRATEGY,
    CONF_BINDKEY,
    CONF_DEVICE_MODEL,
    CONF_SYNC_STORED_RECORDS,
    CONF_TRANSPORT_CREDENTIAL,
    CONF_USER_ALIASES,
    CONF_USER_DIA_MAX,
    CONF_USER_DIA_MIN,
    CONF_USER_EXPECTED_WEIGHT,
    CONF_USER_HEIGHT,
    CONF_USER_NAME,
    CONF_USER_SYS_MAX,
    CONF_USER_SYS_MIN,
    CONF_USER_TIME_WINDOWS,
    CONF_USER_WEIGHT_MAX,
    CONF_USER_WEIGHT_MIN,
    CONF_WEIGHT_TOLERANCE_PCT,
    DOMAIN,
    STRATEGY_RULES,
    STRATEGY_WEIGHT,
    SUBENTRY_USER,
    WEIGHT_TOLERANCE_PCT_DEFAULT,
    WEIGHT_TOLERANCE_PCT_MAX,
    WEIGHT_TOLERANCE_PCT_MIN,
)
from .omron_ble.body_metrics import (
    HEIGHT_MAX_CM,
    HEIGHT_MIN_CM,
    WEIGHT_MAX_KG,
    WEIGHT_MIN_KG,
)
from .users import strategy_for, supports_users, user_subentries
from .omron_ble.const import DEFAULT_DEVICE_MODEL
from .omron_ble.bluez import _bluez_is_paired, _bluez_remove_device, is_local_adapter
from .omron_ble.session import OmronDeviceSession
from .omron_ble.pairing import (
    async_fetch_device_model_number,
    async_pair_and_sync_device,
)

_LOGGER = logging.getLogger(__name__)


def _resolved_user_aliases_from_input(
    num_users: int, user_input: dict[str, Any]
) -> list[str]:
    """Return display label per slot (1..num_users), with empty -> user{n}."""
    resolved: list[str] = []
    for i in range(1, num_users + 1):
        key = f"user_alias_{i}"
        raw = str(user_input.get(key, f"user{i}") or "").strip()
        resolved.append(raw if raw else f"user{i}")
    return resolved


def _user_aliases_are_unique(resolved: list[str]) -> bool:
    """True if aliases produce distinct entity-key slugs for every slot."""
    slugs = [
        slugify_for_entity_key(label) or f"user{index}"
        for index, label in enumerate(resolved, start=1)
    ]
    return len(set(slugs)) == len(slugs)


def _user_aliases_schema(
    num_users: int, defaults: dict[str, Any] | None = None
) -> vol.Schema:
    """Build voluptuous schema for user_alias_1..N."""
    defaults = defaults or {}
    fields: dict[Any, Any] = {}
    for i in range(1, num_users + 1):
        key = f"user_alias_{i}"
        def_val = str(defaults.get(key, f"user{i}") or f"user{i}")
        fields[vol.Required(key, default=def_val)] = vol.All(str, vol.Length(max=64))
    fields[vol.Optional("reselect_model", default=False)] = bool
    return vol.Schema(fields)


def _scan_interval_is_valid(value: int) -> bool:
    """0 turns scheduled polling off; otherwise at least a minute apart.

    Not in the schema: the form is serialised for the frontend, which cannot
    render a ``vol.Any`` of a literal and a range, so the form would not load.
    """
    return value == 0 or value >= 60


def _options_init_schema(
    entry: ConfigEntry,
    cfg: DeviceConfig,
    *,
    user_input: dict[str, Any] | None = None,
) -> vol.Schema:
    """Options form: scan interval plus multi-user alias fields when applicable."""
    interval_default: int
    if user_input is not None:
        interval_default = int(
            user_input.get(
                CONF_SCAN_INTERVAL,
                entry.options.get(
                    CONF_SCAN_INTERVAL,
                    entry.data.get(CONF_SCAN_INTERVAL, 300),
                ),
            )
        )
    else:
        interval_default = int(
            entry.options.get(
                CONF_SCAN_INTERVAL,
                entry.data.get(CONF_SCAN_INTERVAL, 300),
            )
        )
    fields: dict[Any, Any] = {
        vol.Required(CONF_SCAN_INTERVAL, default=interval_default): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=86400)
        ),
    }
    if cfg.supports_stored_record_sync:
        sync_default = bool(
            user_input.get(CONF_SYNC_STORED_RECORDS, False)
            if user_input is not None
            else entry.options.get(CONF_SYNC_STORED_RECORDS, False)
        )
        fields[vol.Required(CONF_SYNC_STORED_RECORDS, default=sync_default)] = bool
    if supports_users(entry) and not cfg.is_blood_pressure:
        # A scale can tell its users apart by weight or by rules; a cuff only
        # has rules, so it has nothing to choose.
        strategy_default = (
            user_input.get(CONF_ASSIGNMENT_STRATEGY)
            if user_input is not None and CONF_ASSIGNMENT_STRATEGY in user_input
            else strategy_for(entry, is_scale=True)
        )
        tolerance_default = float(
            user_input.get(CONF_WEIGHT_TOLERANCE_PCT, WEIGHT_TOLERANCE_PCT_DEFAULT)
            if user_input is not None
            else entry.options.get(
                CONF_WEIGHT_TOLERANCE_PCT, WEIGHT_TOLERANCE_PCT_DEFAULT
            )
        )
        fields[vol.Required(CONF_ASSIGNMENT_STRATEGY, default=strategy_default)] = (
            SelectSelector(
                SelectSelectorConfig(
                    options=[STRATEGY_WEIGHT, STRATEGY_RULES],
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="assignment_strategy",
                )
            )
        )
        fields[vol.Required(CONF_WEIGHT_TOLERANCE_PCT, default=tolerance_default)] = (
            vol.All(
                vol.Coerce(float),
                vol.Range(min=WEIGHT_TOLERANCE_PCT_MIN, max=WEIGHT_TOLERANCE_PCT_MAX),
            )
        )
    if cfg.num_users > 1:
        raw_aliases = entry.options.get(
            CONF_USER_ALIASES, entry.data.get(CONF_USER_ALIASES, {})
        )
        current_aliases: dict[str, Any] = (
            dict(raw_aliases) if isinstance(raw_aliases, dict) else {}
        )
        for i in range(1, cfg.num_users + 1):
            key = f"user_alias_{i}"
            if user_input is not None and key in user_input:
                prev = str(user_input.get(key, "") or "")
            else:
                prev = str(current_aliases.get(str(i), f"user{i}") or f"user{i}")
            fields[vol.Required(key, default=prev)] = vol.All(str, vol.Length(max=64))
    return vol.Schema(fields)


def _log_pairing_exception(prefix: str, exc: BaseException) -> None:
    """Emit structured detail for BLE pairing failures (BlueZ / D-Bus / Bleak)."""
    lines = [
        prefix,
        f"  type: {type(exc).__module__}.{type(exc).__name__}",
        f"  str: {exc!s}",
        f"  repr: {exc!r}",
    ]
    dbus_error = getattr(exc, "dbus_error", None)
    if dbus_error is not None:
        lines.append(f"  dbus_error: {dbus_error!s}")
    # Bleak / backend-specific (best-effort)
    for attr in (
        "dbus_path",
        "name",
        "details",
        "reply",
        "error_name",
        "error_message",
    ):
        val = getattr(exc, attr, None)
        if val is not None:
            lines.append(f"  {attr}: {val!r}")
    cause = exc.__cause__
    depth = 0
    while cause is not None and depth < 8:
        lines.append(
            f"  __cause__[{depth}]: "
            f"{type(cause).__module__}.{type(cause).__name__}: {cause!s}"
        )
        dbus_c = getattr(cause, "dbus_error", None)
        if dbus_c is not None:
            lines.append(f"    dbus_error: {dbus_c!s}")
        cause = cause.__cause__
        depth += 1
    ctx = exc.__context__
    if ctx is not None and ctx is not exc.__cause__:
        lines.append(
            f"  __context__: {type(ctx).__module__}.{type(ctx).__name__}: {ctx!s}"
        )
    _LOGGER.error("\n".join(lines))
    tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    _LOGGER.error("%s (full traceback)\n%s", prefix, "".join(tb_lines))


@dataclasses.dataclass
class Discovery:
    """A discovered bluetooth device."""

    title: str
    discovery_info: BluetoothServiceInfoBleak
    device: DeviceData


def _title(discovery_info: BluetoothServiceInfoBleak, device: DeviceData) -> str:
    return device.title or device.get_device_name() or discovery_info.name


class OmronConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Omron Bluetooth."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_device: DeviceData | None = None
        self._discovered_devices: dict[str, Discovery] = {}
        self._selected_model: str | None = None
        self._scan_interval: int = 300
        self._model_form: tuple[Any, ...] | None = None
        self._user_aliases: dict[str, str] = {}
        self._removed = False
        self._probed_model_name: str | None = None

    @callback
    def async_remove(self) -> None:
        """Close a model-probe link when this flow ends before pairing adopts it."""
        self._removed = True
        if self._discovery_info is None:
            return
        address = self._discovery_info.address
        self.hass.async_create_task(discard_probe_session(self.hass, address))

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        device = DeviceData()

        if not device.supported(discovery_info):
            return self.async_abort(reason="not_supported")

        title = _title(discovery_info, device)
        self.context["title_placeholders"] = {"name": title}
        self._discovery_info = discovery_info
        self._discovered_device = device

        return await self.async_step_bluetooth_confirm()

    async def async_step_select_model(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle device model selection step."""
        if user_input is not None:
            interval = user_input.get(CONF_SCAN_INTERVAL, 300)
            if not _scan_interval_is_valid(interval):
                # The form is always shown before it can be submitted.
                assert self._model_form is not None
                return self._show_model_form(
                    *self._model_form,
                    errors={CONF_SCAN_INTERVAL: "invalid_scan_interval"},
                    interval=interval,
                    chosen_model=user_input[CONF_DEVICE_MODEL],
                )
            self._selected_model = user_input[CONF_DEVICE_MODEL]
            self._scan_interval = interval

            # Update device data with selected model
            if self._discovered_device:
                self._discovered_device.device_model = self._selected_model
                title = _title(self._discovery_info, self._discovered_device)
                self.context["title_placeholders"] = {"name": title}
            cfg = get_device_config(self._selected_model)
            if cfg.num_users > 1:
                return await self.async_step_user_aliases()
            self._user_aliases = {}
            return await self.async_step_pairing()

        self._selected_model = None
        self._user_aliases = {}

        models = get_supported_models()
        model_dict = {m: m for m in models}
        stats = get_supported_model_stats()
        # No default until something identifies the device: the fallback is a
        # real profile, so confirming it reads the cuff through the wrong
        # EEPROM map with no error anywhere (#45).
        inferred_model: str | None = None
        probed_name: str | None = None

        # Check manufacturer data directly if available for model code (often in BLE beacons)
        # Or read from config flow context if we cached it.
        # However, for passive discovery, we only have local_name.
        if self._discovery_info:
            inferred = infer_model_id_from_local_name(self._discovery_info.name)
            if inferred is not None:
                inferred_model = inferred
            elif self._probed_model_name is not None:
                probed_name = self._probed_model_name
                inferred = infer_model_id_from_local_name(probed_name)
                if inferred:
                    inferred_model = inferred
            else:
                # If we cannot infer from the advertised model name, actively connect to read Model Number.
                ble_device = async_ble_device_from_address(
                    self.hass, self._discovery_info.address
                )
                if ble_device:
                    # Keep the link: on WLD3.0 cuffs this read is where the
                    # bond is made, and closing it here cuts SMP off before
                    # EDIV/Rand arrive (see async_fetch_device_model_number).
                    model_num, probe_session = await async_fetch_device_model_number(
                        ble_device, keep_session_open=True
                    )
                    if probe_session is not None:
                        if self._removed:
                            await probe_session.aclose()
                        else:
                            stash_probe_session(
                                self.hass,
                                self._discovery_info.address,
                                probe_session,
                            )
                    if model_num:
                        self._probed_model_name = model_num
                        probed_name = model_num
                        inferred = infer_model_id_from_local_name(model_num)
                        if inferred:
                            inferred_model = inferred

        # Nothing read over the air separates a shared name, so name the
        # candidates instead of saying "could not identify".
        candidates: tuple[str, ...] = ()
        if inferred_model is None:
            for name in (probed_name, getattr(self._discovery_info, "name", None)):
                candidates = ambiguous_model_candidates(name)
                if candidates:
                    break

        address = getattr(self._discovery_info, "address", "the device")
        if candidates:
            _LOGGER.warning(
                "%s reports the model name %r, which covers %d models that do "
                "not read alike: %s. Pick the one printed on the cuff -- "
                "nothing sent over the air distinguishes them",
                address,
                probed_name or self._discovery_info.name,
                len(candidates),
                ", ".join(candidates),
            )
        elif inferred_model is None:
            _LOGGER.warning(
                "Could not identify %s from its advertised name%s; the model has "
                "to be picked by hand. Choosing the wrong one reads the cuff "
                "through another model's memory map and reports no error",
                address,
                f" or its reported model number {probed_name!r}" if probed_name else "",
            )

        desc_ph = {
            **self.context.get("title_placeholders", {}),
            "model_total": str(stats["total"]),
            "profile_count": str(stats["profiles"]),
            "variant_count": str(stats["extra_variants"]),
            "probed_model": probed_name or "",
            "candidate_count": str(len(candidates)),
            # Markdown list: one model per line.
            "candidates": "\n".join(f"- **{c}**" for c in candidates),
        }

        # Three step ids, because only a step id gets its wording translated.
        if candidates:
            step_id = "select_model_ambiguous"
        elif inferred_model is None and probed_name:
            step_id = "select_model_unknown"
        else:
            step_id = "select_model"

        self._model_form = (step_id, desc_ph, inferred_model, model_dict)
        return self._show_model_form(*self._model_form, interval=self._scan_interval)

    def _show_model_form(
        self,
        step_id: str,
        desc_ph: dict[str, str],
        inferred_model: str | None,
        model_dict: dict[str, str],
        *,
        errors: dict[str, str] | None = None,
        interval: int | None = None,
        chosen_model: str | None = None,
    ) -> ConfigFlowResult:
        """The model form. Kept so a rejected interval re-shows it without re-probing.

        ``chosen_model`` keeps what the user picked, so a typo in the interval
        does not put the auto-detected model back.
        """
        if interval is None:
            interval = getattr(self, "_scan_interval", 300)
        default_model = chosen_model or inferred_model
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    # No default, so the choice is made rather than confirmed.
                    (
                        vol.Required(CONF_DEVICE_MODEL, default=default_model)
                        if default_model is not None
                        else vol.Required(CONF_DEVICE_MODEL)
                    ): vol.In(model_dict),
                    vol.Optional(CONF_SCAN_INTERVAL, default=interval): vol.All(
                        vol.Coerce(int), vol.Range(min=0, max=86400)
                    ),
                }
            ),
            description_placeholders=desc_ph,
            errors=errors,
        )

    async def async_step_select_model_unknown(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Model form after a probe returned a name that is in no table."""
        return await self.async_step_select_model(user_input)

    async def async_step_select_model_ambiguous(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Model form after a probe returned a name several models share."""
        return await self.async_step_select_model(user_input)

    def _require_selected_model(self) -> str:
        """The model chosen in select_model, which every later step depends on.

        Substituting a default here would put the device on a foreign EEPROM
        map without saying so -- the failure mode of #45. Every path into these
        steps runs select_model first, so this only fires if that stops being
        true.
        """
        if self._selected_model is None:
            raise AbortFlow("model_not_selected")
        return self._selected_model

    async def async_step_user_aliases(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect display names for each device user slot (multi-user models only)."""
        if user_input is not None and user_input.get("reselect_model"):
            if self._discovery_info:
                await discard_probe_session(self.hass, self._discovery_info.address)
            return await self.async_step_select_model()

        model = self._require_selected_model()
        cfg = get_device_config(model)
        if cfg.num_users <= 1:
            self._user_aliases = {}
            return await self.async_step_pairing()

        if user_input is not None:
            resolved = _resolved_user_aliases_from_input(cfg.num_users, user_input)
            if not _user_aliases_are_unique(resolved):
                title_ph = self.context.get("title_placeholders") or {}
                return self.async_show_form(
                    step_id="user_aliases",
                    data_schema=_user_aliases_schema(cfg.num_users, user_input),
                    errors={"base": "duplicate_user_aliases"},
                    description_placeholders={
                        "model": model,
                        "num_users": str(cfg.num_users),
                        "name": str(title_ph.get("name", model)),
                    },
                )
            self._user_aliases = {
                str(i): resolved[i - 1] for i in range(1, cfg.num_users + 1)
            }
            return await self.async_step_pairing()

        title_ph = self.context.get("title_placeholders") or {}
        return self.async_show_form(
            step_id="user_aliases",
            data_schema=_user_aliases_schema(cfg.num_users),
            description_placeholders={
                "model": model,
                "num_users": str(cfg.num_users),
                "name": str(title_ph.get("name", model)),
            },
        )

    async def async_step_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle device pairing step (classic -P- pairing flow)."""
        return await self._async_step_pairing(user_input)

    async def async_step_pairing_os(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle device pairing step (OS-level bonding; same logic, different UI strings)."""
        return await self._async_step_pairing(user_input)

    async def _async_step_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Shared pairing: form step_id must match async_step_<step_id> on the next POST."""
        if user_input is not None and user_input.get("reselect_model"):
            if self._discovery_info:
                await discard_probe_session(self.hass, self._discovery_info.address)
            return await self.async_step_select_model()

        errors: dict[str, str] = {}

        if user_input is not None:
            # User clicked submit - attempt to pair
            try:
                await self._async_do_pairing()
                return self._async_get_or_create_entry(model=self._selected_model)
            except ConnectionError as exc:
                _log_pairing_exception("Pairing failed (ConnectionError)", exc)
                errors["base"] = "pairing_failed"
            except Exception as exc:
                _log_pairing_exception("Unexpected error during pairing", exc)
                errors["base"] = "pairing_failed"

        model = self._require_selected_model()
        config = get_device_config(model)
        step_id = (
            "pairing_os"
            if config.host_pairing_mode == HostPairingMode.OS_BONDING
            else "pairing"
        )

        title_ph = self.context.get("title_placeholders") or {}
        device_name = str(title_ph.get("name") or model)
        interval_seconds = int(self._scan_interval)
        device_address = ""
        if self._discovery_info:
            device_address = self._discovery_info.address

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Optional("reselect_model", default=False): bool,
                }
            ),
            errors=errors,
            description_placeholders={
                "model": model,
                "device_name": device_name,
                "interval_seconds": str(interval_seconds),
                "device_address": device_address,
            },
        )

    async def _async_do_pairing(self) -> None:
        """Perform the actual BLE pairing with the device."""
        if not self._discovery_info:
            raise ConnectionError("No device discovered")

        model = self._require_selected_model()
        config = get_device_config(model)
        profile_key = resolve_profile_model_id(model)
        if model != profile_key:
            _LOGGER.debug(
                "Catalog variant %s -> profile %s",
                model,
                profile_key,
            )
        address = self._discovery_info.address
        advertised_services = self._discovery_info.service_uuids

        if advertised_services and not config.is_advertisement_compatible(
            advertised_services
        ):
            raise ConnectionError(
                f"Selected model {model} does not match advertised BLE services "
                f"(services={advertised_services})"
            )
        if advertised_services and not config.is_service_compatible(
            advertised_services
        ):
            _LOGGER.debug(
                "Advertisement lists standard BP service only; Omron service may appear "
                "after connect (model=%s ads=%s)",
                model,
                advertised_services,
            )

        # Get BLE device from HA's bluetooth stack
        ble_device = async_ble_device_from_address(self.hass, address)
        if not ble_device:
            raise ConnectionError(f"BLE device {address} not available")

        # Continue on the probe's link when it is still up: it is the one the
        # cuff bonded over, and reconnecting is what leaves the bond half made.
        #
        # Except when the profile bonds at connect. The probe opened its link
        # before the model was known, so it could not, and a link that has
        # already run discovery cannot be made to bond before it. Adopting it
        # would silently skip the connect-time bonding this profile asks for.
        # The one run whose retained-bond reconnect worked (2.7.8-beta.15) did
        # exactly this: a fresh connection with pair=True rather than the
        # probe's. The half-made bond the handoff avoids does not survive here
        # either, because the reconnect bonds again straight away.
        #
        # Local adapters only, matching the connect path: on a proxy no pair
        # request follows, so dropping the probe link would discard the one the
        # bond was being made over and replace it with an identical connect.
        session = take_probe_session(self.hass, address)
        if (
            session is not None
            and config.pair_on_connect
            and is_local_adapter(ble_device)
        ):
            _LOGGER.debug(
                "Reconnecting to %s to bond before service discovery, which the "
                "model-number link could not do",
                address,
            )
            await close_probe_session(session, address)
            session = None
        if session is None and is_local_adapter(ble_device):
            if await _bluez_is_paired(ble_device, all_adapters=True):
                _LOGGER.warning(
                    "Stale BlueZ bond detected for %s before pairing; "
                    "removing bond to ensure a clean pairing",
                    address,
                )
                await _bluez_remove_device(ble_device, all_adapters=True)
        if session is not None:
            session = OmronDeviceSession.adopt(
                session.release_client(), config, pairing_session=True
            )
            session.reclaim_ownership()
            _LOGGER.debug(
                "Pairing over the link opened to read the model number for %s",
                address,
            )
        else:
            session = OmronDeviceSession(ble_device, config, pairing_session=True)
        try:
            await session.connect()
            await async_pair_and_sync_device(
                session, model, leave_memory_session_open=True
            )
        except Exception:
            # Pairing failed: drop the link so a later retry starts clean.
            await session.aclose()
            if is_local_adapter(ble_device) and await _bluez_is_paired(
                ble_device, all_adapters=True
            ):
                _LOGGER.warning(
                    "Pairing failed for %s and BlueZ bond exists; clearing bond for retry",
                    address,
                )
                await _bluez_remove_device(ble_device, all_adapters=True)
            raise
        else:
            # Pairing succeeded: hand the still-open session to the first poll
            # with the memory readout session left open so setup does not
            # close-then-immediately-reopen on the same link (which breaks
            # GATT on some stacks). The poll path closes it when finished.
            # Profiles whose transport keeps its own credential hand one back
            # here. Losing it costs the user another pass through the cuff's
            # pairing mode, so it goes into the entry with everything else.
            if session.new_credential is not None:
                self._transport_credential = session.new_credential.hex()
            await stash_handoff_session(self.hass, address, session)

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm discovery."""
        if user_input is not None or not onboarding.async_is_onboarded(self.hass):
            return await self.async_step_select_model()

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=self.context["title_placeholders"],
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user step to pick discovered device."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()
            discovery = self._discovered_devices[address]

            self.context["title_placeholders"] = {"name": discovery.title}

            self._discovery_info = discovery.discovery_info
            self._discovered_device = discovery.device

            return await self.async_step_select_model()

        current_addresses = self._async_current_ids(include_ignore=False)
        for discovery_info in async_discovered_service_info(self.hass, False):
            address = discovery_info.address
            if address in current_addresses or address in self._discovered_devices:
                continue
            device = DeviceData()
            if device.supported(discovery_info):
                self._discovered_devices[address] = Discovery(
                    title=_title(discovery_info, device),
                    discovery_info=discovery_info,
                    device=device,
                )

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        titles = {
            address: discovery.title
            for (address, discovery) in self._discovered_devices.items()
        }
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(titles)}),
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle a flow initialized by a reauth event."""
        device: DeviceData = entry_data["device"]
        self._discovered_device = device

        self._discovery_info = device.last_service_info

        return self.async_abort(reason="reauth_successful")

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
        """Create the options flow."""
        return OmronOptionsFlowHandler(config_entry)

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Users, for a device that cannot tell people apart (#301)."""
        if supports_users(config_entry):
            return {SUBENTRY_USER: OmronUserSubentryFlow}
        return {}

    def _async_get_or_create_entry(
        self, bindkey: str | None = None, model: str | None = None
    ) -> ConfigFlowResult:
        data: dict[str, Any] = {}
        if bindkey:
            data[CONF_BINDKEY] = bindkey
        if model:
            data[CONF_DEVICE_MODEL] = model

        data[CONF_SCAN_INTERVAL] = int(getattr(self, "_scan_interval", 300))
        data[CONF_USER_ALIASES] = dict(getattr(self, "_user_aliases", {}))
        credential = getattr(self, "_transport_credential", None)
        if credential:
            data[CONF_TRANSPORT_CREDENTIAL] = credential
        elif self.source == SOURCE_REAUTH:
            # Re-auth rebuilds data from scratch; a pairing that established no
            # new credential must not drop the stored one.
            existing = self._get_reauth_entry().data.get(CONF_TRANSPORT_CREDENTIAL)
            if existing:
                data[CONF_TRANSPORT_CREDENTIAL] = existing

        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data=data
            )

        return self.async_create_entry(
            title=self.context["title_placeholders"]["name"],
            data=data,
        )


class OmronOptionsFlowHandler(OptionsFlow):
    """Handle options flow for Omron."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        try:
            # Newer HA builds can accept config_entry in base __init__.
            super().__init__(config_entry)
        except TypeError:
            # Older/newer variants may expose object.__init__ style signature.
            super().__init__()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        entry = self._config_entry
        model = entry.data.get(CONF_DEVICE_MODEL, DEFAULT_DEVICE_MODEL)
        cfg = get_device_config(model)

        if user_input is not None:
            if not _scan_interval_is_valid(user_input[CONF_SCAN_INTERVAL]):
                return self.async_show_form(
                    step_id="init",
                    data_schema=_options_init_schema(entry, cfg, user_input=user_input),
                    errors={CONF_SCAN_INTERVAL: "invalid_scan_interval"},
                    description_placeholders={"num_users": str(cfg.num_users)},
                )
            out: dict[str, Any] = {
                CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
            }
            if cfg.supports_stored_record_sync:
                out[CONF_SYNC_STORED_RECORDS] = bool(
                    user_input.get(CONF_SYNC_STORED_RECORDS, False)
                )
            if supports_users(entry) and not cfg.is_blood_pressure:
                out[CONF_ASSIGNMENT_STRATEGY] = user_input.get(
                    CONF_ASSIGNMENT_STRATEGY, STRATEGY_WEIGHT
                )
                out[CONF_WEIGHT_TOLERANCE_PCT] = float(
                    user_input.get(
                        CONF_WEIGHT_TOLERANCE_PCT, WEIGHT_TOLERANCE_PCT_DEFAULT
                    )
                )
            if cfg.num_users > 1:
                resolved = _resolved_user_aliases_from_input(cfg.num_users, user_input)
                if not _user_aliases_are_unique(resolved):
                    return self.async_show_form(
                        step_id="init",
                        data_schema=_options_init_schema(
                            entry, cfg, user_input=user_input
                        ),
                        errors={"base": "duplicate_user_aliases"},
                        description_placeholders={
                            "num_users": str(cfg.num_users),
                        },
                    )
                out[CONF_USER_ALIASES] = {
                    str(i): resolved[i - 1] for i in range(1, cfg.num_users + 1)
                }
                # Keep each user's entities (and history) under the new alias.
                async_migrate_alias_unique_ids(
                    self.hass,
                    entry,
                    aliases_dict_from_entry(entry),
                    {i: resolved[i - 1] for i in range(1, cfg.num_users + 1)},
                    cfg.num_users,
                )
            else:
                out[CONF_USER_ALIASES] = {}
            return self.async_create_entry(title="", data=out)

        return self.async_show_form(
            step_id="init",
            data_schema=_options_init_schema(entry, cfg),
            description_placeholders={
                "num_users": str(cfg.num_users),
            },
        )


def _number(low: float, high: float, step: float, unit: str) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=low,
            max=high,
            step=step,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement=unit,
        )
    )


# Form step per kind of user input: a scale by weight, a scale by rules, a cuff.
_STEP_WEIGHT = "weight"
_STEP_RULES = "rules"
_STEP_PRESSURE = "pressure"
_RANGE_PAIRS = (
    (CONF_USER_WEIGHT_MIN, CONF_USER_WEIGHT_MAX),
    (CONF_USER_SYS_MIN, CONF_USER_SYS_MAX),
    (CONF_USER_DIA_MIN, CONF_USER_DIA_MAX),
)


# The form groups its fields in two sections; the stored subentry stays flat.
_SECTION_WHEN = "when"
_SECTION_RANGE = "usual_range"
_SECTIONS = (_SECTION_WHEN, _SECTION_RANGE)
# One time window is shown as two pickers; every further one is added to a list
# with the Add button. The list's items have these two fields.
_WINDOW_START = "window_start"
_WINDOW_END = "window_end"
_MORE_WINDOWS = "more_windows"
_ITEM_START = "start"
_ITEM_END = "end"
# Key of the selector's own translations (`selector.time_window.fields.*`).
_WINDOW_TRANSLATION_KEY = "time_window"
_WHEN_FIELDS = (_WINDOW_START, _WINDOW_END, _MORE_WINDOWS)
_MINUTES_PER_DAY = 24 * 60


def _user_schema(step_id: str) -> vol.Schema:
    """The fields of a user, which depend on how the device assigns."""
    fields: dict[Any, Any] = {
        vol.Required(CONF_USER_NAME): vol.All(str, vol.Length(max=64))
    }
    height = _number(HEIGHT_MIN_CM, HEIGHT_MAX_CM, 0.1, "cm")
    if step_id == _STEP_WEIGHT:
        fields[vol.Required(CONF_USER_EXPECTED_WEIGHT)] = _number(
            WEIGHT_MIN_KG, WEIGHT_MAX_KG, 0.1, "kg"
        )
        fields[vol.Optional(CONF_USER_HEIGHT)] = height
        return vol.Schema(fields)
    ranges: dict[Any, Any] = {}
    if step_id == _STEP_RULES:
        fields[vol.Optional(CONF_USER_HEIGHT)] = height
        for key in (CONF_USER_WEIGHT_MIN, CONF_USER_WEIGHT_MAX):
            ranges[vol.Optional(key)] = _number(WEIGHT_MIN_KG, WEIGHT_MAX_KG, 0.1, "kg")
    else:
        for key in (
            CONF_USER_SYS_MIN,
            CONF_USER_SYS_MAX,
            CONF_USER_DIA_MIN,
            CONF_USER_DIA_MAX,
        ):
            ranges[vol.Optional(key)] = _number(20, 300, 1, "mmHg")
    when = {
        vol.Optional(_WINDOW_START): TimeSelector(),
        vol.Optional(_WINDOW_END): TimeSelector(),
        vol.Optional(_MORE_WINDOWS): ObjectSelector(
            ObjectSelectorConfig(
                multiple=True,
                fields={
                    _ITEM_START: {"selector": TimeSelector(), "required": True},
                    _ITEM_END: {"selector": TimeSelector(), "required": True},
                },
                label_field=_ITEM_START,
                description_field=_ITEM_END,
                translation_key=_WINDOW_TRANSLATION_KEY,
            )
        ),
    }
    fields[vol.Required(_SECTION_WHEN)] = section(
        vol.Schema(when), {"collapsed": False}
    )
    fields[vol.Required(_SECTION_RANGE)] = section(
        vol.Schema(ranges), {"collapsed": True}
    )
    return vol.Schema(fields)


def _flatten(user_input: dict[str, Any]) -> dict[str, Any]:
    """The form input with its sections folded into one level."""
    flat = {k: v for k, v in user_input.items() if k not in _SECTIONS}
    for name in _SECTIONS:
        inner = user_input.get(name)
        if isinstance(inner, Mapping):
            flat.update(inner)
    return flat


def _errors_by_section(errors: dict[str, str]) -> dict[str, str]:
    """Errors of fields inside a section, moved onto the section itself.

    The frontend shows a form error only for a top-level field: a section
    renders its inner fields without the errors, so an error keyed by an inner
    field would be shown nowhere. The first error of a section is the one shown.
    """
    owner = dict.fromkeys(_WHEN_FIELDS, _SECTION_WHEN)
    owner.update({key: _SECTION_RANGE for pair in _RANGE_PAIRS for key in pair})
    moved: dict[str, str] = {}
    for key, error in errors.items():
        moved.setdefault(owner.get(key, key), error)
    return moved


def _picked_minutes(value: Any) -> int | None:
    """Minutes after midnight of a time picker's ``HH:MM[:SS]``; seconds dropped."""
    try:
        picked = dt.time.fromisoformat(str(value))
    except ValueError:
        return None
    return picked.hour * 60 + picked.minute


def _picker_text(minutes: int) -> str:
    minutes %= _MINUTES_PER_DAY
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


def _has_rules(data: Mapping[str, Any]) -> bool:
    return CONF_USER_TIME_WINDOWS in data or any(
        key in data for pair in _RANGE_PAIRS for key in pair
    )


class OmronUserSubentryFlow(ConfigSubentryFlow):
    """Add or edit one user of a single-slot device (#301).

    The fields follow the device's assignment strategy, which the options flow
    sets: an expected weight for the weight-based one, time windows and value
    ranges for the rule-based one.
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        # Confirmation steps still to show before saving (warnings that do not
        # block saving).
        self._warnings: list[str] = []
        self._placeholders: dict[str, str] = {}

    def _form_step(self) -> str:
        entry = self._get_entry()
        config = get_device_config(
            entry.data.get(CONF_DEVICE_MODEL, DEFAULT_DEVICE_MODEL)
        )
        if config.is_blood_pressure:
            return _STEP_PRESSURE
        if strategy_for(entry, is_scale=True) == STRATEGY_RULES:
            return _STEP_RULES
        return _STEP_WEIGHT

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._async_form(self._form_step(), user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._async_form(self._form_step(), user_input)

    async def async_step_weight(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._async_form(_STEP_WEIGHT, user_input)

    async def async_step_rules(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._async_form(_STEP_RULES, user_input)

    async def async_step_pressure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._async_form(_STEP_PRESSURE, user_input)

    async def async_step_confirm_no_rules(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._async_confirm(user_input)

    async def async_step_confirm_no_rules_shared(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._async_confirm(user_input)

    async def async_step_confirm_overlap(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._async_confirm(user_input)

    def _editing(self):
        return (
            self._get_reconfigure_subentry()
            if self.source == SOURCE_RECONFIGURE
            else None
        )

    async def _async_form(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> SubentryFlowResult:
        schema = _user_schema(step_id)
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = self._validate(user_input)
            if not errors:
                self._data = data
                self._warnings = self._collect_warnings(step_id, data)
                return await self._async_confirm(None)
            suggested = user_input
        else:
            suggested = self._suggested_values(step_id)
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    def _suggested_values(self, step_id: str) -> dict[str, Any]:
        """What the form shows for the user being edited, in the form's shape."""
        editing = self._editing()
        if editing is None:
            return {}
        data = dict(editing.data)
        data.setdefault(CONF_USER_NAME, editing.title)
        if step_id == _STEP_WEIGHT:
            return data
        suggested = {
            key: data[key] for key in (CONF_USER_NAME, CONF_USER_HEIGHT) if key in data
        }
        when: dict[str, str] = {}
        windows = parse_windows(data.get(CONF_USER_TIME_WINDOWS)) or []
        if windows:
            when[_WINDOW_START] = _picker_text(windows[0][0])
            when[_WINDOW_END] = _picker_text(windows[0][1])
        if windows[1:]:
            when[_MORE_WINDOWS] = [
                {_ITEM_START: _picker_text(start), _ITEM_END: _picker_text(end)}
                for start, end in windows[1:]
            ]
        suggested[_SECTION_WHEN] = when
        suggested[_SECTION_RANGE] = {
            key: data[key] for pair in _RANGE_PAIRS for key in pair if key in data
        }
        return suggested

    def _validate(
        self, user_input: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        errors: dict[str, str] = {}
        data = {k: v for k, v in _flatten(user_input).items() if v not in (None, "")}
        name = str(data.get(CONF_USER_NAME, "")).strip()
        data[CONF_USER_NAME] = name
        editing = self._editing()
        taken = {
            str(sub.data.get(CONF_USER_NAME, sub.title)).strip().casefold()
            for sub_id, sub in user_subentries(self._get_entry()).items()
            if editing is None or sub_id != editing.subentry_id
        }
        if not name or name.casefold() in taken:
            errors[CONF_USER_NAME] = "invalid_name"
        windows, window_error = self._picked_windows(
            data.pop(_WINDOW_START, None),
            data.pop(_WINDOW_END, None),
            data.pop(_MORE_WINDOWS, None),
        )
        if window_error:
            errors[_SECTION_WHEN] = window_error
        if windows:
            data[CONF_USER_TIME_WINDOWS] = format_windows(windows)
        for low_key, high_key in _RANGE_PAIRS:
            low, high = data.get(low_key), data.get(high_key)
            if low is not None and high is not None and low > high:
                errors[high_key] = "invalid_range"
        return data, _errors_by_section(errors)

    @staticmethod
    def _picked_windows(
        start: Any, end: Any, more: Any
    ) -> tuple[list[tuple[int, int]], str | None]:
        """The windows of the default pickers and the added list, and the first error.

        The default window comes first, then the added ones in their order. The
        default pair may be empty (only the added windows count then), but not
        half filled. Every window needs a start and an end that differ.
        """
        pairs: list[tuple[Any, Any]] = []
        if start is not None or end is not None:
            pairs.append((start, end))
        if more is None:
            more = []
        if not isinstance(more, list):
            return [], "time_window_incomplete"
        for item in more:
            if not isinstance(item, Mapping):
                return [], "time_window_incomplete"
            pairs.append((item.get(_ITEM_START), item.get(_ITEM_END)))
        windows: list[tuple[int, int]] = []
        error: str | None = None
        for first, last in pairs:
            first_min = None if first in (None, "") else _picked_minutes(first)
            last_min = None if last in (None, "") else _picked_minutes(last)
            if first_min is None or last_min is None:
                error = error or "time_window_incomplete"
            elif first_min == last_min:
                error = error or "time_window_empty"
            else:
                windows.append((first_min, last_min))
        return windows, error

    def _collect_warnings(self, step_id: str, data: dict[str, Any]) -> list[str]:
        """Things worth a second look that must not stop the user from saving."""
        warnings: list[str] = []
        if step_id == _STEP_WEIGHT:
            return warnings
        editing = self._editing()
        others = {
            sub_id: sub
            for sub_id, sub in user_subentries(self._get_entry()).items()
            if editing is None or sub_id != editing.subentry_id
        }
        if not _has_rules(data):
            ruleless = [
                str(sub.data.get(CONF_USER_NAME, sub.title))
                for sub in others.values()
                if not _has_rules(sub.data)
            ]
            if ruleless:
                # Two users that fit everything: nothing can ever be assigned.
                warnings.append("confirm_no_rules_shared")
                self._placeholders = {"others": ", ".join(ruleless)}
            else:
                warnings.append("confirm_no_rules")
        mine = parse_windows(data.get(CONF_USER_TIME_WINDOWS)) or []
        overlapping = [
            str(sub.data.get(CONF_USER_NAME, sub.title))
            for sub in others.values()
            if mine
            and windows_overlap(
                mine, parse_windows(sub.data.get(CONF_USER_TIME_WINDOWS)) or []
            )
        ]
        if overlapping:
            warnings.append("confirm_overlap")
            self._placeholders = {"others": ", ".join(overlapping)}
        return warnings

    async def _async_confirm(
        self, user_input: dict[str, Any] | None
    ) -> SubentryFlowResult:
        if user_input is not None and self._warnings:
            self._warnings.pop(0)
        if self._warnings:
            return self.async_show_form(
                step_id=self._warnings[0],
                data_schema=vol.Schema({}),
                description_placeholders={
                    "name": self._data[CONF_USER_NAME],
                    **self._placeholders,
                },
            )
        name = self._data[CONF_USER_NAME]
        editing = self._editing()
        if editing is not None:
            return self.async_update_and_abort(
                self._get_entry(), editing, title=name, data=self._data
            )
        return self.async_create_entry(title=name, data=self._data)
