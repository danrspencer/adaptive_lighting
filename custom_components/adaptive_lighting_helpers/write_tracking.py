"""
Tracks which context.id (and, optionally, caller-supplied owner_id) this
integration itself last used to write each light entity, so grouping.py
can tell "did WE make the last change to this light" apart from any
other cause - a person, another automation, a device regaining power
with its own fresh context. context.id (not context.user_id) is the
right signal: every service call made within one automation run shares
the same context.id (confirmed against HA core's helpers/script.py -
each run's Script._context is passed through to every action step),
while anything else - including a *different* automation, which HA
gives its own fresh Context() too - gets an unrelated one. This is what
lets the "leave alone" check catch the case context.user_id couldn't:
another automation (e.g. one triggered directly by a physical button)
setting a light carries no user_id either, identical to our own writes
under the old check.

owner_id (apply_lighting's optional caller-supplied string, e.g. the
blueprint passing its own `this.entity_id`) is recorded alongside
context.id so a *different* apply_lighting caller's write can also be
recognised as "not mine" even though it was technically still this
integration writing it - see grouping.py's externally_set() for the
full comparison. A caller that omits owner_id entirely skips the check
altogether (its own writes are still recorded, with owner_id=None, so a
later keyed caller correctly sees them as someone else's).

Persisted via Store (not just in-memory) so a HA restart doesn't make
every already-on light look externally-set until it happens to change
again some other way.

A device regaining power after an outage is a real gap in the write-
record model above: its own reconnect state report gets a fresh
context too (confirmed against HA core's core.py - any state write
without an explicit context, which a reconnecting device's own state
report always is, gets `Context(id=ulid_at_time(...))`, unconditionally),
indistinguishable from a genuine external change. Left as-is, a
recovered light would look externally-set forever, since nothing else
here ever un-marks it. async_start_listening() closes this by clearing
an entity's own record the moment it's *observed* going unavailable/
unknown - by the time it reconnects, there's no record left to compare
against, so it's "free to manage" again (the same fallback already used
for a brand new entity), through completely ordinary means - no forced
write, no special-casing, just the next regular call finding no claim
to conflict with.
"""

from __future__ import annotations

from typing import Optional, TypedDict

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.storage import Store

STORAGE_VERSION = 1
STORAGE_KEY = "adaptive_lighting_helpers.last_write_context_ids"


class _WriteRecord(TypedDict):
    context_id: str
    owner_id: Optional[str]


class LastWriteTracker:
    """entity_id -> {context_id, owner_id} of the last light.turn_on/
    turn_off this integration issued for it, across every apply_lighting
    call regardless of which automation made it - one record per entity,
    not a history, since only the most recent write ever matters to
    grouping.py's override check."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, _WriteRecord]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict[str, _WriteRecord] = {}

    async def async_load(self) -> None:
        raw = await self._store.async_load() or {}
        # Defensively drop anything not shaped like a _WriteRecord - covers
        # the pre-owner_id storage format (a bare context.id string per
        # entity), which would otherwise crash last_context_id()/
        # last_owner_id() below. Treating those entities as having no
        # record at all is exactly the safe fallback already in place for
        # a brand new entity or a fresh restart - no real migration needed
        # for what's disposable cache data to begin with.
        self._data = {k: v for k, v in raw.items() if isinstance(v, dict) and "context_id" in v}

    def last_context_id(self, entity_id: str) -> str | None:
        record = self._data.get(entity_id)
        return record["context_id"] if record else None

    def last_owner_id(self, entity_id: str) -> str | None:
        record = self._data.get(entity_id)
        return record.get("owner_id") if record else None

    async def async_record(self, entity_ids: list[str], context_id: str, owner_id: str | None = None) -> None:
        """Called once per apply_lighting invocation with every entity it
        actually issued a light.turn_on/turn_off for - not entities it
        merely considered (already-at-target, unreachable, or currently
        externally-set are never passed here)."""
        if not entity_ids:
            return
        for entity_id in entity_ids:
            self._data[entity_id] = {"context_id": context_id, "owner_id": owner_id}
        await self._store.async_save(self._data)

    def async_start_listening(self, hass: HomeAssistant) -> CALLBACK_TYPE:
        """See the module docstring's "device regaining power" section -
        clears an entity's record the instant it's seen going
        unavailable/unknown, so its eventual reconnect (a write we have
        no way to intercept, since it isn't a light.turn_on/turn_off
        call at all) finds no claim to conflict with.

        A single hass-wide "state_changed" listener, not a per-entity
        subscription that would need to be kept in sync with self._data
        as apply_lighting calls add new entities over time - the filter
        below is a cheap dict lookup, and HA integrations commonly use
        this same broad-listen-then-filter pattern rather than manage
        dynamic per-entity subscriptions for something this cheap to
        check."""

        @callback
        def _on_state_changed(event: Event[EventStateChangedData]) -> None:
            entity_id = event.data["entity_id"]
            if entity_id not in self._data:
                return
            new_state = event.data["new_state"]
            if new_state is None or new_state.state not in ("unavailable", "unknown"):
                return
            del self._data[entity_id]
            hass.async_create_task(self._store.async_save(self._data))

        return hass.bus.async_listen("state_changed", _on_state_changed)
