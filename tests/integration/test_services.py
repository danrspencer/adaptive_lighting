"""
Integration tests for the flare services
(__init__.py), through a real Home Assistant instance - this is the
HA-glue layer that tests/test_grouping.py and tests/test_curve.py
can't reach at all (they exercise grouping.py/curve.py directly via
fakes, never __init__.py's service registration, sensor reading,
context propagation, or write_tracking.py's real Store persistence).

Deliberately doesn't re-prove grouping.py's own tolerance/two-step/RGB
routing logic - that's tests/test_grouping.py's job, already thorough.
These tests are about the wiring: does apply_lighting actually call
light.turn_on/turn_off with the right data, does a bad sensor raise the
right error, does override protection actually survive a real
Store round-trip.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import voluptuous as vol
from freezegun import freeze_time
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.flare import _build_lookup, async_setup_entry
from custom_components.flare.grouping import build_groups
from custom_components.flare.const import (
    CONF_ENTRY_TYPE,
    CONF_TARGET,
    ENTRY_TYPE_TRACKING,
    SUBENTRY_TYPE_STATE,
)
from custom_components.flare.coordinator import state_instances
from custom_components.flare.write_tracking import STALE_RECORD_MAX_AGE_DAYS, ClaimRegistry

DOMAIN = "flare"


async def _setup_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Calls async_setup_entry directly rather than going through
    hass.config_entries.async_setup(), which would also resolve the
    manifest's http/frontend dependencies (needed in production for the
    dashboard card's add_extra_js_url - see __init__.py's async_setup)
    and pull in the separate, large home-assistant-frontend package for
    something these tests never touch. The services themselves don't
    depend on async_setup() having run at all.

    mock_state(LOADED) is needed because async_setup_entry now always
    calls async_forward_entry_setups (for the write-tracking diagnostic
    sensor - see sensor.py's _WriteTrackingSensor - which exists
    regardless of how many schedule instances are configured), and that
    call requires the entry to already be LOADED - normally something
    hass.config_entries.async_setup() itself does around calling into
    the component, which this helper deliberately bypasses."""
    await _track_test_lights(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_TRACKING},
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_STATE,
                title="Test Scope",
                unique_id="test_scope",
                data={CONF_TARGET: {"area_id": [_test_area(hass).id]}},
            )
        ],
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()
    await _attach_tracking_sensors(hass, entry)
    return entry


