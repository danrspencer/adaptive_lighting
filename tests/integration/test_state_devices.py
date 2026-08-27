"""
State devices - the user-configured tracking scopes that own the
override-protection claims.

These cover the two things the redesign turns on: that a light resolves
to exactly one scope (deterministically, from configuration rather than
from whoever wrote last), and that the scope's entities really are the
storage rather than a view kept in step with something else.
"""

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from homeassistant.util import dt as dt_util

from custom_components.adaptive_lighting_helpers.button import async_setup_entry as button_setup
from custom_components.adaptive_lighting_helpers.const import CONF_TARGET, DOMAIN, SUBENTRY_TYPE_STATE
from custom_components.adaptive_lighting_helpers.coordinator import state_instances
from custom_components.adaptive_lighting_helpers.sensor import async_setup_entry as sensor_setup
from custom_components.adaptive_lighting_helpers.write_tracking import ClaimRegistry


def _scope(title: str, target: dict) -> ConfigSubentryData:
    return ConfigSubentryData(
        subentry_type=SUBENTRY_TYPE_STATE, title=title, unique_id=title.lower().replace(" ", "_"), data={CONF_TARGET: target}
    )


async def _setup(hass: HomeAssistant, *scopes: ConfigSubentryData):
    """Builds an entry with the given state devices and attaches their
    real entities, without going through async_forward_entry_setups -
    which would resolve the manifest's frontend dependency (see
    test_services.py's own note)."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, subentries_data=list(scopes))
    entry.add_to_hass(hass)
    registry = ClaimRegistry(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = registry

    added: list = []
    await sensor_setup(hass, entry, lambda entities, **kw: added.extend(entities))
    await button_setup(hass, entry, lambda entities, **kw: added.extend(entities))
    trackers = [e for e in added if hasattr(e, "claims")]
    for instance, tracker in zip(state_instances(entry), trackers):
        tracker.async_claims_changed = lambda: None
        registry.register(instance.subentry_id, tracker)
    return entry, registry, added


def _light(hass: HomeAssistant, entity_id: str, *, area_id=None, device_id=None, state="on", **attrs):
    registry = er.async_get(hass)
    created = registry.async_get_or_create(
        "light", "test", entity_id, suggested_object_id=entity_id.split(".", 1)[1], device_id=device_id
    )
    if area_id:
        registry.async_update_entity(created.entity_id, area_id=area_id)
    hass.states.async_set(
        entity_id, state, {"brightness": 200, "color_temp_kelvin": 3000, **attrs}, context=Context()
    )
    return created.entity_id


async def _record(registry: ClaimRegistry, entity_id: str, context_id: str, target=None):
    await registry.async_record(
        [entity_id],
        {entity_id: f"before-{entity_id}"},
        context_id,
        targets={entity_id: target} if target else None,
    )


# --- resolution -----------------------------------------------------------


async def test_a_named_entity_beats_its_device_which_beats_its_area(hass: HomeAssistant):
    """Most specific wins. The whole point of resolving from
    configuration is that the answer doesn't depend on who wrote last, so
    the order has to be a stated rule rather than an accident."""
    area = ar.async_get(hass).async_get_or_create("Kitchen")
    bulb_entry = MockConfigEntry(domain="test_bulbs")
    bulb_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=bulb_entry.entry_id, identifiers={("test", "bulb")}, name="Bulb"
    )
    entry, registry, _ = await _setup(
        hass,
        _scope("By Area", {"area_id": [area.id]}),
        _scope("By Device", {"device_id": [device.id]}),
        _scope("By Entity", {"entity_id": ["light.a"]}),
    )
    _light(hass, "light.a", area_id=area.id, device_id=device.id)

    assert registry.scope_for("light.a").title == "By Entity"

    entry2, registry2, _ = await _setup(
        hass, _scope("Area Only", {"area_id": [area.id]}), _scope("Device Only", {"device_id": [device.id]})
    )
    assert registry2.scope_for("light.a").title == "Device Only"


async def test_two_scopes_claiming_one_area_resolve_the_same_way_every_time(hass: HomeAssistant):
    """Overlapping targets are user misconfiguration, but it must not be
    a coin flip - state_instances sorts by title precisely so this is
    stable across restarts rather than following dict order."""
    area = ar.async_get(hass).async_get_or_create("Kitchen")
    _entry, registry, _ = await _setup(
        hass, _scope("Zulu", {"area_id": [area.id]}), _scope("Alpha", {"area_id": [area.id]})
    )
    _light(hass, "light.a", area_id=area.id)

    assert registry.scope_for("light.a").title == "Alpha"


async def test_a_light_matching_no_scope_is_not_tracked_and_stays_manageable(hass: HomeAssistant):
    """Deliberately no catch-all. An absent scope is a visible signal
    that a light needs an area or a target; a fallback bucket would
    silently absorb the mistake."""
    area = ar.async_get(hass).async_get_or_create("Kitchen")
    _entry, registry, _ = await _setup(hass, _scope("Kitchen", {"area_id": [area.id]}))
    _light(hass, "light.elsewhere")

    assert registry.scope_for("light.elsewhere") is None
    await _record(registry, "light.elsewhere", "ctx-1", {"brightness": 200, "color_temp_kelvin": 3000})
    assert registry.all_records() == {}
    # Untracked means classify() has nothing to block on.
    assert registry.latest_context_id("light.elsewhere") is None


async def test_a_write_before_the_scopes_entity_exists_is_dropped(hass: HomeAssistant):
    """Services are registered before the platforms are forwarded, so a
    write can genuinely arrive first. Dropping it costs one tick of
    tracking; queueing it would be machinery for a lighting override."""
    area = ar.async_get(hass).async_get_or_create("Kitchen")
    entry = MockConfigEntry(domain=DOMAIN, data={}, subentries_data=[_scope("Kitchen", {"area_id": [area.id]})])
    entry.add_to_hass(hass)
    registry = ClaimRegistry(hass, entry)  # no entities registered yet
    _light(hass, "light.a", area_id=area.id)

    await _record(registry, "light.a", "ctx-1", {"brightness": 200, "color_temp_kelvin": 3000})

    assert registry.all_records() == {}


# --- the entities are the storage ----------------------------------------


async def test_claims_live_on_the_tracking_entity_and_are_published_there(hass: HomeAssistant):
    area = ar.async_get(hass).async_get_or_create("Kitchen")
    _entry, registry, added = await _setup(hass, _scope("Kitchen", {"area_id": [area.id]}))
    tracker = next(e for e in added if hasattr(e, "claims"))
    _light(hass, "light.a", area_id=area.id)

    await _record(registry, "light.a", "ctx-1", {"brightness": 200, "color_temp_kelvin": 3000})

    # Not a copy kept in step - the registry mutated this very dict.
    assert "light.a" in tracker.claims
    assert registry.all_records()["light.a"] is tracker.claims["light.a"]
    assert tracker.native_value == 1
    assert tracker.extra_state_attributes["claims"]["light.a"]["latest"]["context_id"] == "ctx-1"
    assert "claims" in tracker._unrecorded_attributes


async def test_counters_split_one_scopes_lights_by_status(hass: HomeAssistant):
    area = ar.async_get(hass).async_get_or_create("Kitchen")
    _entry, registry, added = await _setup(hass, _scope("Kitchen", {"area_id": [area.id]}))
    controlled = next(e for e in added if e.entity_id.endswith("_adaptive_controlled"))
    overridden = next(e for e in added if e.entity_id.endswith("_adaptive_overridden"))

    ours = Context()
    _light(hass, "light.mine", area_id=area.id)
    _light(hass, "light.taken", area_id=area.id)
    await _record(registry, "light.mine", ours.id, {"brightness": 200, "color_temp_kelvin": 3000})
    await _record(registry, "light.taken", "ctx-ours", {"brightness": 200, "color_temp_kelvin": 3000})
    # Ours still matches its recorded target; the other was taken to
    # values matching neither claim.
    hass.states.async_set("light.mine", "on", {"brightness": 200, "color_temp_kelvin": 3000}, context=ours)
    hass.states.async_set("light.taken", "on", {"brightness": 12, "color_temp_kelvin": 6500}, context=Context())

    assert controlled.native_value == 1
    assert overridden.native_value == 1
    assert overridden.extra_state_attributes["lights"] == ["light.taken"]
    assert controlled.extra_state_attributes["total_tracked"] == 2


async def test_two_callers_writing_one_light_share_the_scopes_claims(hass: HomeAssistant):
    """The behaviour the scope model exists to give: two automations
    driving one room co-operate instead of each reading the other's write
    as an override."""
    area = ar.async_get(hass).async_get_or_create("Kitchen")
    _entry, registry, _ = await _setup(hass, _scope("Kitchen", {"area_id": [area.id]}))
    _light(hass, "light.a", area_id=area.id)

    await _record(registry, "light.a", "ctx-automation-one", {"brightness": 200, "color_temp_kelvin": 3000})
    await _record(registry, "light.a", "ctx-automation-two", {"brightness": 120, "color_temp_kelvin": 2700})

    # One record, in one place, carrying the most recent write.
    assert list(registry.all_records()) == ["light.a"]
    assert registry.latest_context_id("light.a") == "ctx-automation-two"


async def test_a_light_going_unavailable_is_cleared_from_its_scope(hass: HomeAssistant):
    area = ar.async_get(hass).async_get_or_create("Kitchen")
    entry, registry, _ = await _setup(hass, _scope("Kitchen", {"area_id": [area.id]}))
    _light(hass, "light.a", area_id=area.id)
    await _record(registry, "light.a", "ctx-1", {"brightness": 200, "color_temp_kelvin": 3000})
    unsub = registry.async_start_listening(hass)

    hass.states.async_set("light.a", "unavailable", {})
    await hass.async_block_till_done()

    assert registry.all_records() == {}
    unsub()


async def test_the_clear_button_clears_only_its_own_scope(hass: HomeAssistant):
    kitchen = ar.async_get(hass).async_get_or_create("Kitchen")
    hall = ar.async_get(hass).async_get_or_create("Hall")
    _entry, registry, added = await _setup(
        hass, _scope("Kitchen", {"area_id": [kitchen.id]}), _scope("Hall", {"area_id": [hall.id]})
    )
    _light(hass, "light.k", area_id=kitchen.id)
    _light(hass, "light.h", area_id=hall.id)
    await _record(registry, "light.k", "ctx-k", {"brightness": 200, "color_temp_kelvin": 3000})
    await _record(registry, "light.h", "ctx-h", {"brightness": 200, "color_temp_kelvin": 3000})

    kitchen_button = next(e for e in added if e.entity_id == "button.kitchen_adaptive_clear")
    assert kitchen_button.extra_state_attributes["tracked"] == 1
    await kitchen_button.async_press()

    assert list(registry.all_records()) == ["light.h"]


# --- the hand-over event --------------------------------------------------


async def test_the_override_event_carries_the_scopes_device_id(hass: HomeAssistant):
    """device_id is what puts the row in that device's Activity - the
    logbook's device query matches on event_data.device_id. The counters
    can never appear there themselves: HA excludes sensors carrying a
    unit or a state_class from the logbook outright."""
    area = ar.async_get(hass).async_get_or_create("Kitchen")
    entry, registry, added = await _setup(hass, _scope("Kitchen", {"area_id": [area.id]}))
    instance = state_instances(entry)[0]
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers=instance.device_info["identifiers"], name="Kitchen"
    )
    tracker = next(e for e in added if hasattr(e, "claims"))

    events: list = []
    hass.bus.async_listen("adaptive_lighting_helpers_light_overridden", events.append)

    _light(hass, "light.a", area_id=area.id)
    await _record(registry, "light.a", "ctx-ours", {"brightness": 200, "color_temp_kelvin": 3000})
    tracker._refresh_statuses()  # seeds without announcing
    assert events == []

    hass.states.async_set("light.a", "on", {"brightness": 12, "color_temp_kelvin": 6500}, context=Context())
    tracker._refresh_statuses()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["device_id"] == device.id
    assert events[0].data["scope"] == "Kitchen"
    assert events[0].data["live"]["brightness"] == 12
    assert events[0].data["latest"]["target"] == {"brightness": 200, "color_temp_kelvin": 3000}


async def test_the_event_omits_device_id_when_there_is_no_device(hass: HomeAssistant):
    """Sent as an absent key rather than an explicit null, so the
    logbook's JSON matcher can never see one."""
    area = ar.async_get(hass).async_get_or_create("Kitchen")
    _entry, registry, added = await _setup(hass, _scope("Kitchen", {"area_id": [area.id]}))
    tracker = next(e for e in added if hasattr(e, "claims"))

    events: list = []
    hass.bus.async_listen("adaptive_lighting_helpers_light_overridden", events.append)

    _light(hass, "light.a", area_id=area.id)
    await _record(registry, "light.a", "ctx-ours", {"brightness": 200, "color_temp_kelvin": 3000})
    tracker._refresh_statuses()
    hass.states.async_set("light.a", "on", {"brightness": 12, "color_temp_kelvin": 6500}, context=Context())
    tracker._refresh_statuses()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert "device_id" not in events[0].data
