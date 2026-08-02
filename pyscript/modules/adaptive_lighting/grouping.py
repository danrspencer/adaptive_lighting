"""
Turns "these entities, this target brightness/colour-temperature" into
the minimal set of light.turn_on/turn_off calls actually needed.

Pure logic, no Home Assistant dependency - HA access (current state,
attributes, device/label lookups) is injected via an EntityLookup so
this is testable with plain pytest and fakes, and so the pyscript app
wrapper (pyscript/apps/adaptive_lighting) stays a thin adapter.

This is a direct port of what used to be the blueprint's repeat-loop
`variables:` block (powerable_entities / multiplier_groups /
group_needing_off / group_needing_update / group_two_step /
group_combined) - same behaviour, same defaults, just Python instead of
namespace-loop Jinja.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class EntityLookup:
    """Home Assistant state/registry access, injected so this module
    never touches `hass` directly."""

    is_state: Callable[[str, str], bool]
    state_attr: Callable[[str, str], object]
    device_id: Callable[[str], Optional[str]]
    labels: Callable[[str], list]
    context_user_id: Callable[[str], Optional[str]]

    def reachable(self, entity_id: str) -> bool:
        """False for anything HA already knows it can't reach - no point commanding it."""
        return not self.is_state(entity_id, "unavailable") and not self.is_state(entity_id, "unknown")

    def tags(self, entity_id: str) -> list:
        """Labels on the entity itself plus its device (if any)."""
        did = self.device_id(entity_id)
        return self.labels(entity_id) + (self.labels(did) if did else [])

    def manually_set(self, entity_id: str) -> bool:
        """True if the entity's *current* state was set by a real person
        (context.user_id present) rather than this automation, another
        automation, or a device simply regaining power - all of which
        leave it null. Checked fresh against live state every call, so
        there's nothing to remember or expire: once a light's state
        changes again for any other reason (it's turned off, or a
        device recovers from unavailable), this naturally stops being
        true on its own."""
        return self.is_state(entity_id, "on") and self.context_user_id(entity_id) is not None


@dataclass
class Group:
    multiplier: float
    brightness: int
    needing_off: list = field(default_factory=list)
    combined: list = field(default_factory=list)
    two_step: list = field(default_factory=list)


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
) -> list:
    """Compute exactly what needs commanding for `entities`, bucketed by
    brightness multiplier. Each returned Group is either an off-group
    (multiplier <= 0, only `needing_off` populated) or an update-group
    (multiplier > 0, `combined`/`two_step` populated with whatever isn't
    already within tolerance of the target)."""
    groups = []
    for multiplier, group_entities in _bucket_by_multiplier(entities, brightness_multipliers).items():
        m = float(multiplier)
        brightness = 0 if m == 0 else max(round(sensor_brightness * m), 1)
        group = Group(multiplier=multiplier, brightness=brightness)

        if brightness <= 0:
            group.needing_off = [
                e
                for e in group_entities
                if lookup.reachable(e) and not lookup.is_state(e, "off") and not lookup.manually_set(e)
            ]
            groups.append(group)
            continue

        needing_update = [
            e
            for e in group_entities
            if lookup.reachable(e)
            and not lookup.manually_set(e)
            and not _already_set(e, brightness, sensor_color_temp_kelvin, lookup, brightness_tolerance, color_temp_tolerance)
        ]
        group.two_step = [e for e in needing_update if two_step_label in lookup.tags(e)]
        group.combined = [e for e in needing_update if e not in group.two_step]
        groups.append(group)

    return groups


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
    current_brightness = _as_int(lookup.state_attr(entity_id, "brightness"), -999)
    current_color_temp = _as_int(lookup.state_attr(entity_id, "color_temp_kelvin"), -999)
    brightness_close = abs(current_brightness - target_brightness) <= brightness_tolerance
    color_temp_close = abs(current_color_temp - target_color_temp_kelvin) <= color_temp_tolerance
    return brightness_close and color_temp_close
