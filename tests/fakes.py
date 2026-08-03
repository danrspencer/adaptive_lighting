"""Fake lookups for tests - plain dict-backed stand-ins for real Home
Assistant state, so grouping.py/scenes.py can be exercised without a
running HA instance."""

from typing import Optional

from grouping import EntityLookup
from scenes import SceneLookup


def make_lookup(states: dict, device_of: Optional[dict] = None, labels_of: Optional[dict] = None) -> EntityLookup:
    """
    states:    {entity_id: {"state": "on"/"off"/"unavailable"/..., "attributes": {...}, "user_id": "..." or None}}
               "user_id" is optional and defaults to None (not human-caused).
    device_of: {entity_id: device_id}
    labels_of: {entity_id_or_device_id: [label, ...]}
    """
    device_of = device_of or {}
    labels_of = labels_of or {}

    def is_state(entity_id, value):
        return states.get(entity_id, {}).get("state") == value

    def state_attr(entity_id, attr):
        return states.get(entity_id, {}).get("attributes", {}).get(attr)

    def device_id(entity_id):
        return device_of.get(entity_id)

    def labels(target_id):
        return labels_of.get(target_id, [])

    def context_user_id(entity_id):
        return states.get(entity_id, {}).get("user_id")

    return EntityLookup(
        is_state=is_state,
        state_attr=state_attr,
        device_id=device_id,
        labels=labels,
        context_user_id=context_user_id,
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