async def _attach_tracking_sensors(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Claims live on each state device's tracking entity, so the
    registry has nothing to write to until those entities exist.

    async_forward_entry_setups can't create them here - it resolves the
    manifest's frontend dependency, which this suite deliberately avoids
    (see _setup_entry). So the real sensor platform is invoked directly
    with a capturing async_add_entities, exercising the real
    _StateTrackingSensor and the real registry routing; only HA's state
    publication is stubbed out, which test_state_devices.py covers
    against a properly added entity."""
    from homeassistant.helpers import device_registry as dr

    from custom_components.flare.sensor import async_setup_entry as sensor_setup

    added: list = []
    await sensor_setup(hass, entry, lambda entities, **kw: added.extend(entities))
    registry = _registry(hass)
    for instance, entity in zip(state_instances(entry), [e for e in added if hasattr(e, "claims")]):
        entity.async_claims_changed = lambda: None
        registry.register(instance.subentry_id, entity)
        # The stub async_add_entities above is a plain list-append, not
        # the real entity platform - it never triggers the device
        # registration a real add_entities call does for an entity
        # carrying device_info. resolve_scope_device (and any test using
        # scope_device_id) needs a real device to resolve, so create it
        # explicitly here, the same identifiers StateInstance.device_info
        # would produce.
        dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, identifiers=instance.device_info["identifiers"], name=instance.title
        )
    # A device registry write also triggers two_step_check.py's own
    # 5s-debounced watcher (it listens for entity AND device registry
    # updates) - flush it the same way _track_test_lights already does
    # for its own entity-registry writes, or the harness fails on a
    # lingering timer.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()


def _registry(hass: HomeAssistant) -> ClaimRegistry:
    """The one live registry, from hass.data. Tests used to build a
    second tracker against the same Store to read state back; there is
    no Store any more, and no second copy to read."""
    return next(v for v in hass.data[DOMAIN].values() if isinstance(v, ClaimRegistry))


def _tracking_entry(hass: HomeAssistant):
    return next(
        e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_TRACKING
    )


def _test_scope_subentry_id(hass: HomeAssistant) -> str:
    """The Test Scope state device's own subentry_id - what
    ClaimRegistry's accessors want directly."""
    return next(iter(_tracking_entry(hass).subentries))


def _scope_device_id(hass: HomeAssistant) -> str:
    """The Test Scope state device's own device_id - what a real caller
    resolves once (e.g. from room_target's area) and passes to a service
    call as scope_device_id, exactly as the blueprint does."""
    from homeassistant.helpers import device_registry as dr

    entry = _tracking_entry(hass)
    identifier = (DOMAIN, next(iter(entry.subentries)))
    # async_get_device(identifiers=...) is deprecated in favour of the
    # by-identifier lookup, which also needs the owning config_entry_id -
    # identifiers are only guaranteed unique within one entry now.
    device = dr.async_get(hass).async_get_device_by_identifier(identifier, entry.entry_id)
    assert device is not None
    return device.id


def _test_area(hass: HomeAssistant):
    """One area every test light belongs to, so the state device created
    in _setup_entry actually resolves them - claims live on a state
    device now, and a light matching none simply isn't tracked."""
    from homeassistant.helpers import area_registry as ar

    return ar.async_get(hass).async_get_or_create("Adaptive Test Area")


# Every light these tests use. Registered into the test area once, in
# _setup_entry, rather than lazily per _set_light: a registry write
# trips two_step_check.py's 5s-debounced watcher, which has to be
# flushed or the harness fails on a lingering timer (see
# _label_two_step), and doing that once in an async place beats making
# every _set_light call await something.
_TEST_LIGHTS = ("light.a", "light.never_tracked", "light.recovering", "light.sibling")


async def _track_test_lights(hass: HomeAssistant) -> None:
    registry = er.async_get(hass)
    area_id = _test_area(hass).id
    for entity_id in _TEST_LIGHTS:
        created = registry.async_get_or_create(
            "light", "test", entity_id, suggested_object_id=entity_id.split(".", 1)[1]
        )
        registry.async_update_entity(created.entity_id, area_id=area_id)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()


def _set_light(hass: HomeAssistant, entity_id: str, state: str, *, context: Context | None = None, **attrs) -> None:
    hass.states.async_set(entity_id, state, attrs, context=context)


_UNSET = object()  # distinguishes "use the default scope" from an explicit scope_device_id=None


async def _apply(hass: HomeAssistant, entities: list[str], *, context: Context | None = None, **overrides) -> None:
    """apply_lighting with the fields most tests don't care about
    defaulted, including scope_device_id (Test Scope's own device) -
    most tests here are about override protection, which needs a real
    scope to mean anything. Pass scope_device_id=None explicitly to
    exercise the untracked path instead."""
    data = {
        "entities": entities,
        "brightness": 200,
        "color_temp_kelvin": 3000,
        "transition": 0,
        "scope_device_id": _scope_device_id(hass),
        **overrides,
    }
    await hass.services.async_call(DOMAIN, "apply_lighting", data, blocking=True, context=context)

async def _label_two_step(hass: HomeAssistant, entity_id: str) -> None:
    """Registers entity_id with the real "no_combined_transition" label
    directly on the entity (no device needed - EntityLookup.tags() reads
    entity labels plus device labels, and a fake/unregistered device
    just contributes nothing). HA's entity registry `labels` field is a
    plain set of label id strings with no foreign-key enforcement, so
    this doesn't need a real label-registry entry to exist first - see
    tests/integration/test_two_step_repair.py for the fuller
    device+label-registry setup a different feature (detecting bulbs
    *missing* this label) actually needs.

    Async because a real entity-registry change here also triggers
    two_step_check.py's own, unrelated registry-change watcher
    (async_start_watching), which schedules a 5s-debounced check -
    flushed here immediately so it doesn't linger past the end of
    whichever test called this and fail the harness's own lingering-
    timer assertion, the same class of gotcha lesson 10/the two-step
    repair feature's own tests already document."""
    domain, object_id = entity_id.split(".", 1)
    er.async_get(hass).async_get_or_create(domain, "test", object_id, suggested_object_id=object_id)
    er.async_get(hass).async_update_entity(entity_id, labels={"no_combined_transition"})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()


@pytest.fixture
async def setup_integration(hass: HomeAssistant):
    await _setup_entry(hass)
    yield hass


async def test_compute_curve_service_returns_expected_shape(setup_integration: HomeAssistant):
    hass = setup_integration
    result = await hass.services.async_call(
        DOMAIN,
        "compute_curve",
        {"morning": 0, "day": 100, "evening": 200, "night": 300, "at": 150},
        blocking=True,
        return_response=True,
    )
    assert result["phase"] == "Day"
    assert "brightness" in result
    assert "kelvin" in result
    assert isinstance(result["rgb_color"], list) and len(result["rgb_color"]) == 3


async def test_compute_lighting_groups_is_read_only(setup_integration: HomeAssistant):
    """The pure planner - no light.turn_on/turn_off should ever be issued."""
    hass = setup_integration
    calls = async_mock_service(hass, "light", "turn_on")
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    result = await hass.services.async_call(
        DOMAIN,
        "compute_lighting_groups",
        {
            "entities": ["light.a"],
            "brightness": 200,
            "color_temp_kelvin": 4000,
        },
        blocking=True,
        return_response=True,
    )

    assert result["groups"][0]["combined"] == ["light.a"]
    assert calls == []


async def test_apply_lighting_turns_on_reachable_entities(setup_integration: HomeAssistant):
    hass = setup_integration
    turn_on_calls = async_mock_service(hass, "light", "turn_on")
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    await _apply(hass, ['light.a'], brightness=180, color_temp_kelvin=3200, transition=2)

    assert len(turn_on_calls) == 1
    assert turn_on_calls[0].data["entity_id"] == ["light.a"]
    assert turn_on_calls[0].data["brightness"] == 180
    assert turn_on_calls[0].data["color_temp_kelvin"] == 3200


async def test_apply_lighting_missing_brightness_raises(setup_integration: HomeAssistant):
    """brightness/color_temp_kelvin are vol.Required - a caller (e.g. the
    blueprint's own state_attr() read, if the sensor it's pointed at
    doesn't actually have the attribute) omitting one must raise, not
    silently dim everything to brightness 1 - see the schema's own
    history: an early version read this off a sensor internally and had
    exactly this silent-default bug, fixed by making it a hard failure.
    Voluptuous's own required-field validation gives that same hard
    failure now, one layer earlier (before the handler even runs)."""
    hass = setup_integration
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    with pytest.raises(vol.Invalid):
        # Deliberately raw, not _apply() - the helper defaults brightness,
        # which is the exact field this test needs missing.
        await hass.services.async_call(
            DOMAIN,
            "apply_lighting",
            {"entities": ["light.a"], "color_temp_kelvin": 3200, "transition": 2},
            blocking=True,
        )


async def test_apply_lighting_non_numeric_color_temp_kelvin_raises(setup_integration: HomeAssistant):
    hass = setup_integration
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    with pytest.raises(vol.Invalid):
        await _apply(hass, ['light.a'], brightness=180, color_temp_kelvin='not-a-number', transition=2)


async def test_apply_lighting_accepts_an_explicit_null_rgb_color(setup_integration: HomeAssistant):
    """A hand-rolled 'bring your own sensor' entity is free to omit
    rgb_color entirely (see docs/blueprint.md), and the blueprint's own
    state_attr(adaptive_sensor, 'rgb_color') then renders a literal
    None, not an omitted key - vol.Length applied to None used to fail
    schema validation outright (vol.Length expects a sized value), which
    would have broken apply_lighting for every such room the moment the
    blueprint started passing this field unconditionally. The fix is
    vol.Any(None, ...) on the schema; this must NOT raise."""
    hass = setup_integration
    turn_on_calls = async_mock_service(hass, "light", "turn_on")
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    await _apply(hass, ['light.a'], brightness=180, color_temp_kelvin=3200, rgb_color=None, transition=2)

    assert len(turn_on_calls) == 1


async def test_compute_lighting_groups_accepts_an_explicit_null_rgb_color(setup_integration: HomeAssistant):
    """Same schema gap, same fix, on compute_lighting_groups - never
    exercised live (nothing calls it with an explicit None today), but
    the identical vol.Length-on-None failure was present before the fix
    and must not resurface."""
    hass = setup_integration
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    result = await hass.services.async_call(
        DOMAIN,
        "compute_lighting_groups",
        {
            "entities": ["light.a"],
            "brightness": 200,
            "color_temp_kelvin": 4000,
            "rgb_color": None,
        },
        blocking=True,
        return_response=True,
    )

    assert result["groups"][0]["combined"] == ["light.a"]


async def _check(hass: HomeAssistant, entities: list[str], *, scope_device_id=_UNSET) -> dict:
    if scope_device_id is _UNSET:
        scope_device_id = _scope_device_id(hass)
    result = await hass.services.async_call(
        DOMAIN,
        "check_control",
        {"entities": entities, "scope_device_id": scope_device_id},
        blocking=True,
        return_response=True,
    )
    return result["results"]


async def _record_write(hass: HomeAssistant, entities: list[str], *, targets=None, context=None, scope_device_id=_UNSET):
    if scope_device_id is _UNSET:
        scope_device_id = _scope_device_id(hass)
    data = {"entities": entities, "scope_device_id": scope_device_id}
    if targets is not None:
        data["targets"] = targets
    return await hass.services.async_call(
        DOMAIN, "record_write", data, blocking=True, context=context, return_response=True
    )


async def test_check_control_reports_untracked_for_a_brand_new_entity(setup_integration: HomeAssistant):
    """check_control is genuinely standalone - no apply_lighting call,
    no sensor, just a light and the write-tracking mechanism."""
    hass = setup_integration
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=100, color_temp_kelvin=3000)

    results = await _check(hass, ["light.a"])

    assert results["light.a"] == {"blocked": False, "status": "untracked", "matched_via": None, "scope": "Test Scope"}


