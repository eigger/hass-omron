"""Config flow for Omron Bluetooth integration."""

from __future__ import annotations

from collections.abc import Mapping
import asyncio
import dataclasses
import datetime as dt
import logging
import traceback
from typing import Any

from .omron_ble import OmronBluetoothDeviceData as DeviceData
from .omron_ble.devices import (
    DEFAULT_DEVICE_MODEL,
    MODEL_VARIANT_MAP,
    get_device_config,
    get_supported_model_stats,
    get_supported_models,
    infer_model_id_from_local_name,
)
import voluptuous as vol

from bleak import BleakClient
from bleak_retry_connector import establish_connection

from homeassistant.components import onboarding
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
    async_ble_device_from_address,
)
from homeassistant.core import callback
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS, CONF_SCAN_INTERVAL

from .const import CONF_DEVICE_MODEL, DOMAIN
from .omron_ble.omron_driver import GattTransport, _bleak_refresh_services

_LOGGER = logging.getLogger(__name__)
CTS_CHARACTERISTIC_UUID = "00002a2b-0000-1000-8000-00805f9b34fb"


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
    _LOGGER.debug("%s (full traceback)\n%s", prefix, "".join(tb_lines))


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

        return await self.async_step_select_model()

    async def async_step_select_model(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle device model selection step."""
        if user_input is not None:
            self._selected_model = user_input[CONF_DEVICE_MODEL]
            self._scan_interval = user_input.get(CONF_SCAN_INTERVAL, 300)
            
            # Update device data with selected model
            if self._discovered_device:
                self._discovered_device.device_model = self._selected_model
                title = _title(self._discovery_info, self._discovered_device)
                self.context["title_placeholders"] = {"name": title}
            return await self.async_step_pairing()

        models = get_supported_models()
        model_dict = {m: m for m in models}
        stats = get_supported_model_stats()
        default_model = DEFAULT_DEVICE_MODEL
        if self._discovery_info:
            inferred = infer_model_id_from_local_name(self._discovery_info.name)
            if inferred is not None:
                default_model = inferred
        desc_ph = {
            **self.context.get("title_placeholders", {}),
            "model_total": str(stats["total"]),
            "profile_count": str(stats["profiles"]),
            "variant_count": str(stats["extra_variants"]),
        }

        return self.async_show_form(
            step_id="select_model",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICE_MODEL, default=default_model
                    ): vol.In(model_dict),
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=300
                    ): vol.All(vol.Coerce(int), vol.Range(min=60, max=86400)),
                }
            ),
            description_placeholders=desc_ph,
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

        model = self._selected_model or DEFAULT_DEVICE_MODEL
        config = get_device_config(model)
        step_id = "pairing_os" if config.supports_os_bonding_only else "pairing"

        title_ph = self.context.get("title_placeholders") or {}
        device_name = str(title_ph.get("name") or model)
        interval_seconds = int(self._scan_interval)
        device_address = ""
        if self._discovery_info:
            device_address = self._discovery_info.address

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({}),
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

        model = self._selected_model or DEFAULT_DEVICE_MODEL
        config = get_device_config(model)
        variant_entry = MODEL_VARIANT_MAP.get(model)
        if variant_entry:
            profile_key, variant = variant_entry
            _LOGGER.info(
                "Catalog variant %s -> profile %s (unverified=%s)",
                model,
                profile_key,
                variant.unverified,
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
        if advertised_services and not config.is_service_compatible(advertised_services):
            _LOGGER.info(
                "Advertisement lists standard BP service only; Omron service may appear "
                "after connect (model=%s ads=%s)",
                model,
                advertised_services,
            )

        # Get BLE device from HA's bluetooth stack
        ble_device = async_ble_device_from_address(self.hass, address)
        if not ble_device:
            raise ConnectionError(f"BLE device {address} not available")

        client = await establish_connection(BleakClient, ble_device, address)
        try:
            # Bleak requires an explicit service discovery before using client.services
            # (otherwise: BleakError "Service Discovery has not been performed yet").
            await _bleak_refresh_services(client)
            parent_uuid = config.parent_service_uuid
            for _ in range(20):
                if parent_uuid in [s.uuid for s in client.services]:
                    break
                await _bleak_refresh_services(client)
                await asyncio.sleep(0.25)

            transport = GattTransport(client, config)
            await transport.pair()
            await _bleak_refresh_services(client)
            await self._async_try_sync_current_time(client, model)
            _LOGGER.info("Successfully paired with %s (%s)", model, address)
        finally:
            if client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    async def _async_try_sync_current_time(self, client: BleakClient, model: str) -> None:
        """Try to sync local time to device via Current Time characteristic (CTS)."""
        char = client.services.get_characteristic(CTS_CHARACTERISTIC_UUID)
        if char is None:
            _LOGGER.debug(
                "Skipping time sync after pairing for %s: CTS characteristic not found",
                model,
            )
            return

        now = dt.datetime.now().astimezone()
        day_of_week = now.isoweekday()  # Monday=1 ... Sunday=7 (CTS format)
        # Bluetooth CTS payload (10 bytes):
        # year(2 LE), month, day, hour, minute, second, day_of_week, fractions256, adjust_reason
        payload = bytearray()
        payload += int(now.year).to_bytes(2, "little")
        payload += bytes(
            [
                now.month,
                now.day,
                now.hour,
                now.minute,
                now.second,
                day_of_week,
                0x00,  # Fractions256
                0x01,  # Adjust reason: manual time update
            ]
        )
        try:
            await client.write_gatt_char(CTS_CHARACTERISTIC_UUID, payload, response=True)
            _LOGGER.info(
                "Synced current time to %s via CTS: %s",
                model,
                now.isoformat(timespec="seconds"),
            )
        except Exception as exc:
            _LOGGER.warning("Failed to sync time via CTS for %s: %s", model, exc)

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
            await self.async_set_unique_id(address, raise_on_progress=False)
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

    def _async_get_or_create_entry(
        self, bindkey: str | None = None, model: str | None = None
    ) -> ConfigFlowResult:
        data: dict[str, Any] = {}
        if bindkey:
            data["bindkey"] = bindkey
        if model:
            data[CONF_DEVICE_MODEL] = model

        data[CONF_SCAN_INTERVAL] = int(
            getattr(self, "_scan_interval", 300)
        )

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
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Retrieve current setting, fallback to data, default 300
        current_interval = self._config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self._config_entry.data.get(CONF_SCAN_INTERVAL, 300)
        )

        options_schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=current_interval,
                ): vol.All(vol.Coerce(int), vol.Range(min=60, max=86400)),
            }
        )

        return self.async_show_form(step_id="init", data_schema=options_schema)
