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
    confirmed_context_ids: Optional[dict] = None,
    confirmed_owner_ids: Optional[dict] = None,
    pending_context_ids: Optional[dict] = None,
    pending_owner_ids: Optional[dict] = None,
    pending_targets: Optional[dict] = None,
) -> EntityLookup:
    """
    states:    {entity_id: {"state": "on"/"off"/"unavailable"/..., "attributes": {...}, "context_id": "..."}}
               "context_id" is optional and defaults to None.
    device_of: {entity_id: device_id}
    labels_of: {entity_id_or_device_id: [label, ...]}
    confirmed_context_ids / confirmed_owner_ids: {entity_id: value} - what
               write_tracking.LastWriteTracker would report as the
               "confirmed" claim for that entity - a write some earlier
               call actually observed landing. Absent means no confirmed
               write yet for that entity.
    pending_context_ids / pending_owner_ids: {entity_id: value} - the
               "pending" claim - the most recent write attempted, not yet
               verified either way. Absent means no attempt is currently
               outstanding.
    pending_targets: {entity_id: {"brightness": ..., "color_temp_kelvin": ...}
               or {"brightness": ..., "rgb_color": [...]}} - what the
               pending claim's write actually asked for. Absent means no
               target is known for that claim (an off-command, or a
               claim write_tracking only observed rather than issued).
    """
    device_of = device_of or {}
    labels_of = labels_of or {}
    confirmed_context_ids = confirmed_context_ids or {}
    confirmed_owner_ids = confirmed_owner_ids or {}
    pending_context_ids = pending_context_ids or {}
    pending_owner_ids = pending_owner_ids or {}
    pending_targets = pending_targets or {}

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

    def confirmed_context_id(entity_id):
        return confirmed_context_ids.get(entity_id)

    def confirmed_owner_id(entity_id):
        return confirmed_owner_ids.get(entity_id)

    def pending_context_id(entity_id):
        return pending_context_ids.get(entity_id)

    def pending_owner_id(entity_id):
        return pending_owner_ids.get(entity_id)

    def pending_target(entity_id):
        return pending_targets.get(entity_id)

    return EntityLookup(
        is_state=is_state,
        state_attr=state_attr,
        device_id=device_id,
        labels=labels,
        context_id=context_id,
        confirmed_context_id=confirmed_context_id,
        confirmed_owner_id=confirmed_owner_id,
        pending_context_id=pending_context_id,
        pending_owner_id=pending_owner_id,
        pending_target=pending_target,
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