async def test_check_control_and_record_write_round_trip(setup_integration: HomeAssistant):
    """The two services used together, standalone - no apply_lighting
    involved at all, matching how an independent automation would use
    this mechanism on its own light.turn_on calls."""
    hass = setup_integration
    our_context = Context()
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=100, color_temp_kelvin=3000, context=our_context)

    await _record_write(
        hass, ["light.a"], targets={"light.a": {"brightness": 100, "color_temp_kelvin": 3000}}, context=our_context
    )

    # Immediately after, still under the same context - not blocked, and
    # attributed to us. matched_via is latest-context because this is the
    # entity's first-ever tracked write: the synthetic baseline records
    # the pre-write context as `observed` too, so both claims share a
    # context.id here - `latest` is checked first, matching classify()'s
    # own precedence.
    results = await _check(hass, ["light.a"])
    assert results["light.a"] == {
        "blocked": False,
        "status": "controlled",
        "matched_via": "latest-context",
        "scope": "Test Scope",
    }

    # Someone else changes it - a different context, different values.
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=40, color_temp_kelvin=6000)

    results = await _check(hass, ["light.a"])
    assert results["light.a"] == {"blocked": True, "status": "overridden", "matched_via": None, "scope": "Test Scope"}

    # Another caller asking about the same still-matching claim, through
    # the same scope, sees it as *not* blocked. There is no owner
    # comparison any more: the claim belongs to whatever scope the caller
    # named, so any caller naming the same scope co-operates rather than
    # blocking the other.
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=100, color_temp_kelvin=3000, context=our_context)
    results = await _check(hass, ["light.a"])
    assert results["light.a"] == {
        "blocked": False,
        "status": "controlled",
        "matched_via": "latest-context",
        "scope": "Test Scope",
    }


async def test_check_control_echoes_back_whatever_scope_it_was_given(setup_integration: HomeAssistant):
    """`scope` is no longer resolved per entity - it simply echoes the
    caller's own scope_device_id back as a title, for every entity in the
    call, regardless of whether that light is anywhere near the scope's
    own target."""
    hass = setup_integration
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=100, color_temp_kelvin=3000)
    # Not in the Test Scope's area at all - the point is that passing the
    # scope explicitly tracks it there anyway, unlike the old per-entity
    # area resolution.
    hass.states.async_set("light.elsewhere", "on", {"brightness": 100, "color_temp_kelvin": 3000})

    results = await _check(hass, ["light.a", "light.elsewhere"])
    assert results["light.a"]["scope"] == "Test Scope"
    assert results["light.elsewhere"]["scope"] == "Test Scope"


async def test_check_control_requires_a_scope(setup_integration: HomeAssistant):
    """This service exists only to answer questions about tracking - with
    nothing to check against, it's not a call worth making, so the schema
    says so up front rather than always answering "untracked"."""
    hass = setup_integration
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=100, color_temp_kelvin=3000)
    with pytest.raises(vol.Invalid):
        await _check(hass, ["light.a"], scope_device_id=None)


async def test_record_write_requires_a_scope(setup_integration: HomeAssistant):
    """This service exists only to write tracking claims - with nowhere
    to record into, there's nothing for it to do, so the schema says so
    up front rather than always silently recording nothing."""
    hass = setup_integration
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=100, color_temp_kelvin=3000)
    with pytest.raises(vol.Invalid):
        await _record_write(hass, ["light.a"], scope_device_id=None)


