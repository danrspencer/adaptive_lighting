"""Fake EntityLookup for tests - a plain dict-backed stand-in for real
Home Assistant state, so grouping.py can be exercised without pyscript
or a running HA instance."""

from typing import Optional

from adaptive_lighting import EntityLookup


def make_lookup(states: dict, device_of: Optional[dict] = None, labels_of: Optional[dict] = None) -> EntityLookup:
    """
    states:    {entity_id: {"state": "on"/"off"/"unavailable"/..., "attributes": {...}}}
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

    return EntityLookup(is_state=is_state, state_attr=state_attr, device_id=device_id, labels=labels)
