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
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed, mock_component
from homeassistant.util import dt as dt_util

from custom_components.flare.button import async_setup_entry as button_setup
from custom_components.flare.const import (
    CONF_ENTRY_TYPE,
    CONF_TARGET,
    DOMAIN,
    ENTRY_TYPE_SCHEDULES,
    ENTRY_TYPE_TRACKING,
    SUBENTRY_TYPE_STATE,
)
from custom_components.flare.coordinator import state_instances
from custom_components.flare.sensor import async_setup_entry as sensor_setup
from custom_components.flare.write_tracking import ClaimRegistry


def _scope(title: str, target: dict) -> ConfigSubentryData:
    return ConfigSubentryData(
        subentry_type=SUBENTRY_TYPE_STATE, title=title, unique_id=title.lower().replace(" ", "_"), data={CONF_TARGET: target}
    )


async def _setup(hass: HomeAssistant, *scopes: ConfigSubentryData):
    """Builds an entry with the given state devices and attaches their
    real entities, without going through async_forward_entry_setups -
    which would resolve the manifest's frontend dependency (see
    test_services.py's own note)."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_TRACKING}, subentries_data=list(scopes)
    )
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
    """Resolves the entity's scope the same way a real caller would -
    look it up, then state it explicitly - rather than relying on the
    integration to search for it. None (no matching scope at all) is
    passed straight through; async_record treats that as a no-op, same
    as any other caller passing no scope."""
    instance = registry.scope_for(entity_id)
    await registry.async_record(
        instance.subentry_id if instance else None,
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
    assert registry.latest_context_id(None, "light.elsewhere") is None


async def test_a_write_before_the_scopes_entity_exists_is_dropped(hass: HomeAssistant):
    """Services are registered before the platforms are forwarded, so a
    write can genuinely arrive first. Dropping it costs one tick of
    tracking; queueing it would be machinery for a lighting override."""
    area = ar.async_get(hass).async_get_or_create("Kitchen")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_TRACKING},
        subentries_data=[_scope("Kitchen", {"area_id": [area.id]})],
    )
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
    controlled = next(e for e in added if e.entity_id.endswith("_flare_controlled"))
    overridden = next(e for e in added if e.entity_id.endswith("_flare_overridden"))

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
    scope = registry.scope_for("light.a").subentry_id
    assert registry.latest_context_id(scope, "light.a") == "ctx-automation-two"


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


async def test_a_scope_holds_its_claims_while_any_of_its_lights_is_still_on(hass: HomeAssistant):
    """Turning one light off in a room somebody is still using is an
    override, and must stay one - releasing on the first light off
    would hand it straight back and relight it on the next tick."""
    area = ar.async_get(hass).async_get_or_create("Kitchen")
    _entry, registry, _ = await _setup(hass, _scope("Kitchen", {"area_id": [area.id]}))
    _light(hass, "light.a", area_id=area.id)
    _light(hass, "light.b", area_id=area.id)
    for e in ("light.a", "light.b"):
        await _record(registry, e, "ctx-ours", {"brightness": 200, "color_temp_kelvin": 3000})
    unsub = registry.async_start_listening(hass)

    hass.states.async_set("light.a", "off", {})
    await hass.async_block_till_done()

    assert set(registry.all_records()) == {"light.a", "light.b"}
    unsub()


async def test_a_scope_releases_once_none_of_its_lights_are_on(hass: HomeAssistant):
    """The whole room going dark is what ends an override: nobody is
    using the room, so handing it back overrides nobody's choice."""
    area = ar.async_get(hass).async_get_or_create("Kitchen")
    _entry, registry, _ = await _setup(hass, _scope("Kitchen", {"area_id": [area.id]}))
    _light(hass, "light.a", area_id=area.id)
    _light(hass, "light.b", area_id=area.id)
    for e in ("light.a", "light.b"):
        await _record(registry, e, "ctx-ours", {"brightness": 200, "color_temp_kelvin": 3000})
    unsub = registry.async_start_listening(hass)

    hass.states.async_set("light.a", "off", {})
    hass.states.async_set("light.b", "off", {})
    await hass.async_block_till_done()

    assert registry.all_records() == {}
    unsub()