async def test_record_write_records_everything_passed_once_a_scope_is_given(setup_integration: HomeAssistant):
    """The counterpart: with a scope, every entity in the call is
    recorded into it - one scope per call, not one resolved per entity.
    light.elsewhere isn't anywhere near the scope's own target area, and
    is recorded anyway, because the caller said so."""
    hass = setup_integration
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=100, color_temp_kelvin=3000)
    hass.states.async_set("light.elsewhere", "on", {"brightness": 100, "color_temp_kelvin": 3000})

    result = await _record_write(hass, ["light.a", "light.elsewhere"])

    assert sorted(result["recorded"]) == ["light.a", "light.elsewhere"]


async def test_scope_device_id_rejects_a_nonexistent_device(setup_integration: HomeAssistant):
    """A typo'd or stale device_id is a caller mistake, not an absent
    scope - it must be loud, not silently treated the same as omitting
    scope_device_id entirely."""
    hass = setup_integration
    with pytest.raises(ServiceValidationError):
        await _check(hass, ["light.a"], scope_device_id="not_a_real_device_id")


async def test_scope_device_id_rejects_a_device_that_isnt_a_tracking_scope(setup_integration: HomeAssistant):
    """Any other device - a schedule sensor's device, or something from
    a completely unrelated integration - is just as much a caller
    mistake as a nonexistent id, and rejected the same way."""
    hass = setup_integration
    from homeassistant.helpers import device_registry as dr

    entry = MockConfigEntry(domain="not_flare")
    entry.add_to_hass(hass)
    other_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("not_flare", "something_else")}
    )
    # A device registry write also triggers two_step_check.py's own
    # 5s-debounced watcher (see _attach_tracking_sensors's own note) -
    # flush it or the harness fails on a lingering timer.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError):
        await _check(hass, ["light.a"], scope_device_id=other_device.id)


async def test_check_control_does_not_take_force(setup_integration: HomeAssistant):
    """Forcing is something a write does. As a question it has exactly
    one answer - is_blocked returns False for everything when force is
    set - so accepting it would only invite callers to ask something
    uninformative."""
    hass = setup_integration
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "check_control",
            {"entities": ["light.a"], "force": True},
            blocking=True,
            return_response=True,
        )


async def test_the_last_light_going_off_releases_the_whole_scope(setup_integration: HomeAssistant):
    """A room with nothing on is a room nobody is using, so its claims
    go and every light in it is free again."""
    hass = setup_integration
    our_context = Context()
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=100, color_temp_kelvin=3000, context=our_context)
    await _record_write(hass, ["light.a"], context=our_context)
    _set_light(hass, "light.a", "off", context=Context(id="ctx-motion-off"))
    await hass.async_block_till_done()

    assert await _check(hass, ["light.a"]) == {
        "light.a": {"blocked": False, "status": "off", "matched_via": None, "scope": "Test Scope"}
    }


async def test_a_light_switched_off_by_hand_in_a_lit_room_is_left_off(setup_integration: HomeAssistant):
    """The counterpart, and the point of the change: switching one light
    off while the room is still in use is a choice, not a gap to fill.
    light.sibling stays on, so the scope is not released and light.a
    keeps the claim it no longer matches.

    Note "the room" means the scope, not the physical room - an
    untracked light being on holds nothing open."""
    hass = setup_integration
    ours = Context()
    for e in ("light.a", "light.sibling"):
        _set_light(hass, e, "on", supported_color_modes=["color_temp"], brightness=100, color_temp_kelvin=3000, context=ours)
    await _record_write(
        hass,
        ["light.a", "light.sibling"],
        targets={"light.a": {"brightness": 100, "color_temp_kelvin": 3000}},
        context=ours,
    )
    _set_light(hass, "light.a", "off", context=Context(id="ctx-wall-switch"))
    await hass.async_block_till_done()

    results = await _check(hass, ["light.a"])
    assert results["light.a"]["status"] == "overridden"
    assert results["light.a"]["blocked"] is True


async def test_apply_lighting_records_what_a_turn_off_asked_for(setup_integration: HomeAssistant):
    """A turn-off is a write, and records a target of its own. Without
    one there is nothing to tell our own off from anyone else's once the
    write's context expires, and a room turned off at bedtime could
    never be turned on again."""
    hass = setup_integration
    turn_offs = async_mock_service(hass, "light", "turn_off")
    async_mock_service(hass, "light", "turn_on")  # light.b is still driven normally
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=100, color_temp_kelvin=3000)
    _set_light(hass, "light.b", "on", supported_color_modes=["color_temp"], brightness=100, color_temp_kelvin=3000)

    await _apply(hass, ["light.a", "light.b"], brightness_multipliers={"light.a": 0})

    assert turn_offs and turn_offs[-1].data["entity_id"] == ["light.a"]
    registry = next(iter(hass.data[DOMAIN].values()))
    assert registry.all_records()["light.a"]["latest"]["target"] == {"state": "off"}


async def test_clear_claims_frees_a_light_stuck_overridden(setup_integration: HomeAssistant):
    """The manual escape hatch: an entity showing "overridden" (blocked
    for any caller) with no other way back - see write_tracking.py's
    async_clear docstring for why this can happen on its own (an
    excluded entity's own pending target goes stale forever, since
    build_groups() never calls record_write/async_record for
    anything already excluded). clear_claims discards the record
    outright; the very next check_control call then sees a brand-new
    entity with nothing to compare against - unclaimed, never blocked."""
    hass = setup_integration
    our_context = Context()
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=100, color_temp_kelvin=3000, context=our_context)
    await _record_write(hass, ["light.a"], context=our_context)
    # A genuinely different value under a genuinely different context -
    # the delayed-echo rescue can't save this one either, so it reads as
    # overridden and would stay excluded from every future write.
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=40, color_temp_kelvin=6000)
    results = await _check(hass, ["light.a"])
    assert results["light.a"]["status"] == "overridden"

    await hass.services.async_call(
        DOMAIN, "clear_claims", {"entities": ["light.a"], "scope_device_id": _scope_device_id(hass)}, blocking=True
    )

    results = await _check(hass, ["light.a"])
    assert results["light.a"] == {"blocked": False, "status": "untracked", "matched_via": None, "scope": "Test Scope"}


