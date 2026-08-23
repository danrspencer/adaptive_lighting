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

        A HA restart recreates every entity's state object from scratch,
        so the very first state report after restart always carries a
        fresh context.id - even when the reported value hasn't actually
        changed and the underlying device never went offline at all.
        That's indistinguishable from a genuine external change to
        externally_set()'s comparison, and since an externally-set light
        is never written, every already-tracked light left alone here
        would look permanently overridden the moment HA comes back up -
        the same lockout the confirmed/pending redesign exists to
        prevent (see the module docstring), just triggered by *any*
        restart instead of a dropped write. Confirmed live: a plain
        restart left `light.kitchen_1` - genuinely on, never dropped
        off the network - excluded from every tick.

        A light still unavailable/unknown at this exact moment isn't
        skipped forever - async_start_listening()'s listener performs
        the identical snapshot the moment that light *does* next report
        a real state, closing what would otherwise be a startup-ordering
        race: a restart puts nearly every entity through
        unavailable/unknown before it reports back on, and this
        one-shot pass runs early enough that many entities are still
        mid-reconnect when it does. Confirmed live: light.kitchen_2,
        genuinely on, stayed excluded through several real ticks after a
        restart that this pass alone did fix light.kitchen_1 against -
        both recovered from the same restart, just close enough
        together in time that only one of them had already reported
        back when this ran.

        Snapshots each tracked entity's current live context as its new
        `confirmed` baseline (see _snapshot_confirmed) - owner_id=None
        and recorded_at=None, exactly the synthetic first-write baseline
        async_record already uses for the analogous "no real claim yet,
        but the light hasn't changed" situation (see its own docstring).
        If nothing touches the light between restart and the next real
        write attempt, its context stays exactly this value, so that
        attempt sees a match and resumes control normally instead of
        treating the restart itself as an override."""
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
        """Called once per apply_lighting invocation with every entity it
        actually issued a light.turn_on/turn_off for - not entities it
        merely considered (already-at-target, unreachable, or currently
        externally-set are never passed here).

        live_context_before_write is each entity's context.id as read
        *before* any of this call's writes were dispatched - the caller
        (apply_lighting) snapshots it straight after build_groups()
        returns, since nothing async happens between that point and
        actually issuing the writes below it, so it's a true "walking in"
        value. It cannot be read fresh in here instead: by the time this
        function runs, this call's own writes have already been awaited,
        so the light's live context may already reflect the very write
        about to be recorded as `pending` - comparing against that would
        make every write look like it promoted itself instantly, whether
        or not the device actually adopted anything.

        For each entity, this is the one and only place promotion
        happens: if the *previous* pending claim's context.id matches
        what was live just before this new write went out, that previous
        attempt is now proven to have landed, and gets promoted into
        `confirmed` - overwriting whatever `confirmed` held before
        it, since it's necessarily newer. Otherwise `confirmed` is left
        completely untouched, and only `pending` is replaced. This is
        what keeps `confirmed` valid across any number of consecutive
        dropped writes - it is only ever replaced by an *observed*
        match, never by assumption, so a light can be retried
        indefinitely without the fallback that's still known-good ever
        being discarded on a guess.

        The one exception is the entity's very first-ever write (no
        prior record at all): there is no previous `confirmed` to fall
        back on, so the context.id that was live *before* this write -
        almost certainly not ours - is recorded as `confirmed` instead,
        with owner_id=None. This isn't claiming that write as ours; it's
        using "the light hasn't changed" as the retry signal it already
        is for every later write. If this first write drops, the light's
        context stays exactly that pre-write value (nothing else can
        produce the same context.id without actually touching the
        entity), so the next call sees a `confirmed` match and retries
        cleanly instead of reading "no record -> free" and losing track
        of the attempt. owner_id=None means this synthetic baseline
        never itself blocks a different owner_id's claim - see
        grouping.py's externally_set().

        targets records, per entity, what this write actually asked for
        - {"brightness": ..., "color_temp_kelvin": ...} or {"brightness":
        ..., "rgb_color": [...]}. An entity missing from targets (an
        off-command has no brightness/colour target) gets None. This is
        what lets externally_set() recognise its own write echoed back
        under a context.id it doesn't otherwise recognise - see its
        docstring for why that's a real, not hypothetical, gap: HA's
        Entity._context expires 5 seconds after the service call that
        set it, so a device whose real confirmation takes longer than
        that reports back under an unrelated context even when it's
        agreeing with us exactly.

        secondary_context_ids / context_id_overrides: for a two-step
        transition (no_combined_transition label), the brightness-only
        step and the colour step now genuinely get two distinct
        context.id values (see __init__.py's _two_step_turn_on) rather
        than sharing one - a device reporting the brightness-only step
        back on its own is a real, expected intermediate state for
        these bulbs, not an anomaly, and neither of those two contexts
        is `context_id` above (the *triggering* apply_lighting call's
        own context - never itself passed to either light.turn_on call
        for a two-step entity). context_id_overrides supplies the
        colour step's context as this claim's actual primary
        `context_id` (the final, complete state); secondary_context_ids
        supplies the brightness step's as `secondary_context_id`,
        recorded alongside it - either one landing is recognised as
        ours (see override_protection.py's _context_matches). Both
        dicts are keyed by entity_id and stay empty for anything that
        isn't a two-step entity, which keeps using `context_id` alone,
        unchanged."""
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
        """Discards any tracked record that hasn't been written or
        observed in over STALE_RECORD_MAX_AGE_DAYS days - the cleanup
        an entity genuinely *deleted* from Home Assistant (not just
        restarting) never otherwise gets. Every existing cleanup path
        here (async_start_listening's drop-detection,
        async_resync_to_live_state's startup pass) depends on
        `hass.states.get(entity_id)` returning *something* - a real
        on/off state, or at least unavailable/unknown - to have anything
        to act on. An entity removed outright (no device, no
        entity_id, nothing - e.g. a Zigbee2MQTT group deleted at the
        source) produces none of that: `hass.states.get(...)` just
        returns `None` forever, silently skipped by both of those paths
        (see their own docstrings), leaving its record in the Store
        indefinitely with nothing left in Home Assistant that could ever
        prompt its removal. Confirmed live: `light.extension_spots_left`,
        a deleted Zigbee2MQTT group with no matching entity anywhere in
        the registry, found stuck in this Store with no way to ever
        observe its own deletion.

        Called once at startup (right after async_load()/
        async_resync_to_live_state), and again every PRUNE_CHECK_INTERVAL
        while running (see __init__.py) - the one-day cutoff needs the
        periodic call to mean anything in practice, since a startup-only
        pass would let a record sit stale for as long as HA happens to
        stay up between restarts before ever being cleaned.

        Deliberately aggressive on timing, not conservative: unlike most
        of this integration's own decisions, there's no failure mode to
        weigh against pruning too soon - `classify()` treats "no record
        at all" identically to `"unclaimed"` (see its own docstring),
        never as blocked, so a pruned-too-early record for a still-real
        light just makes it look brand new again, re-established
        normally on its very next write. The only thing at stake here is
        Store hygiene, not override protection, which is why this can
        be short where nearly everything else in this module is
        deliberately lenient/slow-to-conclude instead.

        Still, a record with no `last_seen` at all
        (shouldn't happen after async_load()'s own backfill, but handled
        defensively) or an unparseable one is left alone rather than
        pruned - when age can't be judged, the same "don't delete on
        ambiguity" preference this integration applies everywhere else
        (see the module docstring's first-write-baseline reasoning)."""
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
        """Watches both directions of the unavailable/unknown boundary
        for every tracked entity, via a single hass-wide "state_changed"
        listener (not a per-entity subscription that would need to be
        kept in sync with self._data as apply_lighting calls add new
        entities over time - the filter below is a cheap dict lookup,
        and HA integrations commonly use this same broad-listen-then-
        filter pattern rather than manage dynamic per-entity
        subscriptions for something this cheap to check):

        - **Drop** (a real on/off state -> unavailable/unknown): clears
          the entity's record entirely - see the module docstring's
          "device regaining power" section. Its eventual reconnect (a
          write we have no way to intercept, since it isn't a
          light.turn_on/turn_off call at all) then finds no claim to
          conflict with.
        - **Recovery** (unavailable/unknown, or no prior state at all ->
          a real state): snapshots the just-observed context as the new
          `confirmed` baseline via _snapshot_confirmed - the same
          operation async_resync_to_live_state performs once at startup,
          triggered here instead by the live event that startup pass
          can miss if this entity was still mid-reconnect at that exact
          moment (see that method's own docstring for the live incident
          - light.kitchen_2 - this closes).

        Both directions require the *other* endpoint to be a genuine
        on/off state, not just checking the destination - almost every
        entity passes through unavailable/unknown as a routine part of
        every HA restart (old_state is None - a fresh process's state
        machine has no history yet - or already unavailable/unknown
        itself), and treating a drop-shaped transition as a real drop
        when it's actually just routine restart noise wiped override
        protection for practically every managed light in the house on
        every single restart, not just ones that had actually dropped
        off the network - a live incident: a light dimmed by hand hours
        after a restart got silently overwritten on the next tick,
        because its record had been cleared during that restart and
        never rewritten since (nothing had needed a real write to it in
        between)."""

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