async def test_an_unavailable_light_holding_a_claim_does_not_hold_a_scope_open(hass: HomeAssistant):
    """Anything not reporting `on` counts as dark. Requiring every
    tracked light to report `off` would let one permanently unavailable
    entity - an orphaned Zigbee group - veto the release forever, the
    same trap the blueprint's `recovered` trigger avoids by asking
    whether anything is reachable rather than whether nothing is
    unavailable.

    The orphan goes unavailable *before* the listener starts, so its
    claim is never popped by the drop branch and is still there to be
    iterated - which is exactly the situation after a restart, and the
    only one in which this rule is reachable at all."""
    area = ar.async_get(hass).async_get_or_create("Kitchen")
    _entry, registry, _ = await _setup(hass, _scope("Kitchen", {"area_id": [area.id]}))
    _light(hass, "light.a", area_id=area.id)
    _light(hass, "light.dead", area_id=area.id)
    for e in ("light.a", "light.dead"):
        await _record(registry, e, "ctx-ours", {"brightness": 200, "color_temp_kelvin": 3000})

    hass.states.async_set("light.dead", "unavailable", {})
    await hass.async_block_till_done()
    assert set(registry.all_records()) == {"light.a", "light.dead"}, "precondition: the claim survives"

    unsub = registry.async_start_listening(hass)
    hass.states.async_set("light.a", "off", {})
    await hass.async_block_till_done()

    assert registry.all_records() == {}
    unsub()


async def test_scopes_release_independently(hass: HomeAssistant):
    """One room going dark says nothing about another."""
    kitchen = ar.async_get(hass).async_get_or_create("Kitchen")
    hall = ar.async_get(hass).async_get_or_create("Hall")
    _entry, registry, _ = await _setup(
        hass, _scope("Kitchen", {"area_id": [kitchen.id]}), _scope("Hall", {"area_id": [hall.id]})
    )
    _light(hass, "light.k", area_id=kitchen.id)
    _light(hass, "light.h", area_id=hall.id)
    for e in ("light.k", "light.h"):
        await _record(registry, e, "ctx-ours", {"brightness": 200, "color_temp_kelvin": 3000})
    unsub = registry.async_start_listening(hass)

    hass.states.async_set("light.k", "off", {})
    await hass.async_block_till_done()

    assert set(registry.all_records()) == {"light.h"}
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

    kitchen_button = next(e for e in added if e.entity_id == "button.kitchen_flare_clear")
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
    hass.bus.async_listen("flare_light_overridden", events.append)

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
    hass.bus.async_listen("flare_light_overridden", events.append)

    _light(hass, "light.a", area_id=area.id)
    await _record(registry, "light.a", "ctx-ours", {"brightness": 200, "color_temp_kelvin": 3000})
    tracker._refresh_statuses()
    hass.states.async_set("light.a", "on", {"brightness": 12, "color_temp_kelvin": 6500}, context=Context())
    tracker._refresh_statuses()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert "device_id" not in events[0].data


# --- the setup offer ------------------------------------------------------


@pytest.fixture
def stub_entry_setup(hass: HomeAssistant):
    """Completing the flow sets the entry up, which resolves the
    manifest's frontend dependency and pulls in the large
    home-assistant-frontend package for the dashboard card these tests
    never look at. Stubbed the same way test_services.py sidesteps it."""
    mock_component(hass, "frontend")
    mock_component(hass, "repairs")
    hass.data.setdefault("frontend_extra_module_url", set())
    return hass


def _entry_of_type(hass: HomeAssistant, entry_type: str):
    return next(e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get(CONF_ENTRY_TYPE) == entry_type)


async def test_setup_offers_one_state_device_per_area_that_has_lights(stub_entry_setup, hass: HomeAssistant):
    """A room is the unit almost everyone wants to track by, so the list
    arrives pre-selected rather than as a wall of work. Areas with no
    lights are left out - a scope that can never resolve anything is
    just an empty device to wonder about."""
    kitchen = ar.async_get(hass).async_get_or_create("Kitchen")
    hall = ar.async_get(hass).async_get_or_create("Hall")
    garage = ar.async_get(hass).async_get_or_create("Garage")
    _light(hass, "light.k", area_id=kitchen.id)
    _light(hass, "light.h", area_id=hall.id)
    # The Garage has entities, just no lights - so it must not be
    # offered. Without a non-light here, "lights only" would look
    # covered while actually being untested.
    door = er.async_get(hass).async_get_or_create("switch", "test", "garage_door")
    er.async_get(hass).async_update_entity(door.entity_id, area_id=garage.id)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "form"
    suggested = result["data_schema"]({})["areas"]
    assert sorted(suggested) == sorted([hall.id, kitchen.id])

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"areas": [kitchen.id]})

    assert result["type"] == "create_entry"
    scopes = state_instances(_entry_of_type(hass, ENTRY_TYPE_TRACKING))
    assert [s.title for s in scopes] == ["Kitchen"]
    assert scopes[0].target == {"area_id": [kitchen.id]}


