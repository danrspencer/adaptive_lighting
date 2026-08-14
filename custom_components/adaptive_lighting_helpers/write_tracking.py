"""
Tracks which context.id this integration itself last used to write each
light entity, so grouping.py can tell "did WE make the last change to
this light" apart from any other cause - a person, another automation, a
device regaining power with its own fresh context. context.id (not
context.user_id) is the right signal: every service call made within
one automation run shares the same context.id (confirmed against HA
core's helpers/script.py - each run's Script._context is passed through
to every action step), while anything else - including a *different*
automation, which HA gives its own fresh Context() too - gets an
unrelated one. This is what lets the "leave alone" check catch the case
context.user_id couldn't: another automation (e.g. one triggered
directly by a physical button) setting a light carries no user_id
either, identical to our own writes under the old check.

Persisted via Store (not just in-memory) so a HA restart doesn't make
every already-on light look externally-set until it happens to change
again some other way.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

STORAGE_VERSION = 1
STORAGE_KEY = "adaptive_lighting_helpers.last_write_context_ids"


class LastWriteTracker:
    """entity_id -> context.id of the last light.turn_on/turn_off this
    integration issued for it, across every apply_lighting call
    regardless of which automation made it - deliberately not scoped
    per-caller, since "adaptive control wrote this most recently" is
    the only distinction grouping.py's override check needs."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, str]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict[str, str] = {}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {}

    def last_context_id(self, entity_id: str) -> str | None:
        return self._data.get(entity_id)

    async def async_record(self, entity_ids: list[str], context_id: str) -> None:
        """Called once per apply_lighting invocation with every entity it
        actually issued a light.turn_on/turn_off for - not entities it
        merely considered (already-at-target, unreachable, or currently
        externally-set are never passed here)."""
        if not entity_ids:
            return
        for entity_id in entity_ids:
            self._data[entity_id] = context_id
        await self._store.async_save(self._data)
