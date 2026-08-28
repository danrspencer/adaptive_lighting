"""
Decides, for a given entity, whether a write should be allowed through
or blocked because something else has touched the entity since - the
"override protection" mechanism. Which state device an entity's claims
belong to (its "scope") is decided by the caller, one layer up in
write_tracking.py; this module only classifies the claims it's handed,
with no notion of scope or caller identity of its own. Pure logic - no
`hass` instance needed, same
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
{brightness, color_temp_kelvin, rgb_color} shape, deliberately kept
concrete rather than generalised to arbitrary entity attributes.

Three consumers share this one decision table: grouping.py's
`EntityLookup.externally_set()`, sensor.py's diagnostic status, and the
standalone `check_control` service. Keeping them on one implementation
is the point - they previously drifted, and an off light was classified
"overridden" in one and "not excluded" in the other.

Each tracked entity carries two claims - `observed` and `latest`.

`observed` is a state we have seen and know is safe to write over. It
gets there several ways, only one of which is a write of our own: a
write an earlier call saw the bulb adopt, the pre-write baseline for an
entity's first-ever write, the snapshot taken at startup, or the one
taken when a device comes back from unavailable. What unites them is
not authorship but confidence - nothing unexplained has happened to the
light since.

`latest` is the most recent write we sent, not yet independently
re-observed. See
write_tracking.py's own module docstring for the full reasoning behind
the two-claim design, the first-write baseline, and the 5-second
Entity._context-expiry rescue `target_matches_values` exists for -
that reasoning lives there rather than being duplicated here, since
write_tracking.py is what actually persists these claims; this module
only classifies them.
"""

from __future__ import annotations

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
    # A two-step transition (no_combined_transition label) genuinely
    # issues two separate light.turn_on calls - brightness first, then
    # colour - each now given its own distinct context.id (see
    # __init__.py's _two_step_turn_on) rather than sharing one. Either
    # one landing counts as this claim having been observed - a device
    # whose real confirmation for the *first* step arrives (its own
    # context, matched here) before the second step's has necessarily
    # adopted this integration's own command, not something external.
    # None for a single combined-write claim, which only ever has one
    # context to begin with.
    secondary_context_id: Optional[str]
    # ISO 8601, or None for the synthetic first-write baseline (see
    # write_tracking.py's async_record docstring).
    recorded_at: Optional[str]
    # What this specific write actually asked for - {"brightness": int,
    # "color_temp_kelvin": int} or {"brightness": int, "rgb_color": [r,
    # g, b]} - or None for a claim that isn't a real write this
    # integration issued (an off-command, or one only ever observed).
    target: Optional[dict]


class _WriteRecord(TypedDict):
    observed: Optional[_ContextClaim]
    latest: Optional[_ContextClaim]
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


def _context_matches(claim: Optional[dict], current_context: Optional[str]) -> bool:
    """True if `current_context` equals either of a claim's two possible
    context ids - the primary one (every claim has this), or the
    secondary one a two-step transition's own first step gets recorded
    under (most claims don't have one at all - see _ContextClaim's own
    field comment)."""
    if claim is None:
        return False
    return current_context == claim["context_id"] or (
        claim.get("secondary_context_id") is not None and current_context == claim["secondary_context_id"]
    )


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


def _asked_for_off(claim: Optional[dict]) -> bool:
    """True if this claim's write was a turn-off. Recorded by
    apply_lighting as {"state": "off"}, which target_matches_values
    deliberately never matches - it compares brightness and colour, and
    an off light has neither."""
    if not claim:
        return False
    target = claim.get("target") or {}
    return target.get("state") == "off"