async def test_adding_the_integration_once_creates_both_entries(stub_entry_setup, hass: HomeAssistant):
    """Two entries is a grouping decision, not a reason to walk through
    Add Integration twice. The one the flow finishes on must be
    Schedules: HA's "integration added" dialog shows an unsuppressable
    rename + area form for every device on the completing flow's entry,
    and Tracking is the half that seeds a device per room."""
    kitchen = ar.async_get(hass).async_get_or_create("Kitchen")
    _light(hass, "light.k", area_id=kitchen.id)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"areas": [kitchen.id]})
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert result["title"] == "FLARE Schedules"
    assert {e.data[CONF_ENTRY_TYPE] for e in hass.config_entries.async_entries(DOMAIN)} == {
        ENTRY_TYPE_SCHEDULES,
        ENTRY_TYPE_TRACKING,
    }
    assert [s.title for s in state_instances(_entry_of_type(hass, ENTRY_TYPE_TRACKING))] == ["Kitchen"]


async def test_the_missing_half_can_be_added_back_on_its_own(stub_entry_setup, hass: HomeAssistant):
    """Deleting one entry has to be recoverable. With Schedules already
    present the flow creates only Tracking - and this time Tracking is
    what the flow itself returns, since there is no second entry to
    hand the visible completion to."""
    kitchen = ar.async_get(hass).async_get_or_create("Kitchen")
    _light(hass, "light.k", area_id=kitchen.id)
    MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_SCHEDULES},
        unique_id=f"{DOMAIN}_{ENTRY_TYPE_SCHEDULES}",
        version=3,
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"areas": [kitchen.id]})
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert result["title"] == "FLARE Tracking"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


