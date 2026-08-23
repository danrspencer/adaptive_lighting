"""
Tracks which context.id (and optional caller-supplied owner_id) this
integration last wrote each light with, so grouping.py can tell "did WE
make the last change" apart from a person, another automation, or a
device reconnecting under its own fresh context.

context.id rather than context.user_id: every service call within one
automation run shares that run's context.id (HA core's
helpers/script.py passes Script._context to every action step), while
anything else - including a different automation - gets an unrelated
one. user_id can't distinguish our own write from another automation's,
since neither carries one.

owner_id (e.g. the blueprint passing its own `this.entity_id`) is
recorded alongside, so a *different* apply_lighting caller's write is
recognised as "not mine" even though this integration still made it.
Omitting owner_id skips the check entirely; the write is still recorded
under owner_id=None so a later keyed caller sees it as someone else's.
grouping.py's externally_set() and override_protection.classify() own
the comparison itself - this module only records and persists.

Persisted via Store so a restart doesn't make every already-on light
look externally set.

Two claims per entity, not one
------------------------------
- `confirmed` - a write some earlier call actually observed landing.
- `pending`   - the most recent attempt, not yet verified either way.

apply_lighting records the context it *issued*; nothing waits to confirm
the bulb adopted it. With a single record, one dropped write locks a
light out permanently - the next tick compares the light's real,
unchanged context against a value the device never adopted, and nothing
that happens afterward can ever make those equal. (Seen live: a light
dropped a colour command at a phase boundary and sat excluded for over
an hour, correctly lit the whole time.)

Two slots fix that without needing a growing history. If the live
context matches `pending`, that attempt is now known-good and is
promoted (`confirmed <- pending`) before the new attempt overwrites
`pending`. If it still matches the old `confirmed`, `pending` never
landed and `confirmed` is left exactly as it was. Either way the light
is still recognised as ours and retried next tick. `confirmed` is only
ever evicted by an *observed* match, so it survives any number of
consecutive dropped writes.

An entity's very first write has no `confirmed` to fall back on, so the
context.id live *before* that write is recorded as `confirmed` instead.
That isn't claiming ownership of it - it's reusing the same "the light
hasn't changed" retry signal every later dropped write relies on, since
a dropped first write leaves the context at exactly that value. See
async_record.

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
newly-observed context as a fresh `confirmed` baseline when it comes
back. async_resync_to_live_state performs the same snapshot once at
startup for whatever is already reporting by then.

The drop direction must *start* from a real on/off state, not merely end
at unavailable/unknown: nearly every entity passes through
unavailable/unknown on every restart, and clearing on the destination
alone wiped override protection for practically every managed light in
the house on each one.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .override_protection import _context_matches, _ContextClaim, _WriteRecord

STORAGE_VERSION = 1
STORAGE_KEY = "adaptive_lighting_helpers.last_write_context_ids"

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

# Fired (with no payload - listeners re-read via snapshot()) whenever
# self._data changes, so the diagnostic sensor in sensor.py can refresh
# itself immediately instead of polling - see snapshot()'s own docstring.
SIGNAL_WRITE_TRACKING_UPDATED = "adaptive_lighting_helpers_write_tracking_updated"


class LastWriteTracker:
    """entity_id -> {confirmed, pending} claims for the last two writes
    this integration issued for it, across every apply_lighting call
    regardless of which automation made it - see the module docstring
    for why exactly two, not one and not a growing history."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[dict[str, _WriteRecord]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict[str, _WriteRecord] = {}

    def snapshot(self) -> dict[str, _WriteRecord]:
        """A shallow copy of every entity currently tracked, for the
        diagnostic sensor (sensor.py's _WriteTrackingSensor) - this is
        otherwise a black box only inspectable indirectly through
        compute_lighting_groups's combined/needing_off output, which
        tells you *whether* a light is excluded, never *why*. Read-only:
        callers must not mutate the returned claims."""
        return dict(self._data)

    async def async_load(self) -> None:
        raw = await self._store.async_load() or {}
        now_iso = dt_util.utcnow().isoformat()
        data: dict[str, _WriteRecord] = {}
        for entity_id, value in raw.items():
            if not isinstance(value, dict):
                continue
            if "confirmed" in value and "pending" in value:
                # Older confirmed/pending records (pre-recorded_at) are
                # missing the key entirely - .setdefault below back-fills
                # None on load rather than needing every reader to
                # handle a missing key as well as an explicit None.
                for claim in (value.get("confirmed"), value.get("pending")):
                    if claim is not None:
                        claim.setdefault("recorded_at", None)
                        claim.setdefault("target", None)
                        # Records from before two-step transitions got
                        # their own second context.id (see async_record's
                        # own docstring) are missing this key entirely -
                        # None here means exactly what it always has for
                        # a claim with only one context: no secondary to
                        # match against.
                        claim.setdefault("secondary_context_id", None)
                # Records from before last_seen existed (every record
                # persisted prior to this feature shipping) get backfilled
                # to *now*, not left missing or backdated - deliberately
                # conservative: this is what gives every already-tracked
                # light a fresh full STALE_RECORD_MAX_AGE_DAYS window
                # after upgrading, rather than risking a one-time mass
                # prune of legitimate, currently-relevant records the very
                # first time this loads on an existing install.
                value.setdefault("last_seen", now_iso)
                data[entity_id] = value  # already this shape
            elif "context_id" in value:
                # The single-record format this integration shipped with
                # before confirmed/pending existed - one {context_id,
                # owner_id} per entity. Treated as an already-established
                # confirmed baseline with nothing pending, not dropped
                # outright - an upgrade shouldn't itself reopen "no
                # record -> free" for every currently-protected light in
                # the house, the same class of incident the restart-blip
                # fix above exists to prevent.
                data[entity_id] = {
                    "confirmed": {
                        "context_id": value["context_id"],
                        "secondary_context_id": None,
                        "owner_id": value.get("owner_id"),
                        "recorded_at": None,
                        "target": None,
                    },
                    "pending": None,
                    "last_seen": now_iso,
                }
            # Anything else (the even older bare-string format, or
            # malformed data) is dropped - same safe "no record -> free"
            # fallback this integration has always used for data it
            # doesn't recognise.
        self._data = data

    async def async_resync_to_live_state(self, hass: HomeAssistant) -> None:
        """Called once at integration startup, right after async_load().

        A restart recreates every entity's state object, so the first
        report after one always carries a fresh context.id even when the
        value never changed and the device never went offline - which is
        indistinguishable from a genuine external change. Without this,
        every already-tracked light would look permanently overridden
        after any restart.

        Snapshots each tracked entity's live context as a new `confirmed`
        baseline (owner_id/recorded_at None - observed, not written, the
        same convention async_record's first-write baseline uses). An
        entity still unavailable here isn't skipped forever:
        async_start_listening's recovery branch performs the identical
        snapshot when it next reports, closing a startup-ordering race
        where a restart leaves many entities still mid-reconnect."""
        changed = False
        for entity_id in list(self._data):
            state = hass.states.get(entity_id)
            if state is None or state.state in ("unavailable", "unknown"):
                continue
            self._snapshot_confirmed(entity_id, state.context.id)
            changed = True
        if changed:
            await self._store.async_save(self._data)
            async_dispatcher_send(self._hass, SIGNAL_WRITE_TRACKING_UPDATED)

    def _snapshot_confirmed(self, entity_id: str, context_id: str) -> None:
        """Replace `confirmed` with a fresh baseline built from a live
        context.id just observed for this entity - owner_id=None and
        recorded_at=None, since this is merely *observed*, not a write
        this integration actually made (the same convention
        async_record's synthetic first-write baseline uses - see its own
        docstring). `pending` is left untouched: a claim from before
        this observation can never match this fresh context either way,
        so it's simply inert until the next real write overwrites it.
        Shared by async_resync_to_live_state's startup pass and
        async_start_listening's live recovery handling below - the same
        operation, triggered two different ways."""
        record = self._data.get(entity_id)
        self._data[entity_id] = {
            "confirmed": {
                "context_id": context_id,
                "secondary_context_id": None,
                "owner_id": None,
                "recorded_at": None,
                "target": None,
            },
            "pending": record.get("pending") if record else None,
            "last_seen": dt_util.utcnow().isoformat(),
        }

    def confirmed_context_id(self, entity_id: str) -> str | None:
        record = self._data.get(entity_id)
        claim = record.get("confirmed") if record else None
        return claim["context_id"] if claim else None

    def confirmed_owner_id(self, entity_id: str) -> str | None:
        record = self._data.get(entity_id)
        claim = record.get("confirmed") if record else None
        return claim.get("owner_id") if claim else None

    def confirmed_target(self, entity_id: str) -> dict | None:
        record = self._data.get(entity_id)
        claim = record.get("confirmed") if record else None
        return claim.get("target") if claim else None

    def confirmed_secondary_context_id(self, entity_id: str) -> str | None:
        record = self._data.get(entity_id)
        claim = record.get("confirmed") if record else None
        return claim.get("secondary_context_id") if claim else None

    def pending_context_id(self, entity_id: str) -> str | None:
        record = self._data.get(entity_id)
        claim = record.get("pending") if record else None
        return claim["context_id"] if claim else None

    def pending_owner_id(self, entity_id: str) -> str | None:
        record = self._data.get(entity_id)
        claim = record.get("pending") if record else None
        return claim.get("owner_id") if claim else None

    def pending_target(self, entity_id: str) -> dict | None:
        record = self._data.get(entity_id)
        claim = record.get("pending") if record else None
        return claim.get("target") if claim else None

    def pending_secondary_context_id(self, entity_id: str) -> str | None:
        record = self._data.get(entity_id)
        claim = record.get("pending") if record else None
        return claim.get("secondary_context_id") if claim else None

    async def async_clear(self, entity_ids: list[str]) -> None:
        """Manually discards an entity's tracked record entirely -
        deliberately invoked, unlike every other path that removes a
        record (async_start_listening's drop-detection, which only ever
        fires on an *observed* unavailable transition). Backs the
        clear_ownership service and the write-tracking dashboard card's
        "Clear" action - the escape hatch for a light that's landed in
        "overridden" without ever actually going unavailable, and so
        has no other way back: build_groups() (grouping.py) never calls
        async_record for anything externally_set() already excludes, so
        an overridden light's own `pending` target only gets staler
        over time and can never refresh itself on a ramping curve -
        confirmed live, several kitchen lights during a Day-phase Kelvin
        ramp, correctly lit the whole time but permanently excluded once
        the live colour temperature drifted a single Kelvin past the
        rescue tolerance of a `pending` claim that was itself frozen the
        moment exclusion began. A no-op for an entity with no record."""
        changed = False
        for entity_id in entity_ids:
            if self._data.pop(entity_id, None) is not None:
                changed = True
        if changed:
            await self._store.async_save(self._data)
            async_dispatcher_send(self._hass, SIGNAL_WRITE_TRACKING_UPDATED)

    async def async_record(
        self,
        entity_ids: list[str],
        live_context_before_write: dict[str, str | None],
        context_id: str,
        owner_id: str | None = None,
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
        write about to be recorded as `pending`, making every write look
        like it promoted itself instantly.

        This is the one and only place promotion happens: if the previous
        `pending` claim matches what was live just before this write went
        out, that attempt is proven landed and becomes `confirmed`.
        Otherwise `confirmed` is left untouched and only `pending` is
        replaced. The exception is an entity's first-ever write, which has
        no `confirmed` to fall back on - the pre-write context is recorded
        as `confirmed` with owner_id=None, so a dropped first write still
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
        for entity_id in entity_ids:
            old = self._data.get(entity_id)
            confirmed: Optional[_ContextClaim]
            if old is not None:
                old_pending = old.get("pending")
                if _context_matches(old_pending, live_context_before_write.get(entity_id)):
                    confirmed = old_pending
                else:
                    confirmed = old.get("confirmed")
            else:
                baseline_context = live_context_before_write.get(entity_id)
                confirmed = (
                    {
                        "context_id": baseline_context,
                        "secondary_context_id": None,
                        "owner_id": None,
                        "recorded_at": None,
                        "target": None,
                    }
                    if baseline_context is not None
                    else None
                )
            self._data[entity_id] = {
                "confirmed": confirmed,
                "pending": {
                    "context_id": context_id_overrides.get(entity_id, context_id),
                    "secondary_context_id": secondary_context_ids.get(entity_id),
                    "owner_id": owner_id,
                    "recorded_at": dt_util.utcnow().isoformat(),
                    "target": targets.get(entity_id),
                },
                "last_seen": dt_util.utcnow().isoformat(),
            }
        await self._store.async_save(self._data)
        async_dispatcher_send(self._hass, SIGNAL_WRITE_TRACKING_UPDATED)

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
        for entity_id, record in self._data.items():
            last_seen = record.get("last_seen")
            if not last_seen:
                continue
            parsed = dt_util.parse_datetime(last_seen)
            if parsed is not None and parsed < cutoff:
                stale.append(entity_id)
        if not stale:
            return
        for entity_id in stale:
            del self._data[entity_id]
        await self._store.async_save(self._data)
        async_dispatcher_send(self._hass, SIGNAL_WRITE_TRACKING_UPDATED)

    def async_start_listening(self, hass: HomeAssistant) -> CALLBACK_TYPE:
        """Watches both directions of the unavailable/unknown boundary for
        every tracked entity, via one hass-wide "state_changed" listener -
        cheaper than keeping per-entity subscriptions in sync with
        self._data as apply_lighting adds entities over time.

        - **Drop** (a real on/off state -> unavailable/unknown): clears
          the record entirely, so the eventual reconnect - a write we
          can't intercept, since it isn't a service call at all - finds
          no claim to conflict with.
        - **Recovery** (unavailable/unknown, or no prior state, -> a real
          state): snapshots the observed context as the new `confirmed`
          baseline, the same operation async_resync_to_live_state does at
          startup, triggered by the live event that pass can miss.

        Both directions require the *other* endpoint to be a genuine
        on/off state, not just the destination. Almost every entity
        passes through unavailable/unknown on every restart, and treating
        that as a real drop cleared protection for practically every
        light in the house each time - a light dimmed by hand hours later
        was then silently overwritten, its record long since wiped."""

        @callback
        def _on_state_changed(event: Event[EventStateChangedData]) -> None:
            entity_id = event.data["entity_id"]
            if entity_id not in self._data:
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
                del self._data[entity_id]
            elif not old_available and new_available:
                self._snapshot_confirmed(entity_id, new_state.context.id)
            else:
                return

            hass.async_create_task(self._store.async_save(self._data))
            async_dispatcher_send(hass, SIGNAL_WRITE_TRACKING_UPDATED)

        return hass.bus.async_listen("state_changed", _on_state_changed)
