"""
Integration tests for the adaptive_lighting_helpers services
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

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.adaptive_lighting_helpers import _build_lookup, async_setup_entry
from custom_components.adaptive_lighting_helpers.grouping import build_groups
from custom_components.adaptive_lighting_helpers.write_tracking import STALE_RECORD_MAX_AGE_DAYS, LastWriteTracker

DOMAIN = "adaptive_lighting_helpers"


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
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()
    return entry


def _set_light(hass: HomeAssistant, entity_id: str, state: str, *, context: Context | None = None, **attrs) -> None:
    hass.states.async_set(entity_id, state, attrs, context=context)


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

    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 180,
            "color_temp_kelvin": 3200,
            "transition": 2,
        },
        blocking=True,
    )

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
        await hass.services.async_call(
            DOMAIN,
            "apply_lighting",
            {
                "entities": ["light.a"],
                "color_temp_kelvin": 3200,
                "transition": 2,
            },
            blocking=True,
        )


async def test_apply_lighting_non_numeric_color_temp_kelvin_raises(setup_integration: HomeAssistant):
    hass = setup_integration
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "apply_lighting",
            {
                "entities": ["light.a"],
                "brightness": 180,
                "color_temp_kelvin": "not-a-number",
                "transition": 2,
            },
            blocking=True,
        )


async def test_apply_lighting_accepts_an_explicit_null_rgb_color(setup_integration: HomeAssistant):
    """A hand-rolled 'bring your own sensor' entity is free to omit
    rgb_color entirely (see docs/BLUEPRINT.md), and the blueprint's own
    state_attr(adaptive_sensor, 'rgb_color') then renders a literal
    None, not an omitted key - vol.Length applied to None used to fail
    schema validation outright (vol.Length expects a sized value), which
    would have broken apply_lighting for every such room the moment the
    blueprint started passing this field unconditionally. The fix is
    vol.Any(None, ...) on the schema; this must NOT raise."""
    hass = setup_integration
    turn_on_calls = async_mock_service(hass, "light", "turn_on")
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 180,
            "color_temp_kelvin": 3200,
            "rgb_color": None,
            "transition": 2,
        },
        blocking=True,
    )

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


async def test_check_ownership_reports_unclaimed_for_a_brand_new_entity(setup_integration: HomeAssistant):
    """check_ownership is genuinely standalone - no apply_lighting call,
    no sensor, just a light and the write-tracking mechanism."""
    hass = setup_integration
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=100, color_temp_kelvin=3000)

    result = await hass.services.async_call(
        DOMAIN,
        "check_ownership",
        {"entities": ["light.a"], "owner_id": "automation.test_room"},
        blocking=True,
        return_response=True,
    )

    assert result["results"]["light.a"] == {"blocked": False, "status": "unclaimed", "owner_id": None, "matched_via": None}


async def test_check_ownership_and_record_ownership_round_trip(setup_integration: HomeAssistant):
    """The two services used together, standalone - no apply_lighting
    involved at all, matching how an independent automation would use
    this mechanism on its own light.turn_on calls."""
    hass = setup_integration
    our_context = Context()
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=100, color_temp_kelvin=3000, context=our_context)

    await hass.services.async_call(
        DOMAIN,
        "record_ownership",
        {"entities": ["light.a"], "owner_id": "automation.test_room", "targets": {"light.a": {"brightness": 100, "color_temp_kelvin": 3000}}},
        blocking=True,
        context=our_context,
    )

    # Immediately after, still under the same context - not blocked, and
    # attributed to us. "pending" (not "controlled") because this is the
    # entity's first-ever tracked write: the synthetic first-write
    # baseline records the pre-write context as `confirmed` too, so both
    # claims happen to share the same context.id here - pending is
    # checked first, matching classify()'s own precedence.
    result = await hass.services.async_call(
        DOMAIN,
        "check_ownership",
        {"entities": ["light.a"], "owner_id": "automation.test_room"},
        blocking=True,
        return_response=True,
    )
    assert result["results"]["light.a"] == {
        "blocked": False,
        "status": "pending",
        "owner_id": "automation.test_room",
        "matched_via": "context",
    }

    # Someone else changes it - a different context, different values.
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=40, color_temp_kelvin=6000)

    result = await hass.services.async_call(
        DOMAIN,
        "check_ownership",
        {"entities": ["light.a"], "owner_id": "automation.test_room"},
        blocking=True,
        return_response=True,
    )
    assert result["results"]["light.a"] == {"blocked": True, "status": "overridden", "owner_id": None, "matched_via": None}

    # A *different* owner_id asking about the same still-matching claim
    # correctly sees it as blocked too (it's not theirs, even though
    # nothing about the light's context has changed since our own write).
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=100, color_temp_kelvin=3000, context=our_context)
    result = await hass.services.async_call(
        DOMAIN,
        "check_ownership",
        {"entities": ["light.a"], "owner_id": "automation.other_room"},
        blocking=True,
        return_response=True,
    )
    assert result["results"]["light.a"] == {
        "blocked": True,
        "status": "pending",
        "owner_id": "automation.test_room",
        "matched_via": "context",
    }


async def test_check_ownership_force_bypasses_regardless_of_claims(setup_integration: HomeAssistant):
    hass = setup_integration
    our_context = Context()
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=100, color_temp_kelvin=3000, context=our_context)
    await hass.services.async_call(
        DOMAIN,
        "record_ownership",
        {"entities": ["light.a"], "owner_id": "automation.test_room"},
        blocking=True,
        context=our_context,
    )
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=40, color_temp_kelvin=6000)

    result = await hass.services.async_call(
        DOMAIN,
        "check_ownership",
        {"entities": ["light.a"], "owner_id": "automation.other_room", "force": True},
        blocking=True,
        return_response=True,
    )
    assert result["results"]["light.a"]["blocked"] is False


async def test_check_ownership_off_light_is_never_blocked(setup_integration: HomeAssistant):
    hass = setup_integration
    our_context = Context()
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=100, color_temp_kelvin=3000, context=our_context)
    await hass.services.async_call(
        DOMAIN,
        "record_ownership",
        {"entities": ["light.a"], "owner_id": "automation.test_room"},
        blocking=True,
        context=our_context,
    )
    # Off under a completely different, unrecorded context - exactly the
    # off-light misclassification found live and fixed structurally via
    # override_protection.classify()'s own "off" precondition.
    _set_light(hass, "light.a", "off", context=Context(id="ctx-motion-off"))

    result = await hass.services.async_call(
        DOMAIN,
        "check_ownership",
        {"entities": ["light.a"], "owner_id": "automation.other_room"},
        blocking=True,
        return_response=True,
    )
    assert result["results"]["light.a"] == {"blocked": False, "status": "off", "owner_id": None, "matched_via": None}


async def test_clear_ownership_frees_a_light_stuck_overridden(setup_integration: HomeAssistant):
    """The manual escape hatch: an entity showing "overridden" (blocked
    for any owner_id) with no other way back - see write_tracking.py's
    async_clear docstring for why this can happen on its own (an
    excluded entity's own pending target goes stale forever, since
    build_groups() never calls record_ownership/async_record for
    anything already excluded). clear_ownership discards the record
    outright; the very next check_ownership call then sees a brand-new
    entity with nothing to compare against - unclaimed, never blocked."""
    hass = setup_integration
    our_context = Context()
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=100, color_temp_kelvin=3000, context=our_context)
    await hass.services.async_call(
        DOMAIN,
        "record_ownership",
        {"entities": ["light.a"], "owner_id": "automation.test_room"},
        blocking=True,
        context=our_context,
    )
    # A genuinely different value under a genuinely different context -
    # the delayed-echo rescue can't save this one either, so it reads as
    # overridden and would stay excluded from every future write.
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=40, color_temp_kelvin=6000)
    result = await hass.services.async_call(
        DOMAIN,
        "check_ownership",
        {"entities": ["light.a"], "owner_id": "automation.test_room"},
        blocking=True,
        return_response=True,
    )
    assert result["results"]["light.a"]["status"] == "overridden"

    await hass.services.async_call(DOMAIN, "clear_ownership", {"entities": ["light.a"]}, blocking=True)

    result = await hass.services.async_call(
        DOMAIN,
        "check_ownership",
        {"entities": ["light.a"], "owner_id": "automation.test_room"},
        blocking=True,
        return_response=True,
    )
    assert result["results"]["light.a"] == {"blocked": False, "status": "unclaimed", "owner_id": None, "matched_via": None}


async def test_clear_ownership_is_a_noop_for_an_untracked_entity(setup_integration: HomeAssistant):
    hass = setup_integration
    result = await hass.services.async_call(
        DOMAIN, "clear_ownership", {"entities": ["light.never_tracked"]}, blocking=True, return_response=True
    )
    assert result == {"cleared": ["light.never_tracked"]}


async def test_override_protection_survives_a_real_write_tracking_round_trip(setup_integration: HomeAssistant):
    """End-to-end version of what tests/test_grouping.py already proves
    at the pure-function level - this time through the real
    write_tracking.py Store and __init__.py's context propagation, not
    a fake. A light manually changed (a different context.id) after our
    own write must be left alone on the next non-forced call with the
    same owner_id.

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
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 180,
            "color_temp_kelvin": 3200,
            "transition": 2,
            "owner_id": "automation.test_room",
        },
        blocking=True,
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

    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 180,
            "color_temp_kelvin": 3200,
            "transition": 2,
            "owner_id": "automation.test_room",
        },
        blocking=True,
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
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 180,
            "color_temp_kelvin": 3200,
            "transition": 2,
            "owner_id": "automation.test_room",
        },
        blocking=True,
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
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 200,
            "color_temp_kelvin": 3000,
            "transition": 2,
            "owner_id": "automation.test_room",
        },
        blocking=True,
        context=second_context,
    )
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
    # test_force_bypasses_protection_and_reclaims_ownership above).
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
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 200,
            "color_temp_kelvin": 3000,
            "transition": 2,
            "owner_id": "automation.test_room",
        },
        blocking=True,
    )
    assert len(turn_on_calls) == 2

    # The curve moves on to a genuinely different value - the real test:
    # a light that's actually excluded would stay excluded here too.
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 100,
            "color_temp_kelvin": 4500,
            "transition": 2,
            "owner_id": "automation.test_room",
        },
        blocking=True,
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

    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 200,
            "color_temp_kelvin": 3000,
            "transition": 0.2,
            "owner_id": "automation.room",
        },
        blocking=True,
    )

    assert len(turn_on_calls) == 2
    brightness_call, color_call = turn_on_calls
    assert brightness_call.data == {"entity_id": ["light.a"], "transition": 0.1, "brightness": 200}
    assert color_call.data["color_temp_kelvin"] == 3000
    # Two genuinely different contexts, and neither is the apply_lighting
    # call's own (nothing threads that through to either light.turn_on
    # call for a two-step entity anymore - see _two_step_turn_on).
    assert brightness_call.context.id != color_call.context.id

    tracker = LastWriteTracker(hass)
    await tracker.async_load()
    assert tracker.pending_context_id("light.a") == color_call.context.id
    assert tracker.pending_secondary_context_id("light.a") == brightness_call.context.id


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

    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 200,
            "color_temp_kelvin": 3000,
            "transition": 0.2,
            "owner_id": "automation.room",
        },
        blocking=True,
    )
    assert len(turn_on_calls) == 2
    brightness_context = turn_on_calls[0].context

    # The device confirms the brightness step on its own - genuinely new
    # brightness, colour still at whatever it was before (light started
    # off, so color_temp_kelvin was never set at all) - under exactly
    # the context that step was issued with.
    _set_light(
        hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=200, context=brightness_context
    )

    result = await hass.services.async_call(
        DOMAIN,
        "check_ownership",
        {"entities": ["light.a"], "owner_id": "automation.room"},
        blocking=True,
        return_response=True,
    )
    assert result["results"]["light.a"] == {
        "blocked": False,
        "status": "pending",
        "owner_id": "automation.room",
        "matched_via": "context",
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

    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 200,
            "color_temp_kelvin": 3000,
            "transition": 0.2,
            "owner_id": "automation.room",
        },
        blocking=True,
    )

    tracker = LastWriteTracker(hass)
    await tracker.async_load()
    brightness_ctx_1 = tracker.pending_secondary_context_id("light.a")
    color_ctx_1 = tracker.pending_context_id("light.a")
    assert brightness_ctx_1 is not None and color_ctx_1 is not None

    # The device only ever confirms the brightness step - the colour
    # command silently dropped (a real, if less common, two-step
    # failure mode alongside the "neither step confirms" case the
    # dropped-first-write test below already covers).
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=200, context=Context(id=brightness_ctx_1))

    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 200,
            "color_temp_kelvin": 3000,
            "transition": 0.2,
            "owner_id": "automation.room",
        },
        blocking=True,
    )

    # A fresh LastWriteTracker/Store, not the one used above - the test
    # harness's mock_storage patch caches a Store instance's first-ever
    # load in `store._data` and never refreshes it on a later load (real
    # HA's Store clears that field right after a write; the mock deliberately
    # doesn't, since it exists to skip disk I/O, not to model write-then-
    # reread staleness) - reusing `tracker` here would just re-read the
    # snapshot from before call 2 ran.
    tracker_after = LastWriteTracker(hass)
    await tracker_after.async_load()
    # Promoted: the first write's own claim (both its contexts) is now
    # `confirmed`, proven via the secondary (brightness) context match,
    # not the primary (colour) one - the light never reported the
    # colour step's context at all.
    assert tracker_after.confirmed_context_id("light.a") == color_ctx_1
    assert tracker_after.confirmed_secondary_context_id("light.a") == brightness_ctx_1


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

    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 180,
            "color_temp_kelvin": 3200,
            "transition": 2,
            "owner_id": "automation.test_room",
        },
        blocking=True,
    )
    assert len(turn_on_calls) == 1

    # The write drops: state is deliberately left untouched, simulating
    # the physical bulb never adopting the command.

    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 180,
            "color_temp_kelvin": 3200,
            "transition": 2,
            "owner_id": "automation.test_room",
        },
        blocking=True,
    )

    # Retried, not locked out - the unchanged live context matched the
    # first-write baseline, so the second call still recognised light.a
    # as free to manage.
    assert len(turn_on_calls) == 2


