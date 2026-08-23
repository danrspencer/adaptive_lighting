"""
Turns "these entities, this target brightness/colour-temperature" into
the minimal set of light.turn_on/turn_off calls actually needed.

Pure logic - HA access (current state, attributes, device/label
lookups) is injected via an EntityLookup so this is testable with
plain pytest and fakes, and so the integration's __init__.py
(custom_components/adaptive_lighting_helpers/__init__.py) stays a thin
adapter registering this as a standalone HA service. Transitively
imports homeassistant.util.color (via override_protection.py's own
_color_temp_matches, used below in _already_set) - see that module's
own docstring for why that's a deliberate exception rather than an
oversight.

This is a direct port of what used to be the blueprint's repeat-loop
`variables:` block (powerable_entities / multiplier_groups /
group_needing_off / group_needing_update / group_two_step /
group_combined) - same behaviour, same defaults, just Python instead of
namespace-loop Jinja.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

try:
    # Real package context (production HA, tests/integration/) - grouping.py
    # is imported as custom_components.adaptive_lighting_helpers.grouping.
    from .override_protection import (  # noqa: F401 (classify/target_matches_values re-exported for sensor.py)
        _color_temp_matches,
        classify,
        is_blocked,
        target_matches_values,
    )
except ImportError:
    # Bare top-level module context (tests/test_grouping.py, via
    # tests/conftest.py putting this directory straight on sys.path -
    # see its own comment for why). override_protection.py sits
    # alongside this file, so a plain top-level import resolves the
    # same way curve.py/scenes.py already do for their own bare-module
    # test usage. Note this module now needs homeassistant importable
    # either way - see override_protection.py's own docstring.
    from override_protection import _color_temp_matches, classify, is_blocked, target_matches_values  # noqa: F401

_RGB_COLOR_MODES = {"rgb", "rgbw", "rgbww", "hs", "xy"}

# Home Assistant's own brightness scale. light.turn_on validates with
# vol.Clamp(min=0, max=255), so it silently accepts an out-of-range value
# and writes the clamped one - which is exactly what makes an unclamped
# target here dangerous rather than merely wrong: the light reports 255,
# _already_set compares it against the un-clamped target, never finds it
# within tolerance, and re-commands the light on every single tick
# forever. Clamping here keeps our idea of "at target" identical to what
# the light can actually report.
MAX_BRIGHTNESS = 255


@dataclass
class EntityLookup:
    """Home Assistant state/registry access, injected so this module
    never touches `hass` directly."""

    is_state: Callable[[str, str], bool]
    state_attr: Callable[[str, str], object]
    device_id: Callable[[str], Optional[str]]
    labels: Callable[[str], list]
    context_id: Callable[[str], Optional[str]]
    # Two independent claims per entity, not one - see write_tracking.py's
    # module docstring for why. "confirmed" is a write some earlier call
    # actually observed landing; "pending" is the most recent attempt,
    # not yet verified either way.
    confirmed_context_id: Callable[[str], Optional[str]]
    confirmed_owner_id: Callable[[str], Optional[str]]
    pending_context_id: Callable[[str], Optional[str]]
    pending_owner_id: Callable[[str], Optional[str]]
    # What the *pending* claim's write actually intended - {brightness,
    # color_temp_kelvin} or {brightness, rgb_color}, or None if that
    # claim isn't a real apply_lighting write (an off-command, or a
    # write_tracking-observed baseline rather than one we issued). Lets
    # externally_set() below tell "our own write, echoed back under an
    # unrelated context" apart from a genuine external change - see its
    # own docstring.
    pending_target: Callable[[str], Optional[dict]]

    def reachable(self, entity_id: str) -> bool:
        """False for anything HA already knows it can't reach - no point commanding it."""
        return not self.is_state(entity_id, "unavailable") and not self.is_state(entity_id, "unknown")

    def tags(self, entity_id: str) -> list:
        """Labels on the entity itself plus its device (if any)."""
        did = self.device_id(entity_id)
        return self.labels(entity_id) + (self.labels(did) if did else [])

    def externally_set(
        self,
        entity_id: str,
        owner_id: Optional[str] = None,
        force: bool = False,
        brightness_tolerance: int = 2,
        color_temp_tolerance: int = 10,
        rgb_color_tolerance: int = 10,
    ) -> bool:
        """True if the entity is on and something other than *this*
        caller's own last apply_lighting write has touched it since - a
        person, another automation (even one with no context.user_id of
        its own, such as one triggered directly by a physical button -
        the gap context.user_id-based detection used to miss), a device
        regaining power under a fresh context, or a *different*
        apply_lighting caller (identified by owner_id, e.g. a different
        room's automation).

        force bypasses the check outright, regardless of owner_id -
        always returns False. Distinct from omitting owner_id (below):
        force still lets the caller claim an identity for the write that
        follows, so a *later* call - not forced, but with the same
        owner_id - correctly recognises that write as its own rather
        than finding an orphaned record with no claimed owner. Without
        this, a forced write would leave the entity looking permanently
        externally-set to its own regular caller from the very next
        tick onward - the actual bug that prompted adding this parameter
        (surfaced by asking, before shipping, "if we do one run without
        an owner id, will subsequent runs with one continue to work?" -
        they would not have, without this).

        owner_id is the caller's own identity for this call, entirely
        optional: pass None (the default - "I don't care who touched
        this last") to skip the check altogether too, same as force,
        but - unlike force - without claiming anything for later calls
        to recognise.

        Two independent claims are checked, not one - see
        write_tracking.py's module docstring for the full reasoning.
        `confirmed` is a write some earlier call actually observed
        landing; `pending` is the most recent attempt, not yet verified
        either way (apply_lighting records the context it *issues*, not
        one it's confirmed the device adopted - the two are asynchronous,
        so a dropped command still gets optimistically recorded). Passing
        an owner_id (and force left False) asks, in order:

        1. Does the entity's current context.id match `pending`? If so,
           the most recent write landed - not externally set, subject to
           the owner check below.
        2. If not, does it match `confirmed` instead? If so, `pending`
           never landed (the device dropped it, or hasn't caught up yet)
           but the light is still ours as of the last write that
           *did* land - not externally set, same owner check.
        3. If neither matches, and there's no `confirmed` at all yet -
           only ever one unconfirmed attempt has been made, and it
           doesn't match either - there isn't enough evidence yet to
           call this external. Stay lenient (not externally set) rather
           than lock the light out over a single write whose fate is
           still unknown; this is the one gap the two-claim design
           doesn't close (see write_tracking.py's module docstring) -
           a light's very first tracked write, if it happens to be the
           one that gets dropped, is indistinguishable from a genuinely
           external change until a `confirmed` baseline exists to check
           against.
        4. Otherwise (a `confirmed` claim exists and neither it nor
           `pending` matches): genuinely externally set - UNLESS the
           `pending` claim recorded what it was actually trying to write
           (brightness/color_temp_kelvin, or brightness/rgb_color) and
           the entity's *current* reported values still match that
           within the same tolerance apply_lighting itself uses to
           decide "already correct". A context mismatch alone doesn't
           prove someone else touched the light - HA's own Entity._context
           expires 5 seconds after the service call that set it
           (confirmed against homeassistant/core.py), so a real device
           whose Zigbee/MQTT round-trip confirmation takes longer than
           that reports back under a brand-new, unrelated context even
           though it's echoing exactly the value we asked for. Without
           this check that echo reads as an external touch and - because
           nothing here ever un-marks it while the light stays
           continuously on (see the module docstring's "naturally stops
           being true" note, which only covers *turning off*) - the
           light is silently excluded from every future tick, forever,
           the instant it next needs a genuinely different value.
           Confirmed live: light.kitchen_3/light.kitchen_5 stuck exactly
           this way for over an hour, still correctly lit the whole
           time, invisible until the phase next changed and they didn't
           follow. Deliberately checked against `pending` (the most
           recent attempt) rather than `confirmed` - `pending` is what
           this specific echo would be confirming.

        The owner check, wherever a context matches: a claim's owner_id
        of None doesn't count against anyone (no claim was ever made);
        otherwise it must equal this caller's own owner_id, or the
        matching write belongs to a *different* apply_lighting caller
        (e.g. a different room's automation) and counts as external too,
        even though nothing about the light's own context has changed.

        No remembered write at all (a brand new entity, or the very
        first tick before anything's been recorded) counts as free to
        manage either way - the same "don't block on missing
        provenance" behaviour a restart used to fall back to under the
        old check. Checked fresh against live state every call, so
        there's nothing to expire: once a light is turned off, this
        naturally stops being true on its own (the is_state check above
        fails first). A device recovering from unavailable is a
        different case, handled entirely by write_tracking.py clearing
        the whole record (both claims) the moment the entity is observed
        going unavailable - by the time it reconnects there's nothing
        left here to compare against at all, so it falls into "no
        remembered write," not into the lenient-pending case above.

        This method is now a thin adapter: the actual decision table
        lives in override_protection.classify()/is_blocked(), shared
        with sensor.py's diagnostic status classification (previously a
        second, separately-maintained copy of this same logic that had
        quietly drifted - see classify()'s own module for the full
        story) and with the standalone check_ownership/record_ownership
        services this same mechanism is also exposed as."""
        confirmed_ctx = self.confirmed_context_id(entity_id)
        confirmed = {"context_id": confirmed_ctx, "owner_id": self.confirmed_owner_id(entity_id)} if confirmed_ctx is not None else None
        pending_ctx = self.pending_context_id(entity_id)
        pending = (
            {
                "context_id": pending_ctx,
                "owner_id": self.pending_owner_id(entity_id),
                "target": self.pending_target(entity_id),
            }
            if pending_ctx is not None
            else None
        )

        status, claim_owner, _matched_via = classify(
            self.is_state(entity_id, "on"),
            confirmed,
            pending,
            self.context_id(entity_id),
            self.state_attr(entity_id, "brightness"),
            self.state_attr(entity_id, "color_temp_kelvin"),
            self.state_attr(entity_id, "rgb_color"),
            brightness_tolerance,
            color_temp_tolerance,
            rgb_color_tolerance,
        )
        return is_blocked(status, claim_owner, owner_id, force)

    def supports_rgb(self, entity_id: str) -> bool:
        """True if the entity's supported_color_modes includes any mode
        HA's light.turn_on rgb_color param works with. A derived method
        (built from the existing state_attr primitive) rather than a new
        injected closure - no change needed to __init__.py's
        _build_lookup() or tests/fakes.py's make_lookup()."""
        modes = self.state_attr(entity_id, "supported_color_modes") or []
        return bool(set(modes) & _RGB_COLOR_MODES)


