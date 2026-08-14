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
    last_write_context_ids: Optional[dict] = None,
    last_write_owner_ids: Optional[dict] = None,
) -> EntityLookup:
    """
    states:    {entity_id: {"state": "on"/"off"/"unavailable"/..., "attributes": {...}, "context_id": "..."}}
               "context_id" is optional and defaults to None.
    device_of: {entity_id: device_id}
    labels_of: {entity_id_or_device_id: [label, ...]}
    last_write_context_ids: {entity_id: context_id} - what write_tracking.LastWriteTracker
               would report as the context.id this integration itself last wrote that
               entity with. Absent/empty means "no record" for every entity.
    last_write_owner_ids: {entity_id: owner_id} - the owner_id that write was made
               under, if any. Absent/empty means no owner_id was recorded (None).
    """
    device_of = device_of or {}
    labels_of = labels_of or {}
    last_write_context_ids = last_write_context_ids or {}
    last_write_owner_ids = last_write_owner_ids or {}

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

    def last_write_context_id(entity_id):
        return last_write_context_ids.get(entity_id)

    def last_write_owner_id(entity_id):
        return last_write_owner_ids.get(entity_id)

    return EntityLookup(
        is_state=is_state,
        state_attr=state_attr,
        device_id=device_id,
        labels=labels,
        context_id=context_id,
        last_write_context_id=last_write_context_id,
        last_write_owner_id=last_write_owner_id,
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
