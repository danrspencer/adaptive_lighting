"""
Tracks which context.id this integration last wrote each light with, so
grouping.py can tell "did WE make the last change" apart from a person,
another automation, or a device reconnecting under its own fresh
context.

context.id rather than context.user_id: every service call within one
automation run shares that run's context.id (HA core's
helpers/script.py passes Script._context to every action step), while
anything else - including a different automation - gets an unrelated
one. user_id can't distinguish our own write from another automation's,
since neither carries one.

There is no caller-supplied owner. A light's claims live on exactly one
state device, resolved from configuration by scope_for() below, so the
scope holding a claim *is* its owner - two automations driving one room
write into the same claims and therefore co-operate rather than each
reading the other as an intruder. grouping.py's externally_set() and
override_protection.classify() own the comparison itself; this module
only records.

Deliberately not persisted. Claims live on each state device's tracking
entity and die with a restart, which is correct rather than merely
tolerable: these are lighting overrides, and losing them means a bulb
someone wanted purple goes back to being managed. A cold start tracks
nothing, classify() therefore returns `untracked`, and every light is
manageable - which is exactly the state the old startup resync existed
to reconstruct.

Two claims per entity, not one
------------------------------
- `observed` - a state we have seen and know is safe to write over.
- `latest`   - the most recent write we sent, not yet re-observed.

`observed` is deliberately not "a write of ours". It is populated four
ways, only one of which we authored: a write an earlier call saw the
bulb adopt, the pre-write baseline for a first-ever write, the startup
snapshot, and the snapshot taken when a device returns from
unavailable. What they share is confidence, not authorship - in each
case nothing unexplained has happened to the light, so writing over it
is safe.

apply_lighting records the context it *issued*; nothing waits to confirm
the bulb adopted it. With a single record, one dropped write locks a
light out permanently - the next tick compares the light's real,
unchanged context against a value the device never adopted, and nothing
that happens afterward can ever make those equal. (Seen live: a light
dropped a colour command at a phase boundary and sat excluded for over
an hour, correctly lit the whole time.)

Two slots fix that without needing a growing history. If the live
context matches `latest`, that attempt is now known-good and is promoted
(`observed <- latest`) before the new attempt overwrites `latest`. If it
still matches the old `observed`, `latest` never landed and `observed`
is left exactly as it was. Either way the light is still recognised as
ours and retried next tick. `observed` is only ever evicted by a fresh
observation, so it survives any number of consecutive dropped writes.

An entity's very first write has no `observed` to fall back on, so the
context.id live *before* that write is recorded as `observed` instead.
That isn't claiming ownership of it - it's the same "nothing
unexplained has happened" signal every later dropped write relies on,
since a dropped first write leaves the context at exactly that value.
See async_record.

Why a context mismatch still isn't proof
-----------------------------------------
HA's Entity._context expires 5 seconds after the service call that set
it (homeassistant/core.py), so a device whose Zigbee/MQTT round-trip
confirmation takes longer reports back under an unrelated context while
echoing exactly the value asked for. Each claim therefore also records
its `target` (brightness plus colour temperature or RGB, or None for a
claim that isn't a real write), and classify() falls back to comparing
the entity's current values against either claim's target before
concluding "external".

Device recovery and restarts
-----------------------------
A reconnecting device's own state report also gets a fresh context (any
state write with no explicit context does - core.py).
async_start_listening() watches both directions of the
unavailable/unknown boundary: it clears an entity's record when it is
observed dropping from a real on/off state, and snapshots the
newly-observed context as a fresh `observed` baseline when it comes
back. There is no startup equivalent, and none is needed - nothing is
tracked at startup.

The drop direction must *start* from a real on/off state, not merely end
at unavailable/unknown: nearly every entity passes through
unavailable/unknown on every restart, and clearing on the destination
alone wiped override protection for practically every managed light in
the house on each one.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from typing import Optional, Protocol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .coordinator import StateInstance, state_instances
from .override_protection import _context_matches, _ContextClaim, _WriteRecord

# How long a tracked record is kept after it was last written or
# observed, before async_prune_stale() discards it outright - see that
# method's own docstring for why this exists at all (an entity deleted
# from HA entirely, not just restarting, has no event this integration
# can observe to know it should stop tracking it). Deliberately short:
# pruning a record is never actually risky, regardless of how soon it
# happens - classify() treats "no record at all" identically to
# "unclaimed" (see its own docstring), never as blocked, so a pruned
# light simply looks brand-new again and re-establishes a real record
# on its next write. There's no lockout to guard against by being
# conservative here, so there's no reason to hold onto a record for a
# still-real, still-relevant light any longer than "hasn't needed a
# write in a day" already implies it's not needed.
STALE_RECORD_MAX_AGE_DAYS = 1

# How often async_prune_stale() actually gets called while running (in
# addition to once at startup, in __init__.py) - with a one-day cutoff,
# only pruning at startup would mean a record could sit stale for as
# long as HA happens to stay up between restarts before ever being
# cleaned, which defeats "a day" as a real promise. Frequent enough to
# keep that promise, infrequent enough that it costs nothing meaningful
# (a plain dict scan over however many entities are tracked, typically
# a few dozen).
PRUNE_CHECK_INTERVAL = timedelta(hours=1)

# Fired (with no payload - listeners re-read through the registry)
# whenever any scope's claims change, so the per-scope count sensors
# refresh immediately instead of polling.
SIGNAL_WRITE_TRACKING_UPDATED = "adaptive_lighting_helpers_write_tracking_updated"


def _as_list(value) -> list[str]:
    """A target selector key is a bare string when one thing is picked
    and a list when several are - normalise before membership tests."""
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


class ClaimStore(Protocol):
    """What ClaimRegistry needs from a state device's tracking entity.

    Declared structurally rather than importing sensor.py, which would
    be circular - sensor.py imports this module."""

    claims: dict[str, _WriteRecord]

    def async_claims_changed(self) -> None:
        """Publish the mutated claims as the entity's own state."""


