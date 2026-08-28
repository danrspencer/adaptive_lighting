"""
Scene-coverage gap filling: given a candidate scene and the set of
entities you want a "default" behaviour applied to, works out which of
those entities the scene actually covers - so you can hand covered
ones to the scene and apply your default (adaptive lighting, or
anything else) only to whatever's left.

Ported from what used to be the blueprint's own desired_scene/
scene_covered_entities/scene_valid/scene_active/adaptive_target_entities
variables - same behaviour, same defaults, just testable Python
instead of inline Jinja. Generic: nothing here is specific to adaptive
lighting, or even to lighting - "apply a scene, then a default for
whatever it doesn't cover" is a reusable pattern on its own.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class SceneLookup:
    exists: Callable[[str], bool]
    covered_entities: Callable[[str], list]


@dataclass
class SceneCoverage:
    scene_active: bool
    scene_valid: bool
    covered_entities: list = field(default_factory=list)
    uncovered_entities: list = field(default_factory=list)


def compute_scene_coverage(
    scene_entity_id: Optional[str],
    scope_entities: list,
    target_entities: list,
    lookup: SceneLookup,
) -> SceneCoverage:
    """A scene only "counts" if it exists and every entity it covers is
    within scope_entities (e.g. the lights you're controlling, plus
    sibling entities on the same devices) - a scene reaching outside
    that scope, or one that doesn't exist (a typo, a renamed scene), is
    treated the same as no scene at all: nothing is covered, your
    default applies to every target entity, same as if scene_entity_id
    had been left blank."""
    if not scene_entity_id or not lookup.exists(scene_entity_id):
        return SceneCoverage(
            scene_active=False,
            scene_valid=False,
            covered_entities=[],
            uncovered_entities=list(target_entities),
        )

    covered = list(lookup.covered_entities(scene_entity_id))
    valid = all(e in scope_entities for e in covered)

    if not valid:
        return SceneCoverage(
            scene_active=False,
            scene_valid=False,
            covered_entities=covered,
            uncovered_entities=list(target_entities),
        )

    return SceneCoverage(
        scene_active=True,
        scene_valid=True,
        covered_entities=covered,
        uncovered_entities=[e for e in target_entities if e not in covered],
    )