async def test_clear_claims_requires_a_scope(setup_integration: HomeAssistant):
    """This service exists only to discard tracking claims - with
    nowhere to clear from, there's nothing for it to do, so the schema
    says so up front rather than always silently clearing nothing."""
    hass = setup_integration
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, "clear_claims", {"entities": ["light.never_tracked"]}, blocking=True, return_response=True
        )


async def test_clear_claims_is_a_noop_for_an_untracked_entity_within_a_real_scope(setup_integration: HomeAssistant):
    """The more meaningful case: a scope that genuinely exists, asked to
    clear an entity it's never written - nothing to discard, no error."""
    hass = setup_integration
    result = await hass.services.async_call(
        DOMAIN,
        "clear_claims",
        {"entities": ["light.never_tracked"], "scope_device_id": _scope_device_id(hass)},
        blocking=True,
        return_response=True,
    )
    assert result == {"cleared": ["light.never_tracked"]}
    assert _registry(hass).all_records() == {}


async def test_override_protection_survives_a_real_write_tracking_round_trip(setup_integration: HomeAssistant):
    """End-to-end version of what tests/test_grouping.py already proves
    at the pure-function level - this time through the real
    write_tracking.py Store and __init__.py's context propagation, not
    a fake. A light manually changed (a different context.id) after our
    own write must be left alone on the next non-forced call with the
    the same state device.

    This is also the direct regression test for the first-write
    baseline in write_tracking.py's async_record(): only one
    apply_lighting call ever happens here before the external change,
    so `confirmed` is never promoted from a prior `pending` - it can
    only be the synthetic pre-write-context baseline recorded on that
    first call. Reverting that baseline back to `confirmed = None`
    makes this fail (the external change would be waved through as
    "no record yet -> free" instead of correctly recognised as
    external)."""
    hass = setup_integration
    turn_on_calls = async_mock_service(hass, "light", "turn_on")
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    our_context = Context()
    await _apply(
        hass,
        ['light.a'],
        brightness=180,
        color_temp_kelvin=3200,
        transition=2,
        context=our_context,
    )
    assert len(turn_on_calls) == 1

    # Reflect our own write back into state (async_mock_service doesn't
    # do this for us) with the exact context apply_lighting issued it
    # under - matching what write_tracker.async_record just persisted.
    _set_light(
        hass,
        "light.a",
        "on",
        supported_color_modes=["color_temp"],
        brightness=180,
        color_temp_kelvin=3200,
        context=our_context,
    )

    # Someone else changes it - a different context, simulating a wall
    # switch or another automation.
    _set_light(
        hass,
        "light.a",
        "on",
        supported_color_modes=["color_temp"],
        brightness=90,
        color_temp_kelvin=3200,
    )

    await _apply(
        hass,
        ['light.a'],
        brightness=180,
        color_temp_kelvin=3200,
        transition=2,
    )

    # Still just the one call from before - the second, non-forced call
    # correctly left the externally-changed light alone.
    assert len(turn_on_calls) == 1


async def test_a_devices_own_delayed_echo_does_not_permanently_lock_the_light_out(setup_integration: HomeAssistant):
    """Real-world incident, not a hypothetical: light.kitchen_3/
    light.kitchen_5 sat excluded from every tick for over an hour, still
    correctly lit the whole time, because HA's own Entity._context
    expires 5 seconds after the service call that set it (confirmed
    against homeassistant/core.py) - a device whose real Zigbee/MQTT
    confirmation lands after that window reports back under a brand-new,
    unrelated context.id even though it's echoing exactly the value we
    asked for. Unlike test_override_protection_survives_a_real_write_tracking_round_trip
    above, the light's *values* never actually change here - only its
    context.id does, with nothing in between issuing a real command.
    Before the fix in grouping.py's externally_set(), this light would
    stay excluded forever once the curve moved on, exactly like the
    original bug write_tracking.py's confirmed/pending design already
    fixes for a dropped write - just triggered by an echo instead."""
    hass = setup_integration
    turn_on_calls = async_mock_service(hass, "light", "turn_on")
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    # Two real writes to *different* targets, so a genuine `confirmed`
    # baseline exists via an observed promotion (not just the lenient
    # first-write gap) - matching kitchen_3/kitchen_5's actual live
    # state, which had both a confirmed and a pending claim.
    first_context = Context()
    await _apply(
        hass,
        ['light.a'],
        brightness=180,
        color_temp_kelvin=3200,
        transition=2,
        context=first_context,
    )
    _set_light(
        hass,
        "light.a",
        "on",
        supported_color_modes=["color_temp"],
        brightness=180,
        color_temp_kelvin=3200,
        context=first_context,
    )

    second_context = Context()
    await _apply(hass, ['light.a'], transition=2, context=second_context)
    _set_light(
        hass,
        "light.a",
        "on",
        supported_color_modes=["color_temp"],
        brightness=200,
        color_temp_kelvin=3000,
        context=second_context,
    )
    assert len(turn_on_calls) == 2

    # The device's own delayed confirmation lands under a fresh context -
    # not from any service call, not our_context, not tied to any
    # apply_lighting write at all - reporting within tolerance of what
    # we asked for (a real bulb's round-trip is rarely exact), not the
    # identical value: HA only replaces an entity's context on an actual
    # state change, not a same-state "state_reported" re-set, so reusing
    # 200/3000 exactly here would leave the light's context untouched
    # and silently defeat this test (see the identical gotcha noted in
    # test_force_bypasses_protection_and_reclaims_the_light above).
    _set_light(
        hass,
        "light.a",
        "on",
        supported_color_modes=["color_temp"],
        brightness=201,
        color_temp_kelvin=3005,
    )

    # Same target - nothing should be written (already correct either
    # way), but this must NOT be the moment the light gets marked
    # externally-set.
    await _apply(hass, ['light.a'], transition=2)
    assert len(turn_on_calls) == 2

    # The curve moves on to a genuinely different value - the real test:
    # a light that's actually excluded would stay excluded here too.
    await _apply(
        hass,
        ['light.a'],
        brightness=100,
        color_temp_kelvin=4500,
        transition=2,
    )
    assert len(turn_on_calls) == 3