def classify(
    is_on: bool,
    observed: Optional[dict],
    latest: Optional[dict],
    current_context: Optional[str],
    current_brightness=None,
    current_color_temp_kelvin=None,
    current_rgb_color=None,
    brightness_tolerance: int = 2,
    color_temp_tolerance: int = 10,
    rgb_color_tolerance: int = 10,
) -> tuple[str, Optional[str]]:
    """The decision table - given everything known about one entity right
    now, returns `(status, matched_via)`.

    - `"off"` - not on. Override protection is moot; checked first,
      before any claim.
    - `"untracked"` - no claim at all, or only a single unverified
      `latest` attempt that doesn't match live state. Not enough
      evidence to call it external, so free to manage. Also what a
      light deliberately handed off to a scene or a hands-off
      multiplier reads as, once the blueprint releases it. This is the
      one gap the two-claim design doesn't close: a light's very first
      tracked write, if that's the one that drops, is indistinguishable
      from a genuine external change until an `observed` baseline
      exists.
    - `"controlled"` - we are in control. Either the live context
      matches a claim, or it matches neither but the current value
      still matches what a claim asked for (`latest` checked first,
      then `observed`). Deliberately one status rather than two: from
      any caller's point of view these are the same situation, and
      `is_blocked()` has always treated them identically. Which claim
      matched, and how, is reported separately in `matched_via` - that
      is diagnostic detail, not a different outcome.

      Two separate reasons a context alone can't be trusted, one per
      claim: `latest`, because Entity._context expires 5s after the
      call that set it, so a slow device confirms under an unrelated
      context while echoing exactly what was asked; `observed`, because
      a light that never adopted the latest write is by definition
      still showing what the last landed write asked for. Without both,
      such a light reads as external and - since nothing un-marks it
      while it stays on - is excluded from every future write the
      instant it next needs a different value.
    - `"overridden"` - an `observed` claim exists and neither claim
      matches, by context or by value.

    Context matching covers *either* of a claim's two context ids; most
    claims have one, a two-step transition's has two.

    `matched_via` names which claim matched and how -
    `"latest-context"`, `"latest-value"`, `"observed-context"` or
    `"observed-value"` - and is `None` for every other status. Purely
    diagnostic (the write-tracking sensor and card), never used for
    decisions here or in `is_blocked()`. This is where the detail that
    used to be split across two statuses now lives: `latest-*` means the
    most recent write is what the bulb is showing, `observed-*` means it
    isn't, and an older write is."""
    if observed is None and latest is None:
        return ("untracked" if is_on else "off"), None
    if _context_matches(latest, current_context):
        return "controlled", "latest-context"
    if _context_matches(observed, current_context):
        return "controlled", "observed-context"
    if not is_on:
        # Off is a state a claim can ask for, so it is compared like any
        # other: an off light matches only a claim that asked for off.
        # A claim asking for brightness means somebody else turned this
        # light off, which is an override.
        if _asked_for_off(latest):
            return "controlled", "latest-value"
        if _asked_for_off(observed):
            return "controlled", "observed-value"
        if observed is None:
            return "untracked", None
        return "overridden", None
    if observed is None:
        return "untracked", None
    if latest is not None and target_matches_values(
        latest.get("target"),
        current_brightness,
        current_color_temp_kelvin,
        current_rgb_color,
        brightness_tolerance,
        color_temp_tolerance,
        rgb_color_tolerance,
    ):
        return "controlled", "latest-value"
    if target_matches_values(
        observed.get("target"),
        current_brightness,
        current_color_temp_kelvin,
        current_rgb_color,
        brightness_tolerance,
        color_temp_tolerance,
        rgb_color_tolerance,
    ):
        return "controlled", "observed-value"
    return "overridden", None


def is_blocked(status: str, force: bool = False) -> bool:
    """Turns classify()'s raw status into the actual yes/no "should this
    write be blocked" decision - the one remaining step both grouping.py's
    EntityLookup.externally_set() and the check_control service need on
    top of the shared classification, kept here so neither re-derives it.

    There is no owner comparison to make. Which state device an
    entity's claims live on is the caller's own choice, made once per
    call (see write_tracking.py) - a `controlled` claim is by
    construction the claim of whatever scope the caller named. Two
    callers naming the same scope for one light write into the same
    claims and therefore co-operate, instead of each reading the other
    as an intruder.

    `force` still bypasses outright."""
    if force:
        return False
    return status == "overridden"
