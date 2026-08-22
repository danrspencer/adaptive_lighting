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

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.adaptive_lighting_helpers import _build_lookup, async_setup_entry
from custom_components.adaptive_lighting_helpers.grouping import build_groups
from custom_components.adaptive_lighting_helpers.write_tracking import LastWriteTracker

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


def _set_sensor(
    hass: HomeAssistant,
    entity_id: str = "sensor.test_adaptive",
    brightness: int = 200,
    color_temp: int = 4000,
    rgb_color: list | None = None,
) -> None:
    attrs = {"brightness": brightness, "color_temp": color_temp}
    if rgb_color is not None:
        attrs["rgb_color"] = rgb_color
    hass.states.async_set(entity_id, "unknown", attrs)


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
            "sensor_brightness": 200,
            "sensor_color_temp_kelvin": 4000,
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
    _set_sensor(hass, brightness=180, color_temp=3200)

    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "sensor_entity_id": "sensor.test_adaptive",
            "transition": 2,
        },
        blocking=True,
    )

    assert len(turn_on_calls) == 1
    assert turn_on_calls[0].data["entity_id"] == ["light.a"]
    assert turn_on_calls[0].data["brightness"] == 180
    assert turn_on_calls[0].data["color_temp_kelvin"] == 3200


async def test_apply_lighting_missing_sensor_attribute_raises(setup_integration: HomeAssistant):
    """A sensor with no brightness/color_temp attribute must raise, not
    silently dim everything to brightness 1 - see _read_sensor_targets'
    own docstring for the incident this guards against."""
    hass = setup_integration
    hass.states.async_set("sensor.broken", "unknown", {})
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "apply_lighting",
            {
                "entities": ["light.a"],
                "sensor_entity_id": "sensor.broken",
                "transition": 2,
            },
            blocking=True,
        )


async def test_apply_lighting_unknown_sensor_raises(setup_integration: HomeAssistant):
    hass = setup_integration
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "apply_lighting",
            {
                "entities": ["light.a"],
                "sensor_entity_id": "sensor.does_not_exist",
                "transition": 2,
            },
            blocking=True,
        )


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
    _set_sensor(hass, brightness=180, color_temp=3200)

    our_context = Context()
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "sensor_entity_id": "sensor.test_adaptive",
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
            "sensor_entity_id": "sensor.test_adaptive",
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
    _set_sensor(hass, brightness=180, color_temp=3200)
    first_context = Context()
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "sensor_entity_id": "sensor.test_adaptive",
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

    _set_sensor(hass, brightness=200, color_temp=3000)
    second_context = Context()
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "sensor_entity_id": "sensor.test_adaptive",
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
            "sensor_entity_id": "sensor.test_adaptive",
            "transition": 2,
            "owner_id": "automation.test_room",
        },
        blocking=True,
    )
    assert len(turn_on_calls) == 2

    # The curve moves on to a genuinely different value - the real test:
    # a light that's actually excluded would stay excluded here too.
    _set_sensor(hass, brightness=100, color_temp=4500)
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "sensor_entity_id": "sensor.test_adaptive",
            "transition": 2,
            "owner_id": "automation.test_room",
        },
        blocking=True,
    )
    assert len(turn_on_calls) == 3


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
    _set_sensor(hass, brightness=180, color_temp=3200)

    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "sensor_entity_id": "sensor.test_adaptive",
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
            "sensor_entity_id": "sensor.test_adaptive",
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
    _set_sensor(hass, brightness=180, color_temp=3200)

    # A light with an existing, unrelated write record (as if a
    # different owner had claimed it).
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=90, color_temp_kelvin=3200)
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "sensor_entity_id": "sensor.test_adaptive",
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
            "sensor_entity_id": "sensor.test_adaptive",
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
            "sensor_brightness": 180,
            "sensor_color_temp_kelvin": 3200,
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
    _set_sensor(hass, brightness=180, color_temp=3200)

    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "sensor_entity_id": "sensor.test_adaptive",
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
            "sensor_entity_id": "sensor.test_adaptive",
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
    _set_sensor(hass, brightness=180, color_temp=3200)

    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "sensor_entity_id": "sensor.test_adaptive",
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
    _set_sensor(hass, brightness=180, color_temp=3200)

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
            "sensor_brightness": 200,
            "sensor_color_temp_kelvin": 3200,
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
    _set_sensor(hass, brightness=180, color_temp=3200)

    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "sensor_entity_id": "sensor.test_adaptive",
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


async def test_recovered_light_is_freed_while_an_unrelated_override_stays_protected(
    setup_integration: HomeAssistant,
):
    """The clear-on-unavailable fix only touches the entity that
    actually went unavailable - a sibling under its own real, unrelated
    override (never went unavailable, so its record is never cleared)
    must stay protected exactly as before."""
    hass = setup_integration
    turn_on_calls = async_mock_service(hass, "light", "turn_on")
    _set_sensor(hass, brightness=180, color_temp=3200)

    for entity_id in ("light.recovering", "light.sibling"):
        _set_light(hass, entity_id, "off", supported_color_modes=["color_temp"])

    our_context = Context()
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.recovering", "light.sibling"],
            "sensor_entity_id": "sensor.test_adaptive",
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
            "sensor_brightness": 180,
            "sensor_color_temp_kelvin": 3200,
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
    _set_sensor(hass, brightness=180, color_temp=3200)

    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    our_context = Context()
    await hass.services.async_call(
        DOMAIN,
        "apply_lighting",
        {
            "entities": ["light.a"],
            "sensor_entity_id": "sensor.test_adaptive",
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
            "sensor_brightness": 180,
            "sensor_color_temp_kelvin": 3200,
            "owner_id": "automation.room",
        },
        blocking=True,
        return_response=True,
    )
    assert "light.a" not in result["groups"][0]["combined"]