async def test_adding_it_again_with_both_present_aborts(stub_entry_setup, hass: HomeAssistant):
    """Nothing left to create, and neither half may be duplicated."""
    for entry_type in (ENTRY_TYPE_SCHEDULES, ENTRY_TYPE_TRACKING):
        MockConfigEntry(
            domain=DOMAIN,
            data={CONF_ENTRY_TYPE: entry_type},
            unique_id=f"{DOMAIN}_{entry_type}",
            version=3,
        ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


async def test_setup_with_no_areas_creates_the_entry_and_no_scopes(stub_entry_setup, hass: HomeAssistant):
    """Nothing here is required. With no areas there is nothing to
    offer, so both entries are created straight away and lights simply
    stay untracked until a state device exists."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert state_instances(_entry_of_type(hass, ENTRY_TYPE_TRACKING)) == []


# --- upgrading an existing entry -----------------------------------------


async def test_upgrading_splits_the_entry_and_keeps_the_schedule_config(stub_entry_setup, hass: HomeAssistant):
    """The existing entry becomes the schedules one, keeping its sensor
    subentries and with them every schedule time and curve value the
    user has set - real configuration worth preserving. Its state
    subentries go; the tracking entry re-seeds equivalents, which cost
    nothing since a scope carries only a target and claims aren't
    persisted."""
    from custom_components.flare import async_migrate_entry
    from custom_components.flare.const import (
        CONF_ENTRY_TYPE as CET,
        ENTRY_TYPE_SCHEDULES,
        SUBENTRY_TYPE_SENSOR,
    )

    kitchen = ar.async_get(hass).async_get_or_create("Kitchen")
    _light(hass, "light.k", area_id=kitchen.id)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        version=2,
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_SENSOR, title="Ground Floor", unique_id="ground_floor", data={}
            ),
            _scope("Old Scope", {"area_id": [kitchen.id]}),
        ],
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)
    await hass.async_block_till_done()

    assert entry.version == 3
    assert entry.data[CET] == ENTRY_TYPE_SCHEDULES
    # The schedule subentry survives; the state one doesn't.
    assert [s.subentry_type for s in entry.subentries.values()] == [SUBENTRY_TYPE_SENSOR]

    tracking = [e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get(CET) == ENTRY_TYPE_TRACKING]
    assert len(tracking) == 1
    assert [s.title for s in state_instances(tracking[0])] == ["Kitchen"]


async def test_upgrading_an_already_migrated_entry_adds_nothing(stub_entry_setup, hass: HomeAssistant):
    """The version bump is the guard, so neither the split nor the
    seeding is redone on the next restart."""
    from custom_components.flare import async_migrate_entry

    kitchen = ar.async_get(hass).async_get_or_create("Kitchen")
    _light(hass, "light.k", area_id=kitchen.id)
    entry = MockConfigEntry(domain=DOMAIN, data={}, version=3)
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert state_instances(entry) == []
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_a_state_device_lands_in_the_area_it_targets(hass: HomeAssistant):
    """A scope created per area should turn up under that room rather
    than in an unsorted heap."""
    from custom_components.flare.sensor import _assign_scope_area

    kitchen = ar.async_get(hass).async_get_or_create("Kitchen")
    entry, _registry_, added = await _setup(hass, _scope("Kitchen", {"area_id": [kitchen.id]}))
    instance = state_instances(entry)[0]
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers=instance.device_info["identifiers"], name="Kitchen"
    )
    tracker = next(e for e in added if hasattr(e, "claims"))
    tracker.registry_entry = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, tracker.unique_id, config_entry=entry, device_id=device.id
    )

    _assign_scope_area(hass, tracker, instance)

    assert dr.async_get(hass).async_get(device.id).area_id == kitchen.id


async def test_a_scope_spanning_several_areas_is_left_unassigned(hass: HomeAssistant):
    """No single right answer, so no guess."""
    from custom_components.flare.sensor import _assign_scope_area

    kitchen = ar.async_get(hass).async_get_or_create("Kitchen")
    hall = ar.async_get(hass).async_get_or_create("Hall")
    entry, _registry_, added = await _setup(hass, _scope("Both", {"area_id": [kitchen.id, hall.id]}))
    instance = state_instances(entry)[0]
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers=instance.device_info["identifiers"], name="Both"
    )
    tracker = next(e for e in added if hasattr(e, "claims"))
    tracker.registry_entry = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, tracker.unique_id, config_entry=entry, device_id=device.id
    )

    _assign_scope_area(hass, tracker, instance)

    assert dr.async_get(hass).async_get(device.id).area_id is None


async def test_counters_refresh_when_a_lights_live_state_changes(hass: HomeAssistant):
    """The counters are views over the claims, but what they show
    depends on each light's *live* state, which changes with nothing
    here being touched. Without the tracking sensor's poll broadcasting,
    they only refresh when a claim mutates and sit stale in between -
    caught live still calling a light overridden minutes after its own
    state had moved on.

    Uses unavailable rather than off: an off light with someone else's
    claim on it is *still* overridden now (see
    test_being_switched_off_by_hand_is_an_override), so off no longer
    changes this count on its own."""
    area = ar.async_get(hass).async_get_or_create("Kitchen")
    _entry, registry, added = await _setup(hass, _scope("Kitchen", {"area_id": [area.id]}))
    tracker = next(e for e in added if hasattr(e, "claims"))
    overridden = next(e for e in added if e.entity_id.endswith("_flare_overridden"))

    _light(hass, "light.a", area_id=area.id)
    await _record(registry, "light.a", "ctx-ours", {"brightness": 200, "color_temp_kelvin": 3000})
    hass.states.async_set("light.a", "on", {"brightness": 12, "color_temp_kelvin": 6500}, context=Context())
    assert overridden.native_value == 1

    refreshed: list = []
    from homeassistant.helpers.dispatcher import async_dispatcher_connect

    from custom_components.flare.write_tracking import SIGNAL_WRITE_TRACKING_UPDATED

    async_dispatcher_connect(hass, SIGNAL_WRITE_TRACKING_UPDATED, lambda: refreshed.append(True))

    # The light drops off the network with no claim changing at all.
    hass.states.async_set("light.a", "unavailable", {})
    await tracker.async_update()
    await hass.async_block_till_done()

    assert refreshed, "the poll must tell the counters to recompute"
    assert overridden.native_value == 0


async def test_each_entry_type_owns_only_its_own_sensors(hass: HomeAssistant):
    """Both entry types use the sensor platform, so the branch deciding
    which entities belong to which is load-bearing: without it a
    schedules entry would try to build tracking entities (and look up a
    claim registry it doesn't have), and vice versa."""
    from custom_components.flare.const import (
        CONF_ENTRY_TYPE as CET,
        ENTRY_TYPE_SCHEDULES,
        SUBENTRY_TYPE_SENSOR,
    )
    from custom_components.flare.coordinator import ScheduleCoordinator, schedule_instances

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CET: ENTRY_TYPE_SCHEDULES},
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_SENSOR, title="Ground Floor", unique_id="ground_floor", data={}
            )
        ],
    )
    entry.add_to_hass(hass)
    for instance in schedule_instances(entry):
        coordinator = ScheduleCoordinator(hass, instance)
        # Plain refresh: async_config_entry_first_refresh needs the
        # coordinator to carry a config entry, which only happens inside
        # a real entry setup.
        await coordinator.async_refresh()
        hass.data.setdefault(DOMAIN, {})[instance.subentry_id] = coordinator

    added: list = []
    await sensor_setup(hass, entry, lambda entities, **kw: added.extend(entities))

    assert [e.entity_id for e in added] == ["sensor.ground_floor_flare"]
    # No claims storage on a schedules entry - it has no registry at all.
    assert not any(hasattr(e, "claims") for e in added)