async def test_force_bypasses_protection_and_reclaims_ownership(setup_integration: HomeAssistant):
    """force=True writes through regardless, and still records owner_id
    - so a later, non-forced call under that same owner_id recognises it
    as its own rather than finding an orphaned record (the bug `force`
    itself was added to fix - see grouping.py's externally_set() and
    CLAUDE.md's dated incident)."""
    hass = setup_integration
    turn_on_calls = async_mock_service(hass, "light", "turn_on")

    # A light with an existing, unrelated write record (as if a
    # different owner had claimed it).
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=90, color_temp_kelvin=3200)
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 180,
            "color_temp_kelvin": 3200,
            "transition": 2,
            "owner_id": "automation.other_room",
        },
        blocking=True,
    )
    assert len(turn_on_calls) == 1  # the "other room" claims it

    forced_context = Context()
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 180,
            "color_temp_kelvin": 3200,
            "transition": 2,
            "owner_id": "automation.test_room",
            "force": True,
        },
        blocking=True,
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
            "owner_id": "automation.test_room",
        },
        blocking=True,
        return_response=True,
    )
    # Same context as the forced write, same owner_id checking it back -
    # not externally-set, and still short of target, so correctly
    # included for update.
    assert result["groups"][0]["combined"] == ["light.a"]


