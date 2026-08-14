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

    def reachable(self, entity_id: str) -> bool:
        """False for anything HA already knows it can't reach - no point commanding it."""
        return not self.is_state(entity_id, "unavailable") and not self.is_state(entity_id, "unknown")

    def tags(self, entity_id: str) -> list:
        """Labels on the entity itself plus its device (if any)."""
        did = self.device_id(entity_id)
        return self.labels(entity_id) + (self.labels(did) if did else [])

    def externally_set(self, entity_id: str) -> bool:
        """True if the entity is on and its *current* context.id doesn't
        match the context.id this integration itself last wrote it with
        (see write_tracking.LastWriteTracker) - i.e. something other
        than our own last apply_lighting call has touched it since:
        a person, another automation (even one with no context.user_id
        of its own, such as one triggered directly by a physical
        button - the gap context.user_id-based detection used to miss),
        or a device regaining power under a fresh context. No
        remembered write at all (a brand new entity, or the very first
        tick before anything's been recorded) counts as free to manage,
        not externally set - the same "don't block on missing
        provenance" behaviour a restart used to fall back to under the
        old check. Checked fresh against live state every call, so
        there's nothing to expire: once a light's state changes again
        for any other reason (it's turned off, or a device recovers
        from unavailable), this naturally stops being true on its own."""
        if not self.is_state(entity_id, "on"):
            return False
        last_write = self.last_write_context_id(entity_id)
        if last_write is None:
            return False
        return self.context_id(entity_id) != last_write

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
    behaviour is otherwise identical to before this parameter existed."""
    use_rgb = prefer_rgb_color and rgb_color is not None
    groups = []
    for multiplier, group_entities in _bucket_by_multiplier(entities, brightness_multipliers).items():
        m = float(multiplier)
        brightness = 0 if m == 0 else max(round(sensor_brightness * m), 1)
        group = Group(multiplier=multiplier, brightness=brightness)

        if brightness <= 0:
            group.needing_off = [
                e
                for e in group_entities
                if lookup.reachable(e) and not lookup.is_state(e, "off") and not lookup.externally_set(e)
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
            and not lookup.externally_set(e)
            and not _already_set(e, brightness, sensor_color_temp_kelvin, lookup, brightness_tolerance, color_temp_tolerance)
        ]
        group.two_step = [e for e in needing_update if two_step_label in lookup.tags(e)]
        group.combined = [e for e in needing_update if e not in group.two_step]

        needing_update_rgb = [
            e
            for e in rgb_entities
            if lookup.reachable(e)
            and not lookup.externally_set(e)
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