async def test_two_step_transition_generates_two_distinct_contexts(setup_integration: HomeAssistant):
    """A two-step transition (no_combined_transition label) really is
    two separate light.turn_on calls - brightness first, then colour -
    and each now gets its own real Context() rather than sharing
    call.context (see __init__.py's _two_step_turn_on). Both land in
    write_tracking: the colour step's (the final, complete state) as
    the claim's primary context_id, the brightness step's as its
    secondary_context_id."""
    hass = setup_integration
    turn_on_calls = async_mock_service(hass, "light", "turn_on")
    await _label_two_step(hass, "light.a")
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    await _apply(hass, ['light.a'], transition=0.2)

    assert len(turn_on_calls) == 2
    brightness_call, color_call = turn_on_calls
    assert brightness_call.data == {"entity_id": ["light.a"], "transition": 0.1, "brightness": 200}
    assert color_call.data["color_temp_kelvin"] == 3000
    # Two genuinely different contexts, and neither is the apply_lighting
    # call's own (nothing threads that through to either light.turn_on
    # call for a two-step entity anymore - see _two_step_turn_on).
    assert brightness_call.context.id != color_call.context.id

    tracker = _registry(hass)
    scope = _test_scope_subentry_id(hass)
    assert tracker.latest_context_id(scope, "light.a") == color_call.context.id
    assert tracker.latest_secondary_context_id(scope, "light.a") == brightness_call.context.id


async def test_two_step_brightness_step_landing_alone_is_recognised_as_ours(setup_integration: HomeAssistant):
    """The actual incident this fixes: a two-step bulb reporting back
    after just its brightness-only step (a real, expected intermediate
    state for these bulbs, not an anomaly) used to look externally-set,
    because that intermediate report's context matched neither the
    single shared context.id apply_lighting used to record nor the
    final combined target's values. Now the brightness step gets its
    own context, recorded as this claim's secondary_context_id - a
    device reporting back under exactly that context is recognised
    directly, even though its colour hasn't caught up to the new target
    yet."""
    hass = setup_integration
    turn_on_calls = async_mock_service(hass, "light", "turn_on")
    await _label_two_step(hass, "light.a")
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    await _apply(hass, ['light.a'], transition=0.2)
    assert len(turn_on_calls) == 2
    brightness_context = turn_on_calls[0].context

    # The device confirms the brightness step on its own - genuinely new
    # brightness, colour still at whatever it was before (light started
    # off, so color_temp_kelvin was never set at all) - under exactly
    # the context that step was issued with.
    _set_light(
        hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=200, context=brightness_context
    )

    results = await _check(hass, ["light.a"])
    assert results["light.a"] == {
        "blocked": False,
        "status": "controlled",
        "matched_via": "latest-context",
        "scope": "Test Scope",
    }


async def test_two_step_promotion_recognises_a_match_via_the_secondary_context(setup_integration: HomeAssistant):
    """write_tracking.py's own promotion logic (async_record) is a
    genuinely different code path from classify()'s read-side check
    covered above - both call the shared _context_matches helper, but
    only a real second apply_lighting call actually exercises the
    promotion branch. A device that only ever confirms a two-step
    write's brightness step (never the colour one - e.g. a dropped
    colour command) must still promote that write into `confirmed` on
    the next call, not treat it as never-landed."""
    hass = setup_integration
    async_mock_service(hass, "light", "turn_on")
    await _label_two_step(hass, "light.a")
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    await _apply(hass, ['light.a'], transition=0.2)

    tracker = _registry(hass)
    scope = _test_scope_subentry_id(hass)
    brightness_ctx_1 = tracker.latest_secondary_context_id(scope, "light.a")
    color_ctx_1 = tracker.latest_context_id(scope, "light.a")
    assert brightness_ctx_1 is not None and color_ctx_1 is not None

    # The device only ever confirms the brightness step - the colour
    # command silently dropped (a real, if less common, two-step
    # failure mode alongside the "neither step confirms" case the
    # dropped-first-write test below already covers).
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=200, context=Context(id=brightness_ctx_1))

    await _apply(hass, ['light.a'], transition=0.2)

    # A fresh ClaimRegistry/Store, not the one used above - the test
    # harness's mock_storage patch caches a Store instance's first-ever
    # load in `store._data` and never refreshes it on a later load (real
    # HA's Store clears that field right after a write; the mock deliberately
    # doesn't, since it exists to skip disk I/O, not to model write-then-
    # reread staleness) - reusing `tracker` here would just re-read the
    # snapshot from before call 2 ran.
    tracker_after = _registry(hass)
    # Promoted: the first write's own claim (both its contexts) is now
    # `confirmed`, proven via the secondary (brightness) context match,
    # not the primary (colour) one - the light never reported the
    # colour step's context at all.
    assert tracker_after.observed_context_id(scope, "light.a") == color_ctx_1
    assert tracker_after.observed_secondary_context_id(scope, "light.a") == brightness_ctx_1