@dataclass
class Group:
    multiplier: float
    brightness: int
    needing_off: list = field(default_factory=list)
    combined: list = field(default_factory=list)
    two_step: list = field(default_factory=list)
    combined_rgb: list = field(default_factory=list)
    two_step_rgb: list = field(default_factory=list)


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bucket_by_multiplier(entities: list, brightness_multipliers: dict) -> dict:
    """Groups entities whose multiplier isn't null/false (that means
    "don't touch this on power-on, something else owns it" - see the
    blueprint's brightness_multiplier_template input) by multiplier
    value, so each bucket can share one command."""
    buckets: dict = {}
    for e in entities:
        m = brightness_multipliers.get(e, 1)
        if m is None or m is False:
            continue
        buckets.setdefault(m, []).append(e)
    return buckets


def build_groups(
    entities: list,
    brightness_multipliers: dict,
    sensor_brightness: int,
    sensor_color_temp_kelvin: int,
    lookup: EntityLookup,
    brightness_tolerance: int = 2,
    color_temp_tolerance: int = 10,
    two_step_label: str = "no_combined_transition",
    prefer_rgb_color: bool = False,
    rgb_color: Optional[tuple] = None,
    rgb_color_tolerance: int = 10,
    owner_id: Optional[str] = None,
    force: bool = False,
) -> list:
    """Compute exactly what needs commanding for `entities`, bucketed by
    brightness multiplier. Each returned Group is either an off-group
    (multiplier <= 0, only `needing_off` populated) or an update-group
    (multiplier > 0, `combined`/`two_step`/`combined_rgb`/`two_step_rgb`
    populated with whatever isn't already within tolerance of the
    target).

    prefer_rgb_color/rgb_color: when both are set, entities within each
    bucket that support RGB (lookup.supports_rgb()) are routed into
    combined_rgb/two_step_rgb instead of combined/two_step, targeting
    rgb_color instead of sensor_color_temp_kelvin. Toggle off, or no
    rgb_color given, and combined_rgb/two_step_rgb are always empty -
    behaviour is otherwise identical to before this parameter existed.

    owner_id/force: this caller's own identity and whether to bypass
    protection outright, passed straight through to every
    EntityLookup.externally_set() check - see its docstring for the full
    semantics, including why force (not just omitting owner_id) is the
    right way to bypass protection for a caller that still wants its
    write recognised as its own next time."""
    use_rgb = prefer_rgb_color and rgb_color is not None
    groups = []
    for multiplier, group_entities in _bucket_by_multiplier(entities, brightness_multipliers).items():
        m = float(multiplier)
        # Clamped at both ends: floored at 1 so a tiny multiplier still
        # leaves the light on rather than silently off (0 means off, and
        # that's the multiplier's job to say explicitly), and capped at
        # MAX_BRIGHTNESS so a multiplier above 1 is a plain "as bright as
        # it goes" rather than something a template has to do arithmetic
        # against the current curve value to avoid.
        brightness = 0 if m == 0 else min(max(round(sensor_brightness * m), 1), MAX_BRIGHTNESS)
        group = Group(multiplier=multiplier, brightness=brightness)

        if brightness <= 0:
            group.needing_off = [
                e
                for e in group_entities
                if lookup.reachable(e)
                and not lookup.is_state(e, "off")
                and not lookup.externally_set(e, owner_id, force, brightness_tolerance, color_temp_tolerance, rgb_color_tolerance)
            ]
            groups.append(group)
            continue

        if use_rgb:
            rgb_entities = [e for e in group_entities if lookup.supports_rgb(e)]
            temp_entities = [e for e in group_entities if e not in rgb_entities]
        else:
            rgb_entities, temp_entities = [], group_entities

        needing_update = [
            e
            for e in temp_entities
            if lookup.reachable(e)
            and not lookup.externally_set(e, owner_id, force, brightness_tolerance, color_temp_tolerance, rgb_color_tolerance)
            and not _already_set(e, brightness, sensor_color_temp_kelvin, lookup, brightness_tolerance, color_temp_tolerance)
        ]
        group.two_step = [e for e in needing_update if two_step_label in lookup.tags(e)]
        group.combined = [e for e in needing_update if e not in group.two_step]

        needing_update_rgb = [
            e
            for e in rgb_entities
            if lookup.reachable(e)
            and not lookup.externally_set(e, owner_id, force, brightness_tolerance, color_temp_tolerance, rgb_color_tolerance)
            and not _already_set_rgb(e, brightness, rgb_color, lookup, brightness_tolerance, rgb_color_tolerance)
        ]
        group.two_step_rgb = [e for e in needing_update_rgb if two_step_label in lookup.tags(e)]
        group.combined_rgb = [e for e in needing_update_rgb if e not in group.two_step_rgb]

        groups.append(group)

    return groups


