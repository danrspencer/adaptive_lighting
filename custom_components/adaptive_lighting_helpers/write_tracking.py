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
an entity's own record the moment it's *observed* going from a real
on/off state to unavailable/unknown - by the time it reconnects,
there's no record left to compare against, so it's "free to manage"
again (the same fallback already used for a brand new entity), through
completely ordinary means - no forced write, no special-casing, just
the next regular call finding no claim to conflict with.

The transition must start from a real on/off state, not just end at
unavailable/unknown - almost every entity passes through unavailable/
unknown as a routine part of every HA restart (a fresh process's state
machine has no prior state for anything yet), which is indistinguishable
from a genuine drop if only the destination state is checked. Clearing
on that alone wiped override protection for practically every managed
light in the house on every single restart, not just ones that had
actually dropped off the network - confirmed as the cause of a live
incident where a light dimmed by hand hours after a restart was
silently overwritten on the very next tick, because its record had
been cleared during that restart and nothing had needed a real
rewrite to it since.

Each entity's record holds not one write but two - `confirmed` and
`pending`:

- `confirmed` is a write some *earlier* call actually observed landing
  (a later tick found the entity's live context.id matching it).
- `pending` is the most recent write attempted, not yet verified either
  way.

apply_lighting records the context it *issued*, not the context the
device actually adopted - the two calls are asynchronous, and nothing
here waits to confirm the physical bulb applied the command before
recording it. A single-record design (what this used to be) treats
that recorded-but-never-adopted context as gospel: the next tick
compares the light's real, unchanged context against it, finds a
mismatch, and concludes the light was touched externally - permanently,
since nothing that ever happens next can make the live context
retroactively equal a value the device never adopted. Confirmed live:
a kitchen light dropped a colour-mode switch command at the Evening
boundary and sat stuck on Day's stale colour temperature for over an
hour, silently excluded from every tick in between.

The fix doesn't need to know *why* a write failed, only to notice when
one did and try again: on each call, if the live context matches
`pending`, the previous attempt is now known-good and gets promoted
(`confirmed <- pending`) before the new attempt overwrites `pending`.
If live context instead still matches the *old* `confirmed` - meaning
`pending` never landed - `confirmed` is left exactly as it was and only
`pending` is replaced. Either way the light is still recognised as ours
and retried on the very next tick, rather than locked out. `confirmed`
is never evicted except by an observed match, so it survives any number
of consecutive dropped writes - the record needs exactly two slots, not
a growing history, to be self-healing (see async_record's own
docstring for the promotion logic in full, and grouping.py's
externally_set() for how the two slots are actually checked).

A light's very first-ever write (no prior record at all) has no
earlier `confirmed` claim to fall back on if it drops. Rather than
either staying lenient (which would leave a real external change in
that same narrow window unprotected) or going strict (which would
resurrect permanent lockout for exactly this case), the context.id
live *before* that first write is itself recorded as `confirmed` -
not because it's ours, but because "the light hasn't changed" is
already the retry signal every later dropped write relies on, and a
dropped first write leaves the light's context at exactly that
pre-write value. See async_record's docstring for the full reasoning.
"""

from __future__ import annotations

from typing import Optional, TypedDict

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

STORAGE_VERSION = 1
STORAGE_KEY = "adaptive_lighting_helpers.last_write_context_ids"

# Fired (with no payload - listeners re-read via snapshot()) whenever
# self._data changes, so the diagnostic sensor in sensor.py can refresh
# itself immediately instead of polling - see snapshot()'s own docstring.
SIGNAL_WRITE_TRACKING_UPDATED = "adaptive_lighting_helpers_write_tracking_updated"


class _ContextClaim(TypedDict):
    context_id: str
    owner_id: Optional[str]
    # ISO 8601, or None for the synthetic first-write baseline (see
    # async_record's docstring) - we only ever observe that context
    # after the fact, with no way to know how long it had already been
    # live, so a timestamp there would claim more precision than we
    # actually have. Lets the diagnostic sensor (and a dashboard card
    # built on it) show "how long has this been pending/confirmed" and
    # narrow a logbook lookup to resolve a context.id into what actually
    # happened, without guessing a search window.
    recorded_at: Optional[str]


class _WriteRecord(TypedDict):
    confirmed: Optional[_ContextClaim]
    pending: Optional[_ContextClaim]


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
                        "owner_id": value.get("owner_id"),
                        "recorded_at": None,
                    },
                    "pending": None,
                }
            # Anything else (the even older bare-string format, or
            # malformed data) is dropped - same safe "no record -> free"
            # fallback this integration has always used for data it
            # doesn't recognise.
        self._data = data

    def confirmed_context_id(self, entity_id: str) -> str | None:
        record = self._data.get(entity_id)
        claim = record.get("confirmed") if record else None
        return claim["context_id"] if claim else None

    def confirmed_owner_id(self, entity_id: str) -> str | None:
        record = self._data.get(entity_id)
        claim = record.get("confirmed") if record else None
        return claim.get("owner_id") if claim else None

    def pending_context_id(self, entity_id: str) -> str | None:
        record = self._data.get(entity_id)
        claim = record.get("pending") if record else None
        return claim["context_id"] if claim else None

    def pending_owner_id(self, entity_id: str) -> str | None:
        record = self._data.get(entity_id)
        claim = record.get("pending") if record else None
        return claim.get("owner_id") if claim else None

    async def async_record(
        self,
        entity_ids: list[str],
        live_context_before_write: dict[str, str | None],
        context_id: str,
        owner_id: str | None = None,
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
        grouping.py's externally_set()."""
        if not entity_ids:
            return
        for entity_id in entity_ids:
            old = self._data.get(entity_id)
            confirmed: Optional[_ContextClaim]
            if old is not None:
                old_pending = old.get("pending")
                if old_pending is not None and old_pending["context_id"] == live_context_before_write.get(entity_id):
                    confirmed = old_pending
                else:
                    confirmed = old.get("confirmed")
            else:
                baseline_context = live_context_before_write.get(entity_id)
                confirmed = (
                    {"context_id": baseline_context, "owner_id": None, "recorded_at": None}
                    if baseline_context is not None
                    else None
                )
            self._data[entity_id] = {
                "confirmed": confirmed,
                "pending": {"context_id": context_id, "owner_id": owner_id, "recorded_at": dt_util.utcnow().isoformat()},
            }
        await self._store.async_save(self._data)
        async_dispatcher_send(self._hass, SIGNAL_WRITE_TRACKING_UPDATED)

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
            # Only a genuine drop - a light that WAS on/off actually
            # going dark - should clear anything. Almost every entity
            # passes through unavailable/unknown as a routine part of
            # every HA restart (old_state is None - a fresh process's
            # state machine has no history yet - or already
            # unavailable/unknown itself), and treating that the same as
            # a real drop wiped override protection for practically
            # every managed light in the house on every restart, not
            # just ones that had actually dropped off the network - a
            # live incident: a light dimmed by hand hours after a
            # restart got silently overwritten on the next tick, because
            # its record had been cleared during that restart and never
            # rewritten since (nothing had needed a real write to it in
            # between).
            old_state = event.data["old_state"]
            if old_state is None or old_state.state in ("unavailable", "unknown"):
                return
            new_state = event.data["new_state"]
            if new_state is None or new_state.state not in ("unavailable", "unknown"):
                return
            del self._data[entity_id]
            hass.async_create_task(self._store.async_save(self._data))
            async_dispatcher_send(hass, SIGNAL_WRITE_TRACKING_UPDATED)

        return hass.bus.async_listen("state_changed", _on_state_changed)