class ClaimRegistry:
    """Routes each light to the state device that tracks it, and reads
    and writes that device's claims.

    Holds no claims of its own. The dict lives on the state device's
    tracking entity, which publishes it as an attribute - so what
    governs behaviour and what you can see in Developer Tools are the
    same object, not a copy kept in step by convention."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._stores: dict[str, ClaimStore] = {}

    @callback
    def register(self, subentry_id: str, store: ClaimStore) -> None:
        self._stores[subentry_id] = store

    @callback
    def unregister(self, subentry_id: str) -> None:
        self._stores.pop(subentry_id, None)

    def scope_for(self, entity_id: str) -> StateInstance | None:
        """Which state device tracks this light - most specific match
        wins, and nothing matches means nothing tracks it.

        Deterministic from configuration rather than from whoever wrote
        last, which is what makes a light's claims live in exactly one
        place no matter how many automations drive it. Two automations
        on one room therefore share that room's claims and co-operate,
        instead of each seeing the other as an intruder.

        A light matching no state device is deliberately left untracked
        - permanently manageable - rather than swept into a catch-all.
        An absent scope is a visible signal that a light needs an area
        or a target; a catch-all would silently absorb the mistake."""
        entry = er.async_get(self._hass).async_get(entity_id)
        device_id = entry.device_id if entry else None
        area_id = entry.area_id if entry else None
        if area_id is None and device_id:
            device = dr.async_get(self._hass).async_get(device_id)
            area_id = device.area_id if device else None

        instances = state_instances(self._entry)  # already sorted by title
        for key, value in (("entity_id", entity_id), ("device_id", device_id), ("area_id", area_id)):
            if value is None:
                continue
            for instance in instances:
                if value in _as_list(instance.target.get(key)):
                    return instance
        return None

    def _store_for(self, entity_id: str) -> ClaimStore | None:
        """The live tracking entity holding this light's claims.

        None whenever the light resolves to no scope, *or* its scope's
        entity hasn't been added yet - services are registered before
        the platforms are forwarded (see __init__.py), so a write can
        genuinely arrive first. Such a write is dropped: the light stays
        untracked and therefore manageable, and the next tick records it
        properly. Nothing is queued, because a lighting override that
        goes unrecorded for one tick costs nothing."""
        # An already-tracked light keeps its existing home even if the
        # targets have since changed, so re-pointing a state device
        # can't strand claims in a scope nothing reads any more.
        for store in self._stores.values():
            if entity_id in store.claims:
                return store
        instance = self.scope_for(entity_id)
        return self._stores.get(instance.subentry_id) if instance else None

    def _record(self, entity_id: str) -> _WriteRecord | None:
        store = self._store_for(entity_id)
        return store.claims.get(entity_id) if store else None

    def all_records(self) -> dict[str, _WriteRecord]:
        """Every tracked light across every scope, flattened. A light
        can only appear once - _store_for keeps it in one scope."""
        merged: dict[str, _WriteRecord] = {}
        for store in self._stores.values():
            merged.update(store.claims)
        return merged

    def records_for_scope(self, subentry_id: str) -> dict[str, _WriteRecord]:
        store = self._stores.get(subentry_id)
        return dict(store.claims) if store else {}

    @callback
    def _notify(self, stores: Iterable[ClaimStore]) -> None:
        for store in stores:
            store.async_claims_changed()
        async_dispatcher_send(self._hass, SIGNAL_WRITE_TRACKING_UPDATED)

    def _snapshot_observed(self, entity_id: str, context_id: str) -> ClaimStore | None:
        """Replace `observed` with a fresh baseline built from a live
        context.id just observed for this entity - recorded_at=None,
        since this is merely *observed*, not a write
        this integration actually made (the same convention
        async_record's synthetic first-write baseline uses - see its own
        docstring). `latest` is left untouched: a claim from before
        this observation can never match this fresh context either way,
        so it's simply inert until the next real write overwrites it.
        Used by async_start_listening's recovery branch. Returns the
        store it touched so the caller can publish it, or None when the
        light belongs to no scope."""
        store = self._store_for(entity_id)
        if store is None:
            return None
        record = store.claims.get(entity_id)
        store.claims[entity_id] = {
            "observed": {
                "context_id": context_id,
                "secondary_context_id": None,
                "recorded_at": None,
                "target": None,
            },
            "latest": record.get("latest") if record else None,
            "last_seen": dt_util.utcnow().isoformat(),
        }
        return store

    def observed_context_id(self, entity_id: str) -> str | None:
        record = self._record(entity_id)
        claim = record.get("observed") if record else None
        return claim["context_id"] if claim else None

    def observed_target(self, entity_id: str) -> dict | None:
        record = self._record(entity_id)
        claim = record.get("observed") if record else None
        return claim.get("target") if claim else None

    def observed_secondary_context_id(self, entity_id: str) -> str | None:
        record = self._record(entity_id)
        claim = record.get("observed") if record else None
        return claim.get("secondary_context_id") if claim else None

    def latest_context_id(self, entity_id: str) -> str | None:
        record = self._record(entity_id)
        claim = record.get("latest") if record else None
        return claim["context_id"] if claim else None

    def latest_target(self, entity_id: str) -> dict | None:
        record = self._record(entity_id)
        claim = record.get("latest") if record else None
        return claim.get("target") if claim else None

    def latest_secondary_context_id(self, entity_id: str) -> str | None:
        record = self._record(entity_id)
        claim = record.get("latest") if record else None
        return claim.get("secondary_context_id") if claim else None

    async def async_clear(self, entity_ids: list[str]) -> None:
        """Manually discards an entity's tracked record entirely -
        deliberately invoked, unlike every other path that removes a
        record (async_start_listening's drop-detection, which only ever
        fires on an *observed* unavailable transition). Backs the
        clear_claims service and the write-tracking dashboard card's
        "Clear" action - the escape hatch for a light that's landed in
        "overridden" without ever actually going unavailable, and so
        has no other way back: build_groups() (grouping.py) never calls
        async_record for anything externally_set() already excludes, so
        an overridden light's own `latest` target only gets staler
        over time and can never refresh itself on a ramping curve -
        confirmed live, several kitchen lights during a Day-phase Kelvin
        ramp, correctly lit the whole time but permanently excluded once
        the live colour temperature drifted a single Kelvin past the
        rescue tolerance of a `latest` claim that was itself frozen the
        moment exclusion began. A no-op for an entity with no record."""
        touched: set[int] = set()
        stores = []
        for entity_id in entity_ids:
            store = self._store_for(entity_id)
            if store is not None and store.claims.pop(entity_id, None) is not None and id(store) not in touched:
                touched.add(id(store))
                stores.append(store)
        if stores:
            self._notify(stores)

    async def async_record(
        self,
        entity_ids: list[str],
        live_context_before_write: dict[str, str | None],
        context_id: str,
        targets: dict[str, dict] | None = None,
        secondary_context_ids: dict[str, str] | None = None,
        context_id_overrides: dict[str, str] | None = None,
    ) -> None:
        """Called once per apply_lighting invocation, with every entity it
        actually issued a light.turn_on/turn_off for - not ones it merely
        considered. See the module docstring for the two-claim model this
        maintains; this documents the arguments.

        live_context_before_write: each entity's context.id as read
        *before* any of this call's writes were dispatched. It cannot be
        read fresh in here - by the time this runs the writes have been
        awaited, so a light's live context may already reflect the very
        write about to be recorded as `latest`, making every write look
        like it promoted itself instantly.

        This is the one and only place promotion happens: if the previous
        `latest` claim matches what was live just before this write went
        out, that attempt is proven landed and becomes `observed`.
        Otherwise `observed` is left untouched and only `latest` is
        replaced. The exception is an entity's first-ever write, which has
        no `observed` to fall back on - the pre-write context is recorded
        as `observed`, so a dropped first write still
        has a retry signal, and that synthetic baseline never blocks
        anyone else's claim.

        targets: per entity, what this write asked for. An entity missing
        from it (an off-command has no colour target) gets None.

        secondary_context_ids / context_id_overrides: two-step entities
        only. Those writes go out as two light.turn_on calls under two
        distinct contexts, neither of which is `context_id` above (the
        triggering call's own, never passed to either). The overrides
        supply the colour step as the claim's primary context; the
        secondaries supply the brightness step. Both stay empty for
        everything else, which keeps using `context_id` alone."""
        if not entity_ids:
            return
        targets = targets or {}
        secondary_context_ids = secondary_context_ids or {}
        context_id_overrides = context_id_overrides or {}
        touched: set[int] = set()
        stores = []
        for entity_id in entity_ids:
            store = self._store_for(entity_id)
            if store is None:
                # No state device covers this light (or its scope's
                # entity isn't up yet) - see _store_for.
                continue
            if id(store) not in touched:
                touched.add(id(store))
                stores.append(store)
            old = store.claims.get(entity_id)
            observed: Optional[_ContextClaim]
            if old is not None:
                old_latest = old.get("latest")
                if _context_matches(old_latest, live_context_before_write.get(entity_id)):
                    observed = old_latest
                else:
                    observed = old.get("observed")
            else:
                baseline_context = live_context_before_write.get(entity_id)
                observed = (
                    {
                        "context_id": baseline_context,
                        "secondary_context_id": None,
                        "recorded_at": None,
                        "target": None,
                    }
                    if baseline_context is not None
                    else None
                )
            store.claims[entity_id] = {
                "observed": observed,
                "latest": {
                    "context_id": context_id_overrides.get(entity_id, context_id),
                    "secondary_context_id": secondary_context_ids.get(entity_id),
                    "recorded_at": dt_util.utcnow().isoformat(),
                    "target": targets.get(entity_id),
                },
                "last_seen": dt_util.utcnow().isoformat(),
            }
        if stores:
            self._notify(stores)

    async def async_prune_stale(self) -> None:
        """Discards any tracked record not written or observed in over
        STALE_RECORD_MAX_AGE_DAYS days - the cleanup an entity genuinely
        *deleted* from HA never otherwise gets. Every other cleanup path
        here needs `hass.states.get()` to return *something* to act on;
        a removed entity returns None forever and is silently skipped by
        all of them, leaving its record stranded.

        Called once at startup and every PRUNE_CHECK_INTERVAL after - a
        startup-only pass would let records sit stale for however long HA
        stays up.

        Deliberately aggressive on timing, unlike most decisions here:
        pruning too soon has no failure mode, since classify() treats "no
        record" as `unclaimed`, never as blocked. A record with no
        parseable `last_seen` is left alone - when age can't be judged,
        the same "don't delete on ambiguity" preference used elsewhere."""
        cutoff = dt_util.utcnow() - timedelta(days=STALE_RECORD_MAX_AGE_DAYS)
        stale = []
        for entity_id, record in self.all_records().items():
            last_seen = record.get("last_seen")
            if not last_seen:
                continue
            parsed = dt_util.parse_datetime(last_seen)
            if parsed is not None and parsed < cutoff:
                stale.append(entity_id)
        if not stale:
            return
        touched: set[int] = set()
        stores = []
        for entity_id in stale:
            store = self._store_for(entity_id)
            if store is None:
                continue
            store.claims.pop(entity_id, None)
            if id(store) not in touched:
                touched.add(id(store))
                stores.append(store)
        if stores:
            self._notify(stores)

    def async_start_listening(self, hass: HomeAssistant) -> CALLBACK_TYPE:
        """Watches both directions of the unavailable/unknown boundary for
        every tracked entity, via one hass-wide "state_changed" listener -
        cheaper than keeping per-entity subscriptions in sync with
        the tracked set as apply_lighting adds entities over time.

        - **Drop** (a real on/off state -> unavailable/unknown): clears
          the record entirely, so the eventual reconnect - a write we
          can't intercept, since it isn't a service call at all - finds
          no claim to conflict with.
        - **Recovery** (unavailable/unknown, or no prior state, -> a real
          state): snapshots the observed context as the new `observed`
          baseline, so a reconnect - a write we can't intercept, since
          it isn't a service call at all - doesn't read as an override.

        Both directions require the *other* endpoint to be a genuine
        on/off state, not just the destination. Almost every entity
        passes through unavailable/unknown on every restart, and treating
        that as a real drop cleared protection for practically every
        light in the house each time - a light dimmed by hand hours later
        was then silently overwritten, its record long since wiped."""

        @callback
        def _on_state_changed(event: Event[EventStateChangedData]) -> None:
            entity_id = event.data["entity_id"]
            store = self._store_for(entity_id)
            if store is None or entity_id not in store.claims:
                return
            old_state = event.data["old_state"]
            new_state = event.data["new_state"]
            old_available = old_state is not None and old_state.state not in ("unavailable", "unknown")
            new_available = new_state is not None and new_state.state not in ("unavailable", "unknown")
            # Drop requires new_state to explicitly report "unavailable"/
            # "unknown" as a string - not just new_state being absent
            # entirely (entity removed from the state machine, e.g. a
            # fresh process's own state machine having no entry for it
            # yet). That distinction matters: treating "gone" the same
            # as "unavailable" here would clear the record the instant
            # an entity's old in-memory state vanishes as a routine part
            # of every restart, before its own reconnect event ever
            # fires - reopening the exact "wiped on every restart, not
            # just real drops" incident this listener exists to prevent
            # (see the module docstring). Recovery has no equivalent
            # asymmetry: old_state being absent entirely is exactly the
            # "no prior state, now reporting real" case it's meant to
            # catch too (a fresh process's first-ever report for this
            # entity, functionally identical to recovering from a drop).
            new_explicitly_unavailable = new_state is not None and new_state.state in ("unavailable", "unknown")

            if old_available and new_explicitly_unavailable:
                store.claims.pop(entity_id, None)
            elif not old_available and new_available:
                self._snapshot_observed(entity_id, new_state.context.id)
            else:
                return

            self._notify([store])

        return hass.bus.async_listen("state_changed", _on_state_changed)
