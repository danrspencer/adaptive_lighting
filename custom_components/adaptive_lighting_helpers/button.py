"""
A per-owner "Clear" button - the write-tracking dashboard card's
per-light Clear action, made available to stock Home Assistant.

Clearing an owner's tracked state is the documented escape hatch for a
light stuck "overridden" (see write_tracking.py's async_clear docstring
for how that can happen with no external cause). Until now it existed
only inside the custom card, which meant the one action you might
actually need in a hurry was the one thing you couldn't put on an
ordinary dashboard, into a script, or behind a physical button.

Optional, sharing CONF_OWNER_SENSORS with sensor.py's per-owner counters
- one toggle for the whole per-owner entity set rather than one per
platform, since wanting the counts and wanting the reset go together.

button rather than switch: this is a stateless "do it now" action with
nothing to turn back off, which is exactly what ButtonEntity is for. It
also means the entity's state is the last-pressed timestamp, so "when
did I last reset this room" ends up in history for free.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .sensor import owner_of, owner_slug, setup_owner_entities
from .write_tracking import SIGNAL_WRITE_TRACKING_UPDATED, LastWriteTracker


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    write_tracker: LastWriteTracker = hass.data[DOMAIN][entry.entry_id]
    setup_owner_entities(
        hass,
        entry,
        write_tracker,
        async_add_entities,
        lambda owner_id, device_info: [_OwnerClearButton(hass, entry, write_tracker, owner_id, device_info)],
    )


class _OwnerClearButton(ButtonEntity):
    """Discards every tracked record belonging to one owner.

    Deliberately all of them, not just the ones currently "overridden":
    "clear this room's tracked state" should be a guaranteed reset rather
    than one that depends on agreeing with classify() about which lights
    are stuck - and being stuck in a way classify() doesn't recognise is
    precisely when you reach for this.

    The cost is real and worth knowing: the owner's healthy lights lose
    their claims too, so each is unprotected until its next write, which
    is treated exactly like a brand-new entity's first write. For a live
    room automation that is one tick.

    Sits on its owner's device alongside that owner's two counters, so
    deleting a dead owner is one device delete rather than three separate
    entity deletions - see sensor.py's owner_device_info."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:broom"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        write_tracker: LastWriteTracker,
        owner_id: str,
        device_info: DeviceInfo,
    ) -> None:
        self.hass = hass
        self._write_tracker = write_tracker
        self._owner_id = owner_id
        self._attr_device_info = device_info
        # Keyed on the *full* owner_id for the same reason
        # _OwnerCountSensor is - see its comment.
        self._attr_unique_id = f"{entry.entry_id}_owner_{owner_id}_clear"
        slug = owner_slug(owner_id)
        # entity_id stays explicit so an existing install's ids don't
        # churn; a later device rename moves only the display name.
        self.entity_id = f"button.{slug}_adaptive_clear"
        self._attr_name = "Clear"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_WRITE_TRACKING_UPDATED, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        # Only `tracked` below can change, but it's the thing that tells
        # you whether pressing would do anything at all.
        self.async_write_ha_state()

    def _owned_entities(self) -> list[str]:
        return sorted(
            entity_id
            for entity_id, record in self._write_tracker.snapshot().items()
            if owner_of(record) == self._owner_id
        )

    async def async_press(self) -> None:
        # async_clear already saves the store and fires
        # SIGNAL_WRITE_TRACKING_UPDATED, so the counter sensors and the
        # card catch up on their own, and it's a no-op for an owner with
        # nothing tracked.
        await self._write_tracker.async_clear(self._owned_entities())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Says what a press would actually do before you press it.
        return {"owner_id": self._owner_id, "tracked": len(self._owned_entities())}
