"""Fake lookups for tests - plain dict-backed stand-ins for real Home
Assistant state, so grouping.py/scenes.py can be exercised without a
running HA instance."""

from typing import Optional

from grouping import EntityLookup
from scenes import SceneLookup


def make_lookup(
    states: dict,
    device_of: Optional[dict] = None,
    labels_of: Optional[dict] = None,
    observed_context_ids: Optional[dict] = None,
    latest_context_ids: Optional[dict] = None,
    latest_targets: Optional[dict] = None,
    observed_targets: Optional[dict] = None,
    latest_secondary_context_ids: Optional[dict] = None,
    observed_secondary_context_ids: Optional[dict] = None,
) -> EntityLookup:
    """
    states:    {entity_id: {"state": "on"/"off"/"unavailable"/..., "attributes": {...}, "context_id": "..."}}
               "context_id" is optional and defaults to None.
    device_of: {entity_id: device_id}
    labels_of: {entity_id_or_device_id: [label, ...]}
    observed_context_ids: {entity_id: value} - what
               write_tracking.LastWriteTracker would report as the
               "observed" claim for that entity - a write some earlier
               call actually observed landing. Absent means no confirmed
               write yet for that entity.
    latest_context_ids: {entity_id: value} - the
               "latest" claim - the most recent write attempted, not yet
               verified either way. Absent means no attempt is currently
               outstanding.
    latest_targets / observed_targets: {entity_id: {"brightness": ...,
               "color_temp_kelvin": ...} or {"brightness": ...,
               "rgb_color": [...]}} - what that claim's own write
               actually asked for. Absent means no target is known for
               that claim (an off-command, or a claim write_tracking
               only observed rather than issued).
    latest_secondary_context_ids / observed_secondary_context_ids:
               {entity_id: value} - the *second* context.id a two-step
               transition's own brightness-only step gets (see
               write_tracking.py's async_record). Absent means that
               claim only has the one (primary) context, same as any
               combined-write claim.
    """
    device_of = device_of or {}
    labels_of = labels_of or {}
    observed_context_ids = observed_context_ids or {}
    latest_context_ids = latest_context_ids or {}
    latest_targets = latest_targets or {}
    observed_targets = observed_targets or {}
    latest_secondary_context_ids = latest_secondary_context_ids or {}
    observed_secondary_context_ids = observed_secondary_context_ids or {}

    def is_state(entity_id, value):
        return states.get(entity_id, {}).get("state") == value

    def state_attr(entity_id, attr):
        return states.get(entity_id, {}).get("attributes", {}).get(attr)

    def device_id(entity_id):
        return device_of.get(entity_id)

    def labels(target_id):
        return labels_of.get(target_id, [])

    def context_id(entity_id):
        return states.get(entity_id, {}).get("context_id")

    def observed_context_id(entity_id):
        return observed_context_ids.get(entity_id)

    def latest_context_id(entity_id):
        return latest_context_ids.get(entity_id)

    def latest_target(entity_id):
        return latest_targets.get(entity_id)

    def observed_target(entity_id):
        return observed_targets.get(entity_id)

    def latest_secondary_context_id(entity_id):
        return latest_secondary_context_ids.get(entity_id)

    def observed_secondary_context_id(entity_id):
        return observed_secondary_context_ids.get(entity_id)

    return EntityLookup(
        is_state=is_state,
        state_attr=state_attr,
        device_id=device_id,
        labels=labels,
        context_id=context_id,
        observed_context_id=observed_context_id,
        latest_context_id=latest_context_id,
        latest_target=latest_target,
        observed_target=observed_target,
        latest_secondary_context_id=latest_secondary_context_id,
        observed_secondary_context_id=observed_secondary_context_id,
    )


def make_scene_lookup(scenes: dict) -> SceneLookup:
    """scenes: {scene_entity_id: [covered_entity_id, ...]} - a scene
    entity_id present as a key exists (even with an empty list); one
    absent from the dict doesn't exist at all."""

    def exists(scene_entity_id):
        return scene_entity_id in scenes

    def covered_entities(scene_entity_id):
        return scenes.get(scene_entity_id, [])

    return SceneLookup(exists=exists, covered_entities=covered_entities)
