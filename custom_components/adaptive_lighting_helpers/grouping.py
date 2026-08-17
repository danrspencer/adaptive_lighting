"""
Turns "these entities, this target brightness/colour-temperature" into
the minimal set of light.turn_on/turn_off calls actually needed.

Pure logic, no Home Assistant dependency - HA access (current state,
attributes, device/label lookups) is injected via an EntityLookup so
this is testable with plain pytest and fakes, and so the integration's
__init__.py (custom_components/adaptive_lighting_helpers/__init__.py)
stays a thin adapter registering this as a standalone HA service.

This is a direct port of what used to be the blueprint's repeat-loop
`variables:` block (powerable_entities / multiplier_groups /
group_needing_off / group_needing_update / group_two_step /
group_combined) - same behaviour, same defaults, just Python instead of
namespace-loop Jinja.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

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
    last_write_context_id: Callable[[str], Optional[str]]
    last_write_owner_id: Callable[[str], Optional[str]]

    def reachable(self, entity_id: str) -> bool:
        """False for anything HA already knows it can't reach - no point commanding it."""
        return not self.is_state(entity_id, "unavailable") and not self.is_state(entity_id, "unknown")

    def tags(self, entity_id: str) -> list:
        """Labels on the entity itself plus its device (if any)."""
        did = self.device_id(entity_id)
        return self.labels(entity_id) + (self.labels(did) if did else [])

    def externally_set(self, entity_id: str, owner_id: Optional[str] = None, force: bool = False) -> bool:
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
        to recognise. Passing a value (and force left False) asks two
        independent questions, in order:

        1. Has *anything* touched this entity since the last recorded
           write (regardless of who made it)? Checked via context.id -
           this must stay the primary signal, since context.id changes
           on every write from any source, whereas an unchanged owner_id
           alone wouldn't catch a human editing the light between two
           ticks of the *same* automation.
        2. If not, was that last recorded write made under *this same*
           owner_id? A write recorded under a different owner_id (a
           different apply_lighting caller) counts as external too,
           even though nothing about the light's own context has
           changed since. A write recorded with *no* owner_id at all
           (the caller omitted it, rather than passing force) doesn't
           count against anyone - there was no claim to conflict with.

        No remembered write at all (a brand new entity, or the very
        first tick before anything's been recorded) counts as free to
        manage either way - the same "don't block on missing
        provenance" behaviour a restart used to fall back to under the
        old check. Checked fresh against live state every call, so
        there's nothing to expire: once a light is turned off, this
        naturally stops being true on its own (the is_state check above
        fails first). A device recovering from unavailable is the
        opposite case - its own reconnect state report is itself a
        fresh-context write, so it *becomes* externally-set rather than
        stops being it, and stays that way indefinitely at this layer
        alone (nothing here ever un-marks it) unless something calls
        back in with force=True. The blueprint's own `recovered` trigger
        is what actually does that, force-resyncing just that entity
        (while still passing its own owner_id, so normal ticks
        afterward recognise it) - see docs/BLUEPRINT.md's "Override
        detection" section."""
        if not self.is_state(entity_id, "on"):
            return False
        if force:
            return False
        if owner_id is None:
            return False
        last_write_context = self.last_write_context_id(entity_id)
        if last_write_context is None:
            return False
        if self.context_id(entity_id) != last_write_context:
            return True
        last_owner = self.last_write_owner_id(entity_id)
        if last_owner is None:
            return False
        return last_owner != owner_id

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
                if lookup.reachable(e) and not lookup.is_state(e, "off") and not lookup.externally_set(e, owner_id, force)
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
            and not lookup.externally_set(e, owner_id, force)
            and not _already_set(e, brightness, sensor_color_temp_kelvin, lookup, brightness_tolerance, color_temp_tolerance)
        ]
        group.two_step = [e for e in needing_update if two_step_label in lookup.tags(e)]
        group.combined = [e for e in needing_update if e not in group.two_step]

        needing_update_rgb = [
            e
            for e in rgb_entities
            if lookup.reachable(e)
            and not lookup.externally_set(e, owner_id, force)
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
    sent - an exact-match check would recommand them forever."""
    if not lookup.is_state(entity_id, "on"):
        return False
    if not _brightness_close(entity_id, target_brightness, lookup, brightness_tolerance):
        return False
    current_color_temp = _as_int(lookup.state_attr(entity_id, "color_temp_kelvin"), -999)
    return abs(current_color_temp - target_color_temp_kelvin) <= color_temp_tolerance


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
