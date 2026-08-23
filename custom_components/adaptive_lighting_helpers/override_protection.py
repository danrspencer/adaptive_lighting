"""
Decides, for a given entity and caller (`owner_id`), whether a write
should be allowed through or blocked because something else has
touched the entity since this caller's own last write - the "override
protection" mechanism. Pure logic - no `hass` instance needed, same
pattern as curve.py/scenes.py/grouping.py: HA access (live state,
Store-backed persistence, the unavailable/recovery listener) stays in
write_tracking.py, which is the thing that actually calls this module.
Does import `homeassistant.util.color` directly (for the Kelvin/mired
rounding `_color_temp_matches` needs) - a deliberate exception to
"zero `homeassistant.*` imports", not an oversight: that import is
cheap (no core/event-loop machinery), it's the actual function HA
itself uses (no risk of drifting from real device behaviour), and the
"these tests run without HA installed" property this was nominally
protecting doesn't hold in practice anyway - pytest's own `testpaths`
config already pulls in `tests/integration/conftest.py`, which
requires `pytest-homeassistant-custom-component`, regardless of which
test file is targeted.

Genuinely generic, not specific to adaptive lighting or even to
lights - the only light-flavoured piece is `target_matches_values`'s
{brightness, color_temp_kelvin, rgb_color} shape (kept as-is rather
than generalised to arbitrary entity attributes - a deliberate,
smaller-scope choice; see CLAUDE.md). Extracted out of grouping.py's
`EntityLookup.externally_set()`, where this same logic used to live
inline, coupled to that module's brightness-bucketing concerns only
through the EntityLookup dataclass boundary - grouping.py's
`externally_set()` is now a thin adapter calling `classify()` below,
and the two new `check_ownership`/`record_ownership` services
(__init__.py) expose the identical mechanism standalone, for any
caller that wants it without any of adaptive_lighting_helpers' curve/
brightness logic at all.

Each tracked entity has not one recorded write but two - `confirmed`
(a write some *earlier* call actually observed landing) and `pending`
(the most recent attempt, not yet verified either way). See
write_tracking.py's own module docstring for the full reasoning behind
the two-claim design, the first-write baseline, and the 5-second
Entity._context-expiry rescue `target_matches_values` exists for -
that reasoning lives there rather than being duplicated here, since
write_tracking.py is what actually persists these claims; this module
only classifies them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, TypedDict

# The real function Home Assistant itself uses (homeassistant/util/color.py) -
# imported directly rather than reimplemented. Cheap to import on its own
# (no core/config_entries/event-loop machinery pulled in - confirmed before
# relying on it), and this module's "no HA dependency" property was already
# not buying a HA-free test run in practice: pytest's own testpaths config
# collects tests/integration/conftest.py (which requires
# pytest-homeassistant-custom-component) regardless of which test file is
# targeted, so the whole suite already needs HA installed either way.
from homeassistant.util.color import color_temperature_kelvin_to_mired as _kelvin_to_mired


class _ContextClaim(TypedDict):
    context_id: str
    owner_id: Optional[str]
    # ISO 8601, or None for the synthetic first-write baseline (see
    # write_tracking.py's async_record docstring).
    recorded_at: Optional[str]
    # What this specific write actually asked for - {"brightness": int,
    # "color_temp_kelvin": int} or {"brightness": int, "rgb_color": [r,
    # g, b]} - or None for a claim that isn't a real write this
    # integration issued (an off-command, or one only ever observed).
    target: Optional[dict]


class _WriteRecord(TypedDict):
    confirmed: Optional[_ContextClaim]
    pending: Optional[_ContextClaim]
    # ISO 8601 - the last time this record was written or observed (a
    # real write, or a startup/recovery resync). Pure write_tracking.py
    # bookkeeping for its own staleness pruning (async_prune_stale) -
    # classify() never reads this, it has no bearing on what a record
    # currently means, only on how long it's allowed to keep existing.
    last_seen: Optional[str]


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# Live incident, 2026-08-23: a Night->Day phase transition writes every
# light in a room (often 10-20+ across a shared Zigbee mesh) in one
# burst. On the very next tick - normally 30-75s later
# (update_interval + update_jitter) - any device whose real round-trip
# confirmation hadn't landed yet reports back with BOTH its context.id
# and its brightness/colour still unchanged from before the write, so
# neither the context check nor the value-rescue (target_matches_values,
# below) can match yet - classify() concluded "overridden" a full tick
# too early, and because an excluded entity is never written again (see
# write_tracking.py's async_record docstring - only entities actually
# written get a fresh claim), that one early misjudgment turned into a
# permanent lockout across a dozen-plus lights in two rooms at once,
# recoverable only via clear_ownership. 120s (confirmed with the user)
# covers a worst-case tick interval plus a full extra tick for a
# genuinely slow/congested round-trip, without leaving a real external
# override unrecognised for too long - the failure mode of being too
# lenient here (we might overwrite a manual change slightly later than
# today) is far cheaper than the failure mode of being too strict (a
# permanent lockout nobody notices until the next phase change, exactly
# what this fixes).
PENDING_GRACE_SECONDS = 120


def _pending_still_fresh(pending: Optional[dict], now: Optional[datetime], grace_seconds: int) -> bool:
    """True if `pending` was recorded recently enough that a genuine
    device round-trip might simply not have landed yet - the same
    "not enough evidence to call this external" leniency classify()
    already gives a light's very first-ever write (see its own
    "unclaimed" case), extended to any pending claim while it's still
    fresh, not just when there's no confirmed baseline at all. `now`
    is passed in rather than read here (datetime.now()/dt_util.utcnow())
    so this stays pure and deterministic for testing - a caller that
    doesn't pass `now` gets no grace period at all (the original,
    stricter behaviour), not an exception."""
    if pending is None or now is None:
        return False
    recorded_at = pending.get("recorded_at")
    if not recorded_at:
        return False
    try:
        recorded = datetime.fromisoformat(recorded_at)
    except ValueError:
        return False
    return (now - recorded).total_seconds() <= grace_seconds


def _color_temp_matches(current_kelvin: int, target_kelvin: int, tolerance_kelvin: int) -> bool:
    """True if within the ordinary Kelvin tolerance (unchanged, user-
    tunable), OR if both values would floor to the identical mired
    integer - not a heuristic in that second case, a hard equivalence:
    a Zigbee bulb communicates colour temperature in mireds
    (1,000,000/kelvin, always a whole number), so two Kelvin values
    that round-trip to the same mired reading are indistinguishable to
    the device - it genuinely cannot have done anything other than
    exactly what it was told. A flat Kelvin tolerance alone doesn't
    reliably cover this: a single mired step is worth ~5K near 2700K
    but ~20K+ near 4500K, so the same tolerance number is far too loose
    at one end of the range and far too tight at the other. Confirmed
    against a live incident: asked for 4373K, HA's own
    color_temperature_kelvin_to_mired floors that to mired 228, and
    flooring 228 back gives 4385K - exactly what the bulb reported,
    with zero drift and nothing external having touched it."""
    if abs(current_kelvin - target_kelvin) <= tolerance_kelvin:
        return True
    return _kelvin_to_mired(current_kelvin) == _kelvin_to_mired(target_kelvin)


def target_matches_values(
    target: Optional[dict],
    current_brightness,
    current_color_temp_kelvin,
    current_rgb_color,
    brightness_tolerance: int = 2,
    color_temp_tolerance: int = 10,
    rgb_color_tolerance: int = 10,
) -> bool:
    """Pure comparison (plain values in, no entity_id/lookup) - shared
    by classify()'s own context-mismatch fallback below and sensor.py's
    diagnostic status classification, so both agree on what counts as
    "still matches what we asked for" even though a context.id says
    otherwise. `target` is a claim's recorded {"brightness": ...,
    "color_temp_kelvin": ...} or {"brightness": ..., "rgb_color":
    [...]} - falsy (None, or an entity missing from a targets dict)
    never matches, same as no claim at all."""
    if not target:
        return False
    target_brightness = target.get("brightness")
    if target_brightness is None:
        return False
    if abs(_as_int(current_brightness, -999) - target_brightness) > brightness_tolerance:
        return False
    target_rgb = target.get("rgb_color")
    if target_rgb is not None:
        return (
            isinstance(current_rgb_color, (list, tuple))
            and len(current_rgb_color) == 3
            and all(abs(a - b) <= rgb_color_tolerance for a, b in zip(current_rgb_color, target_rgb))
        )
    target_color_temp = target.get("color_temp_kelvin")
    if target_color_temp is None:
        return False
    return _color_temp_matches(_as_int(current_color_temp_kelvin, -999), target_color_temp, color_temp_tolerance)


def classify(
    is_on: bool,
    confirmed: Optional[dict],
    pending: Optional[dict],
    current_context: Optional[str],
    current_brightness=None,
    current_color_temp_kelvin=None,
    current_rgb_color=None,
    brightness_tolerance: int = 2,
    color_temp_tolerance: int = 10,
    rgb_color_tolerance: int = 10,
    now: Optional[datetime] = None,
    pending_grace_seconds: int = PENDING_GRACE_SECONDS,
) -> tuple[str, Optional[str], Optional[str]]:
    """The actual decision logic - given everything known about one
    entity right now, returns `(status, claim_owner_id, matched_via)`:

    - `"off"` - not currently on. Override protection is entirely moot
      for a light that isn't on - checked first, before any claim.
    - `"unclaimed"` - no `confirmed`/`pending` claim at all (a brand
      new entity, or the very first tick before anything's been
      recorded), or only a single unconfirmed `pending` attempt that
      doesn't match live state either - there isn't enough evidence
      yet to call this external, so free to manage either way. This is
      the one gap the two-claim design doesn't close (see
      write_tracking.py's module docstring): a light's very first
      tracked write, if it happens to be the one that gets dropped, is
      indistinguishable from a genuinely external change until a
      `confirmed` baseline exists to check against.
    - `"pending"` - live context matches the most recent write
      attempt, not yet independently reconfirmed by a later tick.
    - `"controlled"` - live context matches `confirmed` (settled), OR
      matches neither claim but the entity's current value still
      matches what `pending` itself asked for (see
      `target_matches_values` - HA's own Entity._context expires 5
      seconds after the service call that set it, so a device whose
      real confirmation takes longer than that reports back under a
      brand-new, unrelated context even though it's echoing exactly
      the value asked for; without this rescue that echo reads as
      external and, since nothing else ever un-marks it while the
      entity stays continuously on, it would be silently excluded from
      every future write, forever, the instant it next needed a
      genuinely different value - confirmed live: light.kitchen_3/
      light.kitchen_5 stuck exactly this way for over an hour, still
      correctly lit the whole time).
    - `"pending"` (via the grace period) - a `confirmed` claim exists
      and neither it nor `pending` matches by context or value, but
      `pending` was recorded too recently (`pending_grace_seconds`,
      default `PENDING_GRACE_SECONDS`) for a genuine device round-trip
      to plausibly have landed yet - `matched_via="grace"` distinguishes
      this from an ordinary context-matched "pending" for a diagnostic
      viewer. Only reachable when `now` is actually supplied; a caller
      that omits it gets the original, stricter behaviour (falls
      straight through to "overridden" below).
    - `"overridden"` - a `confirmed` claim exists and neither it nor
      `pending` matches, by context *or* value, and `pending` (if any)
      is old enough that a round-trip delay no longer explains it.
      Something else has genuinely touched this entity since either
      recorded write.

    `claim_owner_id` is the matching claim's recorded owner - `None`
    for `"off"`/`"unclaimed"`/`"overridden"` (nothing to attribute),
    populated for `"pending"`/`"controlled"` (including the value-
    rescue case, which returns `pending`'s own owner) so a caller that
    needs to know whether *this specific* claim belongs to *them* can
    do that comparison on top - deliberately not resolved here, since
    that comparison only matters to a caller asking "is this mine",
    never to something just displaying the raw classification (e.g.
    sensor.py's diagnostic status, which shows every claim's owner
    directly regardless of who's asking).

    `matched_via` says *how* a `"pending"`/`"controlled"` status was
    reached - `"context"` (the live context.id equalled the claim's
    own recorded context.id directly), `"value"` (context.id didn't
    match anything, but the entity's current value still matched what
    `pending` asked for - the delayed-echo/mired rescue), or `"grace"`
    (neither context nor value matched anything yet, but `pending` is
    too recent to conclude anything - the round-trip may simply still
    be in flight). `None` for every other status, where there's nothing
    to explain. This is genuinely new information `status` alone
    doesn't carry - context-matched, value-rescued, and grace-period
    cases can all report the same status otherwise - and exists purely
    for a diagnostic consumer (the write-tracking sensor/dashboard
    card) to show *why*, not for any decision logic here or in
    `is_blocked()`.

    Note this is a real behaviour change from an earlier version of
    this check, not just a move: the value-rescue case used to return
    "not externally set" unconditionally, with no owner comparison at
    all - meaning an entity whose live value happened to match a
    *different* owner's `pending` target would have incorrectly read
    as free-to-manage for anyone. The other two matched cases
    (context-matches-pending, context-matches-confirmed) always did
    check the owner first; returning `pending`'s owner here makes all
    three matched cases go through the identical caller-side owner
    check uniformly."""
    if not is_on:
        return "off", None, None
    if confirmed is None and pending is None:
        return "unclaimed", None, None
    if pending is not None and current_context == pending["context_id"]:
        return "pending", pending.get("owner_id"), "context"
    if confirmed is not None and current_context == confirmed["context_id"]:
        return "controlled", confirmed.get("owner_id"), "context"
    if confirmed is None:
        return "unclaimed", None, None
    if pending is not None and target_matches_values(
        pending.get("target"),
        current_brightness,
        current_color_temp_kelvin,
        current_rgb_color,
        brightness_tolerance,
        color_temp_tolerance,
        rgb_color_tolerance,
    ):
        return "controlled", pending.get("owner_id"), "value"
    if _pending_still_fresh(pending, now, pending_grace_seconds):
        return "pending", pending.get("owner_id"), "grace"
    return "overridden", None, None


def is_blocked(status: str, claim_owner_id: Optional[str], owner_id: Optional[str], force: bool = False) -> bool:
    """Turns classify()'s raw `(status, claim_owner_id)` into the actual
    yes/no "should this write be blocked" decision for one specific
    caller - the one remaining step both grouping.py's
    EntityLookup.externally_set() and the check_ownership service need
    on top of the shared classification, kept here too so neither
    re-derives it independently.

    `force` bypasses outright, regardless of `owner_id`. `owner_id=None`
    also bypasses (skip the check altogether) but - unlike `force` -
    doesn't claim anything for a later call to recognise. See
    write_tracking.py's module docstring for the full force-vs-
    omitted-owner_id distinction."""
    if force or owner_id is None:
        return False
    if status in ("off", "unclaimed"):
        return False
    if status == "overridden":
        return True
    # "pending" or "controlled" - blocked only if the matching claim
    # belongs to someone else.
    return claim_owner_id is not None and claim_owner_id != owner_id
