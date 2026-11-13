"""The ``omron.reassign_measurement`` service: move a handled measurement to another user.

Registered once for the domain. A measurement is named by the id the
``omron_unassigned_measurement`` event and the user sensors' attributes carry;
the new user is the user's sub-device, or nothing to un-assign it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    DOMAIN,
    EVENT_MEASUREMENT_REASSIGNED,
    SERVICE_REASSIGN_MEASUREMENT,
)
from .data import signal_user_updated
from .entity import user_device_identifier
from .history import async_entry_device
from .users import user_subentries

if TYPE_CHECKING:
    from .types import OmronConfigEntry

ATTR_MEASUREMENT_ID = "measurement_id"
ATTR_USER = "user"

REASSIGN_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MEASUREMENT_ID): cv.string,
        vol.Optional(ATTR_USER): vol.Any(None, cv.string),
    }
)


def _error(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders,
    )


def _loaded_entries(hass: HomeAssistant) -> list[OmronConfigEntry]:
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
        and getattr(entry, "runtime_data", None) is not None
        and entry.runtime_data.users is not None
    ]


def _owner_of_user_device(
    hass: HomeAssistant, device_id: str
) -> tuple[OmronConfigEntry, str] | None:
    """The entry and subentry a user sub-device belongs to, if it is one."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for entry in hass.config_entries.async_entries(DOMAIN):
        for subentry_id in user_subentries(entry):
            if (
                user_device_identifier(entry.entry_id, subentry_id)
                in device.identifiers
            ):
                return entry, subentry_id
    return None


async def async_reassign_measurement(
    hass: HomeAssistant, measurement_id: str, user_device_id: str | None
) -> None:
    """Move one logged measurement; raises ``ServiceValidationError`` if it cannot."""
    found = next(
        (
            (entry, logged)
            for entry in _loaded_entries(hass)
            if (logged := entry.runtime_data.users.find_logged(measurement_id))
            is not None
        ),
        None,
    )
    if found is None:
        raise _error("unknown_measurement", measurement_id=measurement_id)
    entry, _ = found

    new_user_id: str | None = None
    if user_device_id:
        owner = _owner_of_user_device(hass, user_device_id)
        if owner is None:
            raise _error("not_a_user")
        if owner[0].entry_id != entry.entry_id:
            raise _error("user_other_device")
        new_user_id = owner[1]

    runtime = entry.runtime_data
    # With the poll's lock: it assigns and saves the same state.
    async with runtime.history_lock:
        users = runtime.users
        logged = users.find_logged(measurement_id) if users is not None else None
        if users is None or logged is None or runtime.unloading:
            raise _error("unknown_measurement", measurement_id=measurement_id)
        old_user_id = logged["user"]
        if old_user_id == new_user_id:
            return
        affected = users.reassign(logged, new_user_id)
        await users.async_save()
        for user_id in affected:
            async_dispatcher_send(hass, signal_user_updated(entry.entry_id, user_id))
        names = _user_names(entry)
        device = async_entry_device(hass, entry)
        hass.bus.async_fire(
            EVENT_MEASUREMENT_REASSIGNED,
            {
                "device_id": device.id if device else None,
                "address": runtime.address,
                "measurement_id": measurement_id,
                "previous_user_id": old_user_id,
                "previous_user": names.get(old_user_id),
                "user_id": new_user_id,
                "user": names.get(new_user_id),
                "measured_at": logged["measured_at"],
                "device_time": logged["device_time"],
                "sequence": logged["sequence"],
                **logged["values"],
            },
        )


def _user_names(entry: OmronConfigEntry) -> dict[str | None, str]:
    return {
        subentry_id: subentry.title
        for subentry_id, subentry in user_subentries(entry).items()
    }


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the domain's services (once; they outlive any one entry)."""

    async def handle_reassign(call: ServiceCall) -> None:
        data: dict[str, Any] = call.data
        await async_reassign_measurement(
            hass, data[ATTR_MEASUREMENT_ID], data.get(ATTR_USER)
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REASSIGN_MEASUREMENT,
        handle_reassign,
        schema=REASSIGN_SCHEMA,
    )