async def test_a_dropped_first_write_self_heals_on_the_next_tick_with_no_interference(
    setup_integration: HomeAssistant,
):
    """The other half of the first-write story alongside the round-trip
    test above: a light whose very first write from us never actually
    lands (the physical bulb silently drops it - state stays completely
    unchanged, unlike the round-trip test's genuine external change)
    must still be retried on the next tick, not locked out. This is the
    production bug the whole confirmed/pending redesign exists to fix
    (a kitchen light that dropped a colour-mode command and sat stuck
    for over an hour - see write_tracking.py's own module docstring)."""
    hass = setup_integration
    turn_on_calls = async_mock_service(hass, "light", "turn_on")
    # Already on, at a brightness/colour that will need correcting -
    # off->on writes bypass the override check entirely (see
    # externally_set()'s own is_state guard), so this has to start "on"
    # to actually exercise it.
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=90, color_temp_kelvin=3200)

    await _apply(
        hass,
        ['light.a'],
        brightness=180,
        color_temp_kelvin=3200,
        transition=2,
    )
    assert len(turn_on_calls) == 1

    # The write drops: state is deliberately left untouched, simulating
    # the physical bulb never adopting the command.

    await _apply(
        hass,
        ['light.a'],
        brightness=180,
        color_temp_kelvin=3200,
        transition=2,
    )

    # Retried, not locked out - the unchanged live context matched the
    # first-write baseline, so the second call still recognised light.a
    # as free to manage.
    assert len(turn_on_calls) == 2


async def test_force_bypasses_protection_and_reclaims_the_light(setup_integration: HomeAssistant):
    """force=True writes through regardless, and still records the write
    - so a later, non-forced call under that same owner_id recognises it
    as its own rather than finding an orphaned record (the bug `force`
    itself was added to fix - see grouping.py's externally_set() and
    CLAUDE.md's dated incident)."""
    hass = setup_integration
    turn_on_calls = async_mock_service(hass, "light", "turn_on")

    # A light with an existing, unrelated write record (as if a
    # different owner had claimed it).
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=90, color_temp_kelvin=3200)
    await _apply(
        hass,
        ['light.a'],
        brightness=180,
        color_temp_kelvin=3200,
        transition=2,
    )
    assert len(turn_on_calls) == 1  # the "other room" claims it

    forced_context = Context()
    await _apply(
        hass,
        ['light.a'],
        brightness=180,
        color_temp_kelvin=3200,
        transition=2,
        force=True,
        context=forced_context,
    )
    assert len(turn_on_calls) == 2  # force wrote through despite the other owner

    # Reflect the forced write's own context back into state, but at a
    # brightness that still mismatches the target - isolates the
    # externally_set check (context+owner match, so not blocked) from
    # the tolerance check (brightness doesn't match, so still "needs
    # updating") rather than conflating the two. Deliberately a
    # different brightness (95, not 90) from the light's very first
    # state above - HA only replaces context on an actual state change,
    # not a same-state "state_reported" re-set (confirmed live: reusing
    # 90 here left the light's context untouched, silently defeating
    # this test).
    _set_light(
        hass,
        "light.a",
        "on",
        supported_color_modes=["color_temp"],
        brightness=95,
        color_temp_kelvin=3200,
        context=forced_context,
    )
    result = await hass.services.async_call(
        DOMAIN,
        "compute_lighting_groups",
        {
            "entities": ["light.a"],
            "brightness": 180,
            "color_temp_kelvin": 3200,
            "scope_device_id": _scope_device_id(hass),
        },
        blocking=True,
        return_response=True,
    )
    # Same context as the forced write, same scope checking it back -
    # not externally-set, and still short of target, so correctly
    # included for update.
    assert result["groups"][0]["combined"] == ["light.a"]


async def test_write_tracking_record_is_cleared_when_light_goes_unavailable(setup_integration: HomeAssistant):
    """Confirms ClaimRegistry.async_start_listening() (wired up by
    async_setup_entry, already active via the setup_integration fixture)
    actually does what it's for - see write_tracking.py's own module
    docstring on the "device regaining power" gap this closes. Without
    it, the light's post-reconnect context (never anything
    apply_lighting itself wrote) would make it look externally-set
    forever, needing a forced write to ever recover - this proves a
    perfectly ordinary, non-forced call is enough once it's cleared."""
    hass = setup_integration
    turn_on_calls = async_mock_service(hass, "light", "turn_on")

    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    await _apply(hass, ['light.a'], brightness=180, color_temp_kelvin=3200, transition=2)
    assert len(turn_on_calls) == 1

    # The device drops off the network.
    _set_light(hass, "light.a", "unavailable", supported_color_modes=["color_temp"])
    await hass.async_block_till_done()

    # It reconnects - a real device's own state report, carrying a
    # context we never issued (matching HA core's own "no context given
    # -> fresh Context()" fallback, which is exactly what a reconnecting
    # device's own state report goes through).
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=90, color_temp_kelvin=3200)
    await hass.async_block_till_done()

    # A normal, non-forced call still updates it - proving there was no
    # stale record left to conflict with the light's new, unrelated
    # context. Before this fix, this second call would have found the
    # light "externally set" and left it alone.
    await _apply(hass, ['light.a'], brightness=180, color_temp_kelvin=3200, transition=2)
    assert len(turn_on_calls) == 2
async def test_prune_stale_removes_a_record_untouched_past_the_cutoff(setup_integration: HomeAssistant):
    hass = setup_integration
    async_mock_service(hass, "light", "turn_on")
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    with freeze_time(dt_util.utcnow()) as frozen:
        await _apply(
            hass,
            ['light.a'],
            brightness=180,
            color_temp_kelvin=3200,
            transition=2,
        )
        tracker = _registry(hass)
        assert "light.a" in tracker.all_records()

        frozen.move_to(dt_util.utcnow() + timedelta(days=STALE_RECORD_MAX_AGE_DAYS, hours=1))
        await tracker.async_prune_stale()

    assert "light.a" not in tracker.all_records()


