"""
A per-scope "Clear" button - discards a state device's tracked claims so
its lights are free to be taken again.

Clearing is the documented escape hatch for a light stuck "overridden"
(see write_tracking.py's async_clear docstring for how that happens with
no external cause). It lived only inside a custom dashboard card until
this existed, which meant the one action you might need in a hurry was
the one thing you couldn't put on an ordinary dashboard, into a script,
or behind a physical button.

button rather than switch: a stateless "do it now" action with nothing
to turn back off is exactly what ButtonEntity is for. It also means the
entity's state is the last-pressed timestamp, so "when did I last reset
this room" ends up in history for free.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import StateInstance, state_instances
from .write_tracking import SIGNAL_WRITE_TRACKING_UPDATED, ClaimRegistry


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    registry: ClaimRegistry = hass.data[DOMAIN][entry.entry_id]
    for instance in state_instances(entry):
        async_add_entities(
            [_ScopeClearButton(hass, registry, instance)], config_subentry_id=instance.subentry_id
        )


class _ScopeClearButton(ButtonEntity):
    """Discards every claim held by one state device.

    Deliberately all of them, not just the ones currently "overridden":
    "clear this room's tracked state" should be a guaranteed reset rather
    than one that depends on agreeing with classify() about which lights
    are stuck - and being stuck in a way classify() doesn't recognise is
    precisely when you reach for this.

    The cost is real and worth knowing: the scope's healthy lights lose
    their claims too, so each is unprotected until its next write, which
    is treated exactly like a brand-new entity's first write. For a live
    room automation that is one tick.
    """

    _attr_has_entity_name = True
    _attr_name = "Clear"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:broom"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, registry: ClaimRegistry, instance: StateInstance) -> None:
        self.hass = hass
        self._registry = registry
        self._instance = instance
        self._attr_unique_id = f"{instance.subentry_id}_clear"
        self.entity_id = f"button.{instance.prefix}flare_clear"
        self._attr_device_info = instance.device_info

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_WRITE_TRACKING_UPDATED, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        # Only `tracked` below can change, but it's the thing that tells
        # you whether pressing would do anything at all.
        self.async_write_ha_state()

    def _tracked(self) -> list[str]:
        return sorted(self._registry.records_for_scope(self._instance.subentry_id))

    async def async_press(self) -> None:
        # async_clear publishes the affected scopes and fires
        # SIGNAL_WRITE_TRACKING_UPDATED, so the counters catch up on
        # their own, and it's a no-op for a scope tracking nothing.
        await self._registry.async_clear(self._instance.subentry_id, self._tracked())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Says what a press would actually do before you press it.
        return {"tracked": len(self._tracked())}