def _brightness_close(entity_id: str, target_brightness: int, lookup: EntityLookup, brightness_tolerance: int) -> bool:
    """Shared by _already_set and _already_set_rgb - brightness tolerance
    doesn't depend on which colour representation is in play, so there's
    only one copy of the "how close counts as close enough" check for it."""
    current_brightness = _as_int(lookup.state_attr(entity_id, "brightness"), -999)
    return abs(current_brightness - target_brightness) <= brightness_tolerance


def _already_set(
    entity_id: str,
    target_brightness: int,
    target_color_temp_kelvin: int,
    lookup: EntityLookup,
    brightness_tolerance: int,
    color_temp_tolerance: int,
) -> bool:
    """Within tolerance (not exact match) because some bulbs round-trip
    brightness/colour-temp a point or two off from what was actually
    sent - an exact-match check would recommand them forever. Colour
    temperature also gets the mired-equivalence check on top of the
    plain Kelvin tolerance (_color_temp_matches) - a target Kelvin
    value that round-trips through a real device's native mired unit
    to a *different* Kelvin reading is still "already set", not a
    genuine mismatch; without this, a light could be needlessly
    re-commanded every single tick purely from that unit-conversion
    rounding, never actually settling into "no write needed"."""
    if not lookup.is_state(entity_id, "on"):
        return False
    if not _brightness_close(entity_id, target_brightness, lookup, brightness_tolerance):
        return False
    current_color_temp = _as_int(lookup.state_attr(entity_id, "color_temp_kelvin"), -999)
    return _color_temp_matches(current_color_temp, target_color_temp_kelvin, color_temp_tolerance)


def _already_set_rgb(
    entity_id: str,
    target_brightness: int,
    target_rgb: tuple,
    lookup: EntityLookup,
    brightness_tolerance: int,
    rgb_color_tolerance: int,
) -> bool:
    """RGB equivalent of _already_set - per-channel tolerance (0-255
    scale, not the Kelvin-domain color_temp_tolerance). Defensive: a
    missing or malformed rgb_color attribute (e.g. a light that hasn't
    reported a colour yet, or is currently in a different colour mode)
    counts as "not close" rather than erroring, same fail-safe spirit as
    _as_int's sentinel default."""
    if not lookup.is_state(entity_id, "on"):
        return False
    if not _brightness_close(entity_id, target_brightness, lookup, brightness_tolerance):
        return False
    current_rgb = lookup.state_attr(entity_id, "rgb_color")
    return (
        isinstance(current_rgb, (list, tuple))
        and len(current_rgb) == 3
        and all(abs(_as_int(c, -999) - int(t)) <= rgb_color_tolerance for c, t in zip(current_rgb, target_rgb))
    )