async def test_prune_stale_leaves_a_recent_record_alone(setup_integration: HomeAssistant):
    """The boundary case - a record still within the cutoff must survive
    a prune pass. Without this, a mutation that pruned everything
    regardless of age would pass test_prune_stale_removes_a_record_untouched_past_the_cutoff
    just as easily as the real implementation."""
    hass = setup_integration
    async_mock_service(hass, "light", "turn_on")
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    with freeze_time(dt_util.utcnow()) as frozen:
        await _apply(
            hass,
            ['light.a'],
            brightness=180,
            color_temp_kelvin=3200,
            transition=2,
        )
        tracker = _registry(hass)

        frozen.move_to(dt_util.utcnow() + timedelta(hours=1))
        await tracker.async_prune_stale()

    assert "light.a" in tracker.all_records()


async def test_prune_stale_leaves_a_record_with_no_last_seen_alone(setup_integration: HomeAssistant):
    """Defensive branch: a record whose age genuinely can't be judged
    (nothing writes this shape, but nothing in this module ever deletes
    on ambiguity either - see async_prune_stale's own docstring) must
    never be pruned."""
    hass = setup_integration
    tracker = _registry(hass)
    tracker._stores[next(iter(tracker._stores))].claims["light.a"] = {  # this shape shouldn't occur naturally
        "observed": {"context_id": "ctx-old", "recorded_at": None, "target": None},
        "latest": None,
        "last_seen": None,
    }

    await tracker.async_prune_stale()

    assert "light.a" in tracker.all_records()


async def test_recovered_light_is_freed_while_an_unrelated_override_stays_protected(
    setup_integration: HomeAssistant,
):
    """The clear-on-unavailable fix only touches the entity that
    actually went unavailable - a sibling under its own real, unrelated
    override (never went unavailable, so its record is never cleared)
    must stay protected exactly as before."""
    hass = setup_integration
    turn_on_calls = async_mock_service(hass, "light", "turn_on")

    for entity_id in ("light.recovering", "light.sibling"):
        _set_light(hass, entity_id, "off", supported_color_modes=["color_temp"])

    our_context = Context()
    await _apply(
        hass,
        ['light.recovering', 'light.sibling'],
        brightness=180,
        color_temp_kelvin=3200,
        transition=2,
        context=our_context,
    )
    assert len(turn_on_calls) == 1

    # Reflect the write into state under our own context, matching what
    # write_tracker just recorded for both lights.
    for entity_id in ("light.recovering", "light.sibling"):
        _set_light(
            hass,
            entity_id,
            "on",
            supported_color_modes=["color_temp"],
            brightness=180,
            color_temp_kelvin=3200,
            context=our_context,
        )

    # light.recovering drops off the network and reconnects - its own
    # state report, an unrelated fresh context.
    _set_light(hass, "light.recovering", "unavailable", supported_color_modes=["color_temp"])
    await hass.async_block_till_done()
    _set_light(
        hass, "light.recovering", "on", supported_color_modes=["color_temp"], brightness=90, color_temp_kelvin=3200
    )

    # light.sibling never went unavailable - someone just changed it
    # directly (a wall switch, another automation).
    _set_light(
        hass, "light.sibling", "on", supported_color_modes=["color_temp"], brightness=90, color_temp_kelvin=3200
    )
    await hass.async_block_till_done()

    result = await hass.services.async_call(
        DOMAIN,
        "compute_lighting_groups",
        {
            "entities": ["light.recovering", "light.sibling"],
            "brightness": 180,
            "color_temp_kelvin": 3200,
            "scope_device_id": _scope_device_id(hass),
        },
        blocking=True,
        return_response=True,
    )
    combined = result["groups"][0]["combined"]
    assert "light.recovering" in combined
    assert "light.sibling" not in combined


async def test_a_restart_style_unavailable_blip_does_not_clear_an_existing_record(
    setup_integration: HomeAssistant,
):
    """Live incident, 2026-08-16: a light dimmed by hand hours after a
    plain HA restart got silently overwritten on the very next tick.
    Root cause - the clear-on-unavailable listener cleared the record
    for *any* observed transition into unavailable/unknown, and nearly
    every entity passes through unavailable/unknown as a routine part
    of every restart (a fresh process's state machine has no prior
    state for anything yet - old_state is None), indistinguishable from
    a genuine drop if only the destination state is checked.

    This reproduces that shape directly: seed a real write record, then
    fire a state_changed event for a transition INTO unavailable with
    no real prior on/off state (old_state None, or old_state itself
    already unavailable/unknown - both routine parts of startup, not a
    real drop) - the record must survive, so a manual change made after
    it is still correctly protected."""
    hass = setup_integration
    turn_on_calls = async_mock_service(hass, "light", "turn_on")

    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    our_context = Context()
    await _apply(
        hass,
        ['light.a'],
        brightness=180,
        color_temp_kelvin=3200,
        transition=2,
        context=our_context,
    )
    assert len(turn_on_calls) == 1
    _set_light(
        hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=180, color_temp_kelvin=3200,
        context=our_context,
    )
    await hass.async_block_till_done()

    # Simulate the entity's in-memory state vanishing and reappearing
    # the way it does across a real HA restart - the write_tracker's
    # own record survives (Store-persisted, untouched by hass.states),
    # but the state machine has no history for this entity in the new
    # process, so its first event has old_state=None, same as this.
    hass.states.async_remove("light.a")
    await hass.async_block_till_done()
    _set_light(hass, "light.a", "unavailable", supported_color_modes=["color_temp"])
    await hass.async_block_till_done()
    _set_light(
        hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=180, color_temp_kelvin=3200,
        context=our_context,
    )
    await hass.async_block_till_done()

    # Someone changes it by hand - a genuinely different context.
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=90, color_temp_kelvin=3200)
    await hass.async_block_till_done()

    result = await hass.services.async_call(
        DOMAIN,
        "compute_lighting_groups",
        {
            "entities": ["light.a"],
            "brightness": 180,
            "color_temp_kelvin": 3200,
            "scope_device_id": _scope_device_id(hass),
        },
        blocking=True,
        return_response=True,
    )
    assert "light.a" not in result["groups"][0]["combined"]