async def test_write_tracking_record_is_cleared_when_light_goes_unavailable(setup_integration: HomeAssistant):
    """Confirms LastWriteTracker.async_start_listening() (wired up by
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
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 180,
            "color_temp_kelvin": 3200,
            "transition": 2,
            "owner_id": "automation.room",
        },
        blocking=True,
    )
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
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 180,
            "color_temp_kelvin": 3200,
            "transition": 2,
            "owner_id": "automation.room",
        },
        blocking=True,
    )
    assert len(turn_on_calls) == 2


async def test_a_restart_resyncs_confirmed_to_live_context_so_an_on_light_stays_manageable(
    setup_integration: HomeAssistant,
):
    """The bug caught live the day this shipped: a plain HA restart
    recreates every entity's state object from scratch, so the first
    state report after restart always carries a fresh context.id - even
    for a light that never actually dropped off the network and whose
    reported value hasn't changed at all. Confirmed on the real
    instance: light.kitchen_1, genuinely on, excluded from every tick
    purely because of a restart. async_resync_to_live_state (called once
    at startup, right after async_load) is what fixes this - simulated
    here via a second LastWriteTracker loading the same persisted Store
    data, the same "restart" a real process go-around would produce."""
    hass = setup_integration
    turn_on_calls = async_mock_service(hass, "light", "turn_on")

    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 180,
            "color_temp_kelvin": 3200,
            "transition": 2,
            "owner_id": "automation.room",
        },
        blocking=True,
    )
    assert len(turn_on_calls) == 1

    # Simulate the restart in one step: light.a is still "off" (the
    # mocked light.turn_on above never actually touched hass.states), so
    # this transition to "on" is itself a genuine state change and gets
    # a real fresh context - exactly a real restart's first post-restart
    # state report for a light that never actually went unavailable, so
    # test_write_tracking_record_is_cleared_when_light_goes_unavailable's
    # listener-based fix above never gets a chance to fire. (An
    # identical-value re-set here would be silently treated as a
    # no-op "state_reported" and its context discarded - HA only
    # replaces context on a genuine change, confirmed elsewhere in this
    # file - so this has to be the light's *first* transition to "on",
    # not a second same-value re-set.)
    restart_context = Context()
    _set_light(
        hass,
        "light.a",
        "on",
        supported_color_modes=["color_temp"],
        brightness=180,
        color_temp_kelvin=3200,
        context=restart_context,
    )

    resynced_tracker = LastWriteTracker(hass)
    await resynced_tracker.async_load()
    await resynced_tracker.async_resync_to_live_state(hass)

    assert resynced_tracker.confirmed_context_id("light.a") == restart_context.id
    assert resynced_tracker.confirmed_owner_id("light.a") is None

    # The real, end-to-end proof: a plain, non-forced check from the
    # same owner still manages the light afterward, rather than finding
    # it permanently excluded. Built directly against resynced_tracker
    # (the same _build_lookup/build_groups path __init__.py's
    # compute_lighting_groups service itself uses) rather than through
    # hass.services.async_call - the *service* still reads whichever
    # tracker got registered when setup_integration's own entry was set
    # up (before this test's write even happened), not this test's
    # separately-constructed resynced_tracker, so going through the
    # service here would just prove the wrong tracker's state.
    lookup = _build_lookup(hass, resynced_tracker)
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={"light.a": 1},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3200,
        lookup=lookup,
        owner_id="automation.room",
    )
    assert groups[0].combined == ["light.a"]


async def test_the_listener_resyncs_a_light_still_unavailable_when_startup_resync_ran(
    setup_integration: HomeAssistant,
):
    """The gap found live the same day the fix above shipped:
    light.kitchen_2 stayed excluded through several real ticks after a
    restart that the bulk startup pass alone did fix light.kitchen_1
    against - both recovered from the same restart, just close enough
    together in time that only one had already reported back when
    async_resync_to_live_state ran.

    Deliberately does NOT reach "unavailable" via a live off->unavailable
    transition (light.a is set unavailable as its very first-ever state
    in this test's hass instance, old_state=None) - going through a real
    on/off->unavailable transition first would trip the *existing*
    clear-on-drop branch, which already frees a light via "no record ->
    free" on its own and would pass this test regardless of whether the
    new recovery branch does anything at all. Setting "unavailable" as
    the first-ever state instead matches what light.kitchen_2 actually
    looked like from this listener's own point of view: its drop
    happened before the new process's listener was ever attached, so
    from here it's as if the light simply arrived already down - a
    pre-existing record with no observed drop, exactly the case that
    needs the *recovery* direction specifically to be fixed."""
    hass = setup_integration
    real_tracker = next(v for v in hass.data[DOMAIN].values() if isinstance(v, LastWriteTracker))

    # A pre-existing record, as if written before this process (and its
    # listener) ever existed - two real writes, so `confirmed` holds an
    # actual claim (not None). A record with confirmed=None is already
    # unconditionally lenient regardless of context (see
    # externally_set()'s own "unconfirmed first attempt" fallback), so
    # seeding only one write here would pass this test even with the
    # recovery fix disabled entirely - proving nothing.
    await real_tracker.async_record(["light.a"], {"light.a": "pre-existing-context"}, "ctx-1", "automation.room")
    await real_tracker.async_record(["light.a"], {"light.a": "ctx-1"}, "ctx-2", "automation.room")

    _set_light(hass, "light.a", "unavailable", supported_color_modes=["color_temp"])
    await hass.async_block_till_done()

    # It recovers - a fresh context nothing here ever issued, exactly
    # what a real device's own post-restart state report looks like.
    recovery_context = Context()
    _set_light(
        hass,
        "light.a",
        "on",
        supported_color_modes=["color_temp"],
        brightness=180,
        color_temp_kelvin=3200,
        context=recovery_context,
    )
    await hass.async_block_till_done()

    # A plain, non-forced call from the same owner manages it
    # immediately - not permanently excluded, and not needing a second
    # restart or a force to recover.
    result = await hass.services.async_call(
        DOMAIN,
        "compute_lighting_groups",
        {
            "entities": ["light.a"],
            "brightness": 200,
            "color_temp_kelvin": 3200,
            "owner_id": "automation.room",
        },
        blocking=True,
        return_response=True,
    )
    assert result["groups"][0]["combined"] == ["light.a"]


async def test_resync_leaves_a_genuinely_unavailable_light_untouched(hass: HomeAssistant):
    """A light still unavailable at resync time (a real drop the restart
    itself doesn't fix) has nothing live to snapshot - left alone for
    the clear-on-unavailable listener to handle once it does report back
    in, not given a bogus confirmed claim.

    Deliberately doesn't use the setup_integration fixture: its own
    write_tracker already has async_start_listening() live, which would
    itself clear this record the moment light.a below goes unavailable -
    exactly correct real behaviour, but it would mean this test was only
    proving the *listener* works (already covered by
    test_write_tracking_record_is_cleared_when_light_goes_unavailable
    above), not resync_to_live_state's own "nothing to snapshot" branch
    in isolation. A bare, unconnected LastWriteTracker keeps the two
    mechanisms properly separated."""
    tracker = LastWriteTracker(hass)
    await tracker.async_load()
    await tracker.async_record(["light.a"], {"light.a": None}, "ctx-1", "automation.room")

    _set_light(hass, "light.a", "unavailable", supported_color_modes=["color_temp"])

    tracker_after_load = LastWriteTracker(hass)
    await tracker_after_load.async_load()
    await tracker_after_load.async_resync_to_live_state(hass)

    # Untouched - still exactly the pending claim just recorded, not
    # None (which would mean it was wrongly cleared) and not a bogus
    # confirmed claim manufactured from the "unavailable" state.
    assert tracker_after_load.confirmed_context_id("light.a") is None
    assert tracker_after_load.pending_context_id("light.a") == "ctx-1"


async def test_resync_preserves_the_pending_claim(setup_integration: HomeAssistant):
    """Only `confirmed` is replaced - a stale pre-restart `pending`
    claim can never match a fresh post-restart context anyway, so
    there's nothing to fix there; it's simply inert until the next real
    write overwrites it."""
    hass = setup_integration
    async_mock_service(hass, "light", "turn_on")

    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 180,
            "color_temp_kelvin": 3200,
            "transition": 2,
            "owner_id": "automation.room",
        },
        blocking=True,
    )
    tracker_before = LastWriteTracker(hass)
    await tracker_before.async_load()
    original_pending_context = tracker_before.pending_context_id("light.a")
    assert original_pending_context is not None

    _set_light(
        hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=180, color_temp_kelvin=3200
    )

    resynced_tracker = LastWriteTracker(hass)
    await resynced_tracker.async_load()
    await resynced_tracker.async_resync_to_live_state(hass)

    assert resynced_tracker.pending_context_id("light.a") == original_pending_context


async def test_async_load_backfills_last_seen_for_legacy_records_without_it(hass: HomeAssistant):
    """Every record persisted before last_seen existed is missing the
    key entirely - backfilled to *now* on load, not left missing (which
    async_prune_stale treats as "can't judge age, leave alone" - see its
    own docstring) and not backdated (which would let a legitimate,
    currently-relevant record get immediately swept up by the very next
    prune pass, the "mass prune on upgrade" this backfill specifically
    exists to avoid)."""
    from homeassistant.helpers.storage import Store

    from custom_components.adaptive_lighting_helpers.write_tracking import STORAGE_KEY, STORAGE_VERSION

    legacy_store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    await legacy_store.async_save(
        {
            "light.a": {
                "confirmed": {"context_id": "ctx-old", "owner_id": "automation.room"},
                "pending": None,
                # no last_seen key at all - the pre-upgrade shape.
            }
        }
    )

    tracker = LastWriteTracker(hass)
    await tracker.async_load()

    record = tracker.snapshot()["light.a"]
    assert record.get("last_seen") is not None
    assert dt_util.utcnow() - dt_util.parse_datetime(record["last_seen"]) < timedelta(seconds=5)


async def test_prune_stale_removes_a_record_untouched_past_the_cutoff(setup_integration: HomeAssistant):
    hass = setup_integration
    async_mock_service(hass, "light", "turn_on")
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    with freeze_time(dt_util.utcnow()) as frozen:
        await hass.services.async_call(
            DOMAIN,
            "apply_lighting",
            {
                "entities": ["light.a"],
                "brightness": 180,
                "color_temp_kelvin": 3200,
                "transition": 2,
                "owner_id": "automation.room",
            },
            blocking=True,
        )
        tracker = LastWriteTracker(hass)
        await tracker.async_load()
        assert "light.a" in tracker.snapshot()

        frozen.move_to(dt_util.utcnow() + timedelta(days=STALE_RECORD_MAX_AGE_DAYS, hours=1))
        await tracker.async_prune_stale()

    assert "light.a" not in tracker.snapshot()


async def test_prune_stale_leaves_a_recent_record_alone(setup_integration: HomeAssistant):
    """The boundary case - a record still within the cutoff must survive
    a prune pass. Without this, a mutation that pruned everything
    regardless of age would pass test_prune_stale_removes_a_record_untouched_past_the_cutoff
    just as easily as the real implementation."""
    hass = setup_integration
    async_mock_service(hass, "light", "turn_on")
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    with freeze_time(dt_util.utcnow()) as frozen:
        await hass.services.async_call(
            DOMAIN,
            "apply_lighting",
            {
                "entities": ["light.a"],
                "brightness": 180,
                "color_temp_kelvin": 3200,
                "transition": 2,
                "owner_id": "automation.room",
            },
            blocking=True,
        )
        tracker = LastWriteTracker(hass)
        await tracker.async_load()

        frozen.move_to(dt_util.utcnow() + timedelta(hours=1))
        await tracker.async_prune_stale()

    assert "light.a" in tracker.snapshot()


async def test_prune_stale_leaves_a_record_with_no_last_seen_alone(hass: HomeAssistant):
    """Defensive branch: a record whose age genuinely can't be judged
    (shouldn't happen after async_load()'s own backfill, but nothing
    else in this module ever deletes on ambiguity either - see
    async_prune_stale's own docstring) must never be pruned."""
    tracker = LastWriteTracker(hass)
    await tracker.async_load()
    tracker._data["light.a"] = {  # bypassing the public API is deliberate here - this shape shouldn't occur naturally
        "confirmed": {"context_id": "ctx-old", "owner_id": "automation.room", "recorded_at": None, "target": None},
        "pending": None,
        "last_seen": None,
    }

    await tracker.async_prune_stale()

    assert "light.a" in tracker.snapshot()


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
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.recovering", "light.sibling"],
            "brightness": 180,
            "color_temp_kelvin": 3200,
            "transition": 2,
            "owner_id": "automation.room",
        },
        blocking=True,
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
            "owner_id": "automation.room",
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
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "brightness": 180,
            "color_temp_kelvin": 3200,
            "transition": 2,
            "owner_id": "automation.room",
        },
        blocking=True,
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
            "owner_id": "automation.room",
        },
        blocking=True,
        return_response=True,
    )
    assert "light.a" not in result["groups"][0]["combined"]
