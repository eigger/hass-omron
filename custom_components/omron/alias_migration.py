"""Keep a user's entities when the user's alias changes.

The alias is part of the unique id of the per-user measurement entities, so a
new alias used to leave the old entities behind and create new ones without
history or a restorable last value. The options flow calls this before it saves
the new aliases, and the entities keep their entity ids and recorded history.
"""

from __future__ import annotations

from collections.abc import Mapping

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import CONF_DEVICE_MODEL, DOMAIN
from .omron_ble.util import user_alias_slug_from_entity_key, user_slot_slug

_MEASUREMENT_DOMAINS = frozenset({"sensor", "binary_sensor"})
_TEMP_SUFFIX = "__alias_migration"


def async_migrate_alias_unique_ids(
    hass: HomeAssistant,
    entry: ConfigEntry,
    old_aliases: Mapping[int, str],
    new_aliases: Mapping[int, str],
    num_users: int,
) -> int:
    """Move per-user entities from the old alias slugs to the new ones.

    Returns how many entities were renamed. Entity ids are kept. A user's
    entities move together or not at all: if the new id of any of them is held
    by an entity that does not move (for example one left by an older alias),
    that user is left alone, so a user is never split across two sets of
    entities. Entities whose slug matches no user are not touched.
    """
    if entry.unique_id is None:
        return 0
    model = entry.data.get(CONF_DEVICE_MODEL, "")
    identifier = entry.unique_id.replace(":", "")[-4:].lower()
    prefix = f"{model.lower().replace('-', '_')}_{identifier}_none_"

    new_slug_by_old: dict[str, str] = {}
    for user in range(1, num_users + 1):
        old_slug = user_slot_slug(old_aliases, user)
        new_slug = user_slot_slug(new_aliases, user)
        if old_slug != new_slug:
            new_slug_by_old[old_slug] = new_slug
    if not new_slug_by_old:
        return 0

    registry = er.async_get(hass)
    moving: list[tuple[str, str, str]] = []
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.platform != DOMAIN or reg_entry.domain not in _MEASUREMENT_DOMAINS:
            continue
        if not reg_entry.unique_id.startswith(prefix):
            continue
        key = reg_entry.unique_id[len(prefix) :]
        old_slug = user_alias_slug_from_entity_key(key)
        if old_slug is None or old_slug not in new_slug_by_old:
            continue
        base = key[: -len(old_slug) - 1]
        moving.append(
            (
                reg_entry.entity_id,
                f"{prefix}{base}_{new_slug_by_old[old_slug]}",
                old_slug,
            )
        )

    # A user may move only if every target id is free, or is held by an entity
    # of a user that moves away too. Dropping one user can block another, so
    # repeat until nothing changes. This also keeps a chain from stopping
    # halfway.
    slot_of = {entity_id: slug for entity_id, _, slug in moving}
    moving_slots = set(slot_of.values())
    changed = True
    while changed:
        changed = False
        for entity_id, new_unique_id, slug in moving:
            if slug not in moving_slots:
                continue
            reg_entry = registry.async_get(entity_id)
            assert reg_entry is not None
            taken = registry.async_get_entity_id(
                reg_entry.domain, DOMAIN, new_unique_id
            )
            if taken is not None and slot_of.get(taken) not in moving_slots:
                moving_slots.discard(slug)
                changed = True
    candidates = {
        entity_id: new_unique_id
        for entity_id, new_unique_id, slug in moving
        if slug in moving_slots
    }

    # Two steps so that two users swapping aliases do not collide halfway.
    moves: list[tuple[str, str, str]] = []
    for entity_id, new_unique_id in candidates.items():
        reg_entry = registry.async_get(entity_id)
        assert reg_entry is not None
        moves.append((entity_id, reg_entry.unique_id, new_unique_id))
    for entity_id, old_unique_id, _ in moves:
        registry.async_update_entity(
            entity_id, new_unique_id=f"{old_unique_id}{_TEMP_SUFFIX}"
        )
    for entity_id, _, new_unique_id in moves:
        registry.async_update_entity(entity_id, new_unique_id=new_unique_id)
    return len(moves)
