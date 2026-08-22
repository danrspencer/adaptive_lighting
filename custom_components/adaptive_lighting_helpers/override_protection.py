"""
Decides, for a given entity and caller (`owner_id`), whether a write
should be allowed through or blocked because something else has
touched the entity since this caller's own last write - the "override
protection" mechanism. Pure logic, no Home Assistant dependency, same
pattern as curve.py/scenes.py/grouping.py: HA access (live state,
Store-backed persistence, the unavailable/recovery listener) stays in
write_tracking.py, which is the thing that actually calls this module.

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

from typing import Optional, TypedDict


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


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
    return abs(_as_int(current_color_temp_kelvin, -999) - target_color_temp) <= color_temp_tolerance


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
) -> tuple[str, Optional[str]]:
    """The actual decision logic - given everything known about one
    entity right now, returns `(status, claim_owner_id)`:

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
    - `"overridden"` - a `confirmed` claim exists and neither it nor
      `pending` matches, by context *or* value. Something else has
      genuinely touched this entity since either recorded write.

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
        return "off", None
    if confirmed is None and pending is None:
        return "unclaimed", None
    if pending is not None and current_context == pending["context_id"]:
        return "pending", pending.get("owner_id")
    if confirmed is not None and current_context == confirmed["context_id"]:
        return "controlled", confirmed.get("owner_id")
    if confirmed is None:
        return "unclaimed", None
    if pending is not None and target_matches_values(
        pending.get("target"),
        current_brightness,
        current_color_temp_kelvin,
        current_rgb_color,
        brightness_tolerance,
        color_temp_tolerance,
        rgb_color_tolerance,
    ):
        return "controlled", pending.get("owner_id")
    return "overridden", None


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
