"""
Integration tests for blueprints/automation/danspencer/adaptive_lighting.yaml
itself - through a real Home Assistant automation/blueprint/template-
trigger engine, not just YAML parsing or isolated template snippets.

This is the only place that can catch bugs living in the blueprint's own
trigger/condition/action wiring - both real incidents this suite exists
to guard against were exactly that kind of bug, invisible to
tests/test_grouping.py and tests/test_curve.py (pure logic, no HA at
all) or to a plain YAML/template sanity check (syntactically fine,
wrong at runtime):

1. The `recovered` trigger's value_template referenced `trigger.*`
   inside itself, which is never in scope during a template trigger's
   own arming evaluation - it could never fire, in any room, from the
   moment it shipped.
2. Once fixed, `apply_lighting` was found to turn on any off light
   whenever *any* non-motion/non-manual tick ran (adaptive, extra, or
   the now-working recovered) - never caught before because recovered
   never ran at all.

Organised to mirror docs/BLUEPRINT.md's own section headings, one test
class per feature - read top to bottom, this file is meant to double as
a spec of what the blueprint actually does, not just a regression net
for the two incidents above.

Doesn't re-prove grouping.py's own tolerance/two-step/RGB logic (see
tests/test_grouping.py) or curve.py's brightness/Kelvin math (see
tests/test_curve.py) - `adaptive_lighting_helpers.apply_lighting` is
mocked here via async_mock_service, so these tests are entirely about
what the blueprint decides to call it *with*, for a given trigger and
room state.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed, async_mock_service

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

BLUEPRINT_PATH = "danspencer/adaptive_lighting.yaml"


async def _setup_room_automation(
    hass: HomeAssistant,
    *,
    room_target: dict,
    entity_id: str = "automation.room",
    alias: str = "room",
    **extra_inputs,
):
    input_ = {
        "adaptive_sensor": "sensor.test_adaptive",
        "room_target": room_target,
        **extra_inputs,
    }
    assert await async_setup_component(
        hass,
        "automation",
        {"automation": [{"alias": alias, "use_blueprint": {"path": BLUEPRINT_PATH, "input": input_}}]},
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id) is not None, f"automation.{entity_id} failed to set up from the blueprint"


def _light(hass: HomeAssistant, entity_id: str, state: str, **attrs) -> None:
    hass.states.async_set(entity_id, state, {"supported_color_modes": ["color_temp"], **attrs})


def _occupancy(hass: HomeAssistant, entity_id: str, state: str) -> None:
    hass.states.async_set(entity_id, state, {"device_class": "occupancy"})


@pytest.fixture
def apply_lighting_calls(hass: HomeAssistant):
    return async_mock_service(hass, "adaptive_lighting_helpers", "apply_lighting")


@pytest.fixture
def scene_turn_on_calls(hass: HomeAssistant):
    return async_mock_service(hass, "scene", "turn_on")


@pytest.fixture
def light_turn_off_calls(hass: HomeAssistant):
    return async_mock_service(hass, "light", "turn_off")


@pytest.fixture(autouse=True)
def _sensor(hass: HomeAssistant):
    """The adaptive sensor every test automation points at - a plain
    state, no real adaptive_lighting_helpers entity needed since the
    blueprint only ever reads its state (for phase/rgb_phases) and
    passes its entity_id straight through to apply_lighting, which is
    mocked in these tests. Phase defaults to "Day" - outside the
    default rgb_phases (Evening/Night), so prefer_rgb_color defaults to
    False unless a test overrides either."""
    hass.states.async_set("sensor.test_adaptive", "Day", {"brightness": 200, "color_temp": 4000})


@pytest.fixture(autouse=True)
def expected_lingering_timers():
    """Every test here sets up a real automation with a live `reconcile`
    time_pattern trigger (default every 5 minutes) - that timer is
    still correctly scheduled when the test ends, since nothing tears
    the automation down first. Overrides pytest-homeassistant-custom-
    component's own default (False), which otherwise fails every test
    in this file at teardown for exactly that expected timer, not an
    actual leak."""
    return True


class TestAdaptiveScheduleAndTransitions:
    """docs/BLUEPRINT.md#brightness--colour-temperature-schedule"""

    async def test_periodic_adaptive_tick_updates_an_already_on_light(self, hass, apply_lighting_calls):
        _light(hass, "light.a", "on", brightness=190, color_temp_kelvin=4000)
        await hass.async_block_till_done()
        await _setup_room_automation(hass, room_target={"entity_id": "light.a"})

        hass.states.async_set("sensor.test_adaptive", "Day", {"brightness": 210, "color_temp": 4000})
        await hass.async_block_till_done()

        calls = apply_lighting_calls
        assert calls and calls[-1].data["entities"] == ["light.a"]

    async def test_adaptive_tick_uses_the_adaptive_transition_duration(self, hass, apply_lighting_calls):
        _light(hass, "light.a", "on")
        await hass.async_block_till_done()
        await _setup_room_automation(
            hass, room_target={"entity_id": "light.a"}, adaptive_transition=45, motion_on_transition=2
        )

        hass.states.async_set("sensor.test_adaptive", "Day", {"brightness": 210, "color_temp": 4000})
        await hass.async_block_till_done()

        calls = apply_lighting_calls
        assert calls[-1].data["transition"] == 45

    async def test_motion_on_uses_the_motion_on_transition_duration(self, hass, apply_lighting_calls):
        _occupancy(hass, "binary_sensor.occ", "off")
        _light(hass, "light.a", "off")
        await hass.async_block_till_done()
        await _setup_room_automation(
            hass,
            room_target={"entity_id": ["light.a", "binary_sensor.occ"]},
            adaptive_transition=45,
            motion_on_transition=2,
        )

        _occupancy(hass, "binary_sensor.occ", "on")
        await hass.async_block_till_done()

        calls = apply_lighting_calls
        assert calls[-1].data["transition"] == 2


class TestRoomTargetResolution:
    """docs/BLUEPRINT.md#one-target-two-jobs - room_target does double
    duty: lights within it are controlled, occupancy-class sensors
    within it govern occupancy. Entity-list resolution is covered
    throughout the rest of this file; this class is specifically about
    resolving via an area."""

    async def test_area_id_room_target_resolves_both_lights_and_occupancy_sensors(
        self, hass, apply_lighting_calls
    ):
        area = ar.async_get(hass).async_get_or_create("test_room")
        entry = MockConfigEntry(domain="test")
        entry.add_to_hass(hass)
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={("test", "light_device")}
        )
        dr.async_get(hass).async_update_device(device.id, area_id=area.id)
        ent_reg = er.async_get(hass)
        ent_reg.async_get_or_create(
            "light", "test", "light_a", suggested_object_id="a", device_id=device.id
        )
        ent_reg.async_update_entity("light.a", area_id=area.id)
        ent_reg.async_get_or_create("binary_sensor", "test", "occ_a", suggested_object_id="occ")
        ent_reg.async_update_entity("binary_sensor.occ", area_id=area.id)

        _light(hass, "light.a", "off")
        _occupancy(hass, "binary_sensor.occ", "off")
        await hass.async_block_till_done()

        await _setup_room_automation(hass, room_target={"area_id": area.id})

        _occupancy(hass, "binary_sensor.occ", "on")
        await hass.async_block_till_done()

        # Occupancy detected in the area turned on/updated light.a - proves
        # both halves of room_target's resolution worked through an area.
        calls = apply_lighting_calls
        assert calls and calls[-1].data["entities"] == ["light.a"]


class TestOccupancyDrivenOnOff:
    """docs/BLUEPRINT.md#occupancy-driven-onoff"""

    async def test_occupancy_detected_turns_on_off_lights_in_the_room(self, hass, apply_lighting_calls):
        _occupancy(hass, "binary_sensor.occ", "off")
        _light(hass, "light.a", "off")
        await hass.async_block_till_done()

        await _setup_room_automation(hass, room_target={"entity_id": ["light.a", "binary_sensor.occ"]})

        _occupancy(hass, "binary_sensor.occ", "on")
        await hass.async_block_till_done()

        calls = apply_lighting_calls
        assert calls and calls[-1].data["entities"] == ["light.a"]

    async def test_occupancy_cleared_turns_lights_off_after_the_wait(self, hass, light_turn_off_calls):
        _occupancy(hass, "binary_sensor.occ", "on")
        _light(hass, "light.a", "on")
        await hass.async_block_till_done()

        await _setup_room_automation(
            hass, room_target={"entity_id": ["light.a", "binary_sensor.occ"]}, no_motion_wait=0
        )

        _occupancy(hass, "binary_sensor.occ", "off")
        await hass.async_block_till_done()
        # The `for:` duration is scheduled via call_later even at 0s - let
        # it actually elapse rather than assuming synchronous firing.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
        await hass.async_block_till_done()

        assert light_turn_off_calls and light_turn_off_calls[-1].data["entity_id"] == ["light.a"]

    async def test_occupancy_cleared_does_not_turn_off_lights_while_a_second_sensor_is_still_on(
        self, hass, light_turn_off_calls
    ):
        """Regression test for the nightlight-override incident (see
        CLAUDE.md's dated note on automation.bedroom_hall_lights):
        occupancy.cleared fires per-entity, not per-target - with two
        occupancy-class sensors in room_target, this trigger fires the
        instant EITHER goes from on to off, even while the other still
        reports occupied. Must not turn the lights off in that case."""
        _occupancy(hass, "binary_sensor.motion", "on")
        _occupancy(hass, "binary_sensor.nightlight_override", "on")
        _light(hass, "light.a", "on")
        await hass.async_block_till_done()

        await _setup_room_automation(
            hass,
            room_target={"entity_id": ["light.a", "binary_sensor.motion", "binary_sensor.nightlight_override"]},
            no_motion_wait=0,
        )

        # The real motion sensor clears, but the override sensor is still on.
        _occupancy(hass, "binary_sensor.motion", "off")
        await hass.async_block_till_done()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
        await hass.async_block_till_done()

        assert light_turn_off_calls == []

    async def test_no_occupancy_sensor_still_updates_an_already_on_light(self, hass, apply_lighting_calls):
        """Occupancy is entirely optional - a room with no occupancy-class
        sensor in it still keeps already-on lights updated via the
        `occupied` fallback (at least one light already on counts as "in
        use"), it just can't turn anything on by itself (see
        TestAllowTurnOn below)."""
        _light(hass, "light.a", "on", brightness=190, color_temp_kelvin=4000)
        await hass.async_block_till_done()
        await _setup_room_automation(hass, room_target={"entity_id": "light.a"})

        hass.states.async_set("sensor.test_adaptive", "Day", {"brightness": 210, "color_temp": 4000})
        await hass.async_block_till_done()

        calls = apply_lighting_calls
        assert calls and calls[-1].data["entities"] == ["light.a"]

    async def test_fully_dark_unoccupied_room_periodic_tick_does_not_turn_anything_on(
        self, hass, apply_lighting_calls
    ):
        _light(hass, "light.a", "off")
        await hass.async_block_till_done()
        await _setup_room_automation(hass, room_target={"entity_id": "light.a"})

        hass.states.async_set("sensor.test_adaptive", "Day", {"brightness": 210, "color_temp": 4000})
        await hass.async_block_till_done()

        for call in apply_lighting_calls:
            assert "light.a" not in call.data["entities"]


class TestAllowTurnOn:
    """The general policy (see the blueprint's own allow_turn_on comment):
    only motion, a manual run, or the room already being occupied (any
    of its lights already on) may bring a light on. Everything else may
    only update lights that are already on."""

    async def test_manual_run_forces_the_tick_and_turns_on_off_lights(self, hass, apply_lighting_calls):
        _light(hass, "light.a", "off")
        await hass.async_block_till_done()
        await _setup_room_automation(hass, room_target={"entity_id": "light.a"})

        await hass.services.async_call("automation", "trigger", {"entity_id": "automation.room"}, blocking=True)
        await hass.async_block_till_done()

        calls = apply_lighting_calls
        assert calls and calls[-1].data["entities"] == ["light.a"]
        assert calls[-1].data["force"] is True

    async def test_occupied_room_lets_a_periodic_tick_turn_on_a_different_off_light(
        self, hass, apply_lighting_calls
    ):
        _occupancy(hass, "binary_sensor.occ", "on")
        _light(hass, "light.on_light", "on", brightness=200, color_temp_kelvin=4000)
        _light(hass, "light.off_light", "off")
        await hass.async_block_till_done()

        await _setup_room_automation(
            hass, room_target={"entity_id": ["light.on_light", "light.off_light", "binary_sensor.occ"]}
        )

        hass.states.async_set("sensor.test_adaptive", "Day", {"brightness": 210, "color_temp": 4000})
        await hass.async_block_till_done()

        calls = apply_lighting_calls
        assert calls and set(calls[-1].data["entities"]) == {"light.on_light", "light.off_light"}


class TestOverrideDetection:
    """docs/BLUEPRINT.md#override-detection"""

    async def test_apply_lighting_is_called_with_this_automations_owner_id(self, hass, apply_lighting_calls):
        """The mechanism itself (context.id/owner_id comparison) is
        grouping.py's job and already thoroughly tested in
        tests/test_grouping.py and tests/integration/test_services.py -
        this only confirms the blueprint wires its own identity through,
        which is what makes the whole thing work per room."""
        _light(hass, "light.a", "on")
        await hass.async_block_till_done()
        await _setup_room_automation(hass, room_target={"entity_id": "light.a"})

        hass.states.async_set("sensor.test_adaptive", "Day", {"brightness": 210, "color_temp": 4000})
        await hass.async_block_till_done()

        calls = apply_lighting_calls
        assert calls and calls[-1].data["owner_id"] == "automation.room"
        assert calls[-1].data["force"] is False


class TestRecoveredTrigger:
    """docs/BLUEPRINT.md's "A device regaining power after an outage"
    section - see also the dated CLAUDE.md incident this whole feature,
    and its two follow-up bugs, came from."""

    async def test_fires_and_resyncs_a_light_that_reconnects_on(self, hass, apply_lighting_calls):
        """Regression test for the trigger's original dead-on-arrival bug:
        before the fix, this scenario produced zero automation runs at
        all (confirmed live against Jacob's real pendant) - the
        value_template could never become true because it referenced
        `trigger` inside its own arming evaluation, which is never in
        scope there."""
        _light(hass, "light.a", "unavailable")
        await hass.async_block_till_done()
        await _setup_room_automation(hass, room_target={"entity_id": "light.a"})

        _light(hass, "light.a", "on", brightness=255)
        await hass.async_block_till_done()

        forced_calls = [c for c in apply_lighting_calls if c.data.get("force")]
        assert any(c.data["entities"] == ["light.a"] for c in forced_calls)

    async def test_does_not_turn_on_a_light_that_reconnects_off_in_a_dark_room(self, hass, apply_lighting_calls):
        """The bug found immediately after fixing the trigger above:
        Jacob's only light, off at bedtime, turned on purely from a
        network blip. A light reconnecting *off*, in a room with
        nothing else on, must stay off."""
        _light(hass, "light.a", "unavailable")
        await hass.async_block_till_done()
        await _setup_room_automation(hass, room_target={"entity_id": "light.a"})

        _light(hass, "light.a", "off")
        await hass.async_block_till_done()

        for call in apply_lighting_calls:
            assert "light.a" not in call.data["entities"]

    async def test_a_plain_off_to_on_transition_does_not_fire_it(self, hass, apply_lighting_calls):
        """recovered is specifically for unavailable/unknown -> real
        state - an ordinary off -> on never touches the "is anything
        unavailable" aggregate at all, so it must not produce a forced,
        unconditional resync."""
        _light(hass, "light.a", "off")
        await hass.async_block_till_done()
        await _setup_room_automation(hass, room_target={"entity_id": "light.a"})

        _light(hass, "light.a", "on", brightness=255)
        await hass.async_block_till_done()

        assert not any(c.data.get("force") for c in apply_lighting_calls)

    async def test_only_resyncs_the_light_that_recovered_not_the_whole_room(self, hass, apply_lighting_calls):
        """Deliberately scoped to trigger.entity_id alone (see the
        blueprint's own comment on the force-resync step) - a genuinely
        different light in the same room, under its own real manual
        override, must not be clobbered just because something else
        nearby blipped."""
        _light(hass, "light.recovering", "unavailable")
        _light(hass, "light.sibling", "on", brightness=90, color_temp_kelvin=4000)
        await hass.async_block_till_done()
        await _setup_room_automation(hass, room_target={"entity_id": ["light.recovering", "light.sibling"]})

        _light(hass, "light.recovering", "on", brightness=255)
        await hass.async_block_till_done()

        forced_calls = [c for c in apply_lighting_calls if c.data.get("force")]
        assert all(c.data["entities"] == ["light.recovering"] for c in forced_calls)
        assert not any("light.sibling" in c.data["entities"] for c in forced_calls)


class TestSceneHandoff:
    """docs/BLUEPRINT.md#scene-handoff"""

    async def test_valid_scene_activates_via_a_phase_change_and_adaptive_lighting_only_covers_uncovered_entities(
        self, hass, apply_lighting_calls, scene_turn_on_calls
    ):
        """Regression test for a real bug: an earlier version suppressed
        the *entire* adaptive tick whenever trigger.id == 'adaptive' and
        a scene was already active - which also blocked the very tick
        meant to activate a phase-picked scene in the first place,
        whenever the room stayed continuously occupied across the phase
        boundary (no fresh motion to fall back on). Triggered here via a
        genuine phase change on the adaptive sensor - the actual
        real-world trigger for this feature - not a manual run."""
        _light(hass, "light.covered", "on")
        _light(hass, "light.uncovered", "on")
        hass.states.async_set("scene.evening_scene", "2024-01-01T00:00:00+00:00", {"entity_id": ["light.covered"]})
        await hass.async_block_till_done()

        await _setup_room_automation(
            hass,
            room_target={"entity_id": ["light.covered", "light.uncovered"]},
            evening_scene="scene.evening_scene",
        )

        # A real phase change (Day -> Evening), not just an attribute
        # tick - this is what actually happens at the phase boundary.
        hass.states.async_set("sensor.test_adaptive", "Evening", {"brightness": 150, "color_temp": 3000})
        await hass.async_block_till_done()

        assert scene_turn_on_calls and scene_turn_on_calls[-1].data["entity_id"] == ["scene.evening_scene"]
        calls = apply_lighting_calls
        assert calls and calls[-1].data["entities"] == ["light.uncovered"]

    async def test_scene_recheck_is_skipped_on_a_same_phase_attribute_only_tick(
        self, hass, apply_lighting_calls, scene_turn_on_calls
    ):
        """Once the scene's been activated for the current phase, a
        subsequent tick that's purely a brightness/colour-temp change
        (same phase, no real transition - the common case, since the
        sensor ticks roughly once a minute) shouldn't re-call
        scene.turn_on - scenes carry none of apply_lighting's own
        tolerance/override protections, so doing this every minute would
        silently stomp any manual change to the scene's own lights."""
        _light(hass, "light.covered", "on")
        hass.states.async_set("scene.evening_scene", "2024-01-01T00:00:00+00:00", {"entity_id": ["light.covered"]})
        await hass.async_block_till_done()

        await _setup_room_automation(
            hass, room_target={"entity_id": "light.covered"}, evening_scene="scene.evening_scene"
        )

        hass.states.async_set("sensor.test_adaptive", "Evening", {"brightness": 150, "color_temp": 3000})
        await hass.async_block_till_done()
        assert len(scene_turn_on_calls) == 1

        # Same phase, only brightness ticked - no real transition.
        hass.states.async_set("sensor.test_adaptive", "Evening", {"brightness": 140, "color_temp": 3000})
        await hass.async_block_till_done()

        assert len(scene_turn_on_calls) == 1

    async def test_scene_reaching_outside_scope_is_treated_as_invalid(
        self, hass, apply_lighting_calls, scene_turn_on_calls
    ):
        _light(hass, "light.a", "on")
        hass.states.async_set(
            "scene.bad_scene", "2024-01-01T00:00:00+00:00", {"entity_id": ["light.a", "light.not_in_room"]}
        )
        await hass.async_block_till_done()

        await _setup_room_automation(
            hass, room_target={"entity_id": "light.a"}, scene_template="scene.bad_scene"
        )

        hass.states.async_set("sensor.test_adaptive", "Day", {"brightness": 210, "color_temp": 4000})
        await hass.async_block_till_done()

        assert scene_turn_on_calls == []
        calls = apply_lighting_calls
        assert calls and calls[-1].data["entities"] == ["light.a"]

    async def test_phase_scene_is_used_when_the_template_returns_nothing(
        self, hass, apply_lighting_calls, scene_turn_on_calls
    ):
        _light(hass, "light.covered", "on")
        hass.states.async_set("scene.day_scene", "2024-01-01T00:00:00+00:00", {"entity_id": ["light.covered"]})
        # Start outside the phase day_scene is keyed to, so the
        # transition into Day below is a genuine phase change.
        hass.states.async_set("sensor.test_adaptive", "Night", {"brightness": 80, "color_temp": 2700})
        await hass.async_block_till_done()

        await _setup_room_automation(hass, room_target={"entity_id": "light.covered"}, day_scene="scene.day_scene")

        hass.states.async_set("sensor.test_adaptive", "Day", {"brightness": 210, "color_temp": 4000})
        await hass.async_block_till_done()

        assert scene_turn_on_calls and scene_turn_on_calls[-1].data["entity_id"] == ["scene.day_scene"]


class TestBrightnessScaling:
    """docs/BLUEPRINT.md#per-light-brightness-scaling"""

    async def test_phase_exclude_list_sets_a_zero_multiplier_for_that_light(self, hass, apply_lighting_calls):
        _light(hass, "light.a", "on")
        _light(hass, "light.excluded", "on")
        await hass.async_block_till_done()

        await _setup_room_automation(
            hass, room_target={"entity_id": ["light.a", "light.excluded"]}, day_exclude_lights=["light.excluded"]
        )

        hass.states.async_set("sensor.test_adaptive", "Day", {"brightness": 210, "color_temp": 4000})
        await hass.async_block_till_done()

        calls = apply_lighting_calls
        assert calls[-1].data["brightness_multipliers"] == {"light.excluded": 0}

    async def test_brightness_multiplier_template_wins_over_the_phase_exclude_list_on_collision(
        self, hass, apply_lighting_calls
    ):
        _light(hass, "light.a", "on")
        _light(hass, "light.b", "on")
        await hass.async_block_till_done()

        await _setup_room_automation(
            hass,
            room_target={"entity_id": ["light.a", "light.b"]},
            day_exclude_lights=["light.a", "light.b"],
            brightness_multiplier_template="{{ {'light.a': 0.5} }}",
        )

        hass.states.async_set("sensor.test_adaptive", "Day", {"brightness": 210, "color_temp": 4000})
        await hass.async_block_till_done()

        calls = apply_lighting_calls
        # light.a: template's own value (0.5) wins over the phase list's 0.
        # light.b: not in the template, so the phase list's 0 fills in.
        assert calls[-1].data["brightness_multipliers"] == {"light.a": 0.5, "light.b": 0}


class TestRgbColour:
    """docs/BLUEPRINT.md#rgb-colour"""

    async def test_prefer_rgb_color_is_true_only_during_a_configured_phase(self, hass, apply_lighting_calls):
        _light(hass, "light.a", "on")
        await hass.async_block_till_done()
        await _setup_room_automation(hass, room_target={"entity_id": "light.a"}, rgb_phases=["Day"])

        hass.states.async_set("sensor.test_adaptive", "Day", {"brightness": 210, "color_temp": 4000})
        await hass.async_block_till_done()

        calls = apply_lighting_calls
        assert calls[-1].data["prefer_rgb_color"] is True

    async def test_prefer_rgb_color_is_false_outside_configured_phases(self, hass, apply_lighting_calls):
        _light(hass, "light.a", "on")
        await hass.async_block_till_done()
        # Default rgb_phases is [Evening, Night] - current phase is Day.
        await _setup_room_automation(hass, room_target={"entity_id": "light.a"})

        hass.states.async_set("sensor.test_adaptive", "Day", {"brightness": 210, "color_temp": 4000})
        await hass.async_block_till_done()

        calls = apply_lighting_calls
        assert calls[-1].data["prefer_rgb_color"] is False


class TestAdditionalTriggers:
    """docs/BLUEPRINT.md#additional-triggers"""

    async def test_extra_trigger_entity_change_causes_immediate_reevaluation(self, hass, apply_lighting_calls):
        _light(hass, "light.a", "on", brightness=190, color_temp_kelvin=4000)
        hass.states.async_set("binary_sensor.dependency", "off")
        await hass.async_block_till_done()

        await _setup_room_automation(
            hass, room_target={"entity_id": "light.a"}, extra_triggers=["binary_sensor.dependency"]
        )

        hass.states.async_set("binary_sensor.dependency", "on")
        await hass.async_block_till_done()

        calls = apply_lighting_calls
        assert calls and calls[-1].data["entities"] == ["light.a"]


class TestSelfHealing:
    """docs/BLUEPRINT.md#self-healing"""

    async def test_reconcile_retries_turning_off_a_light_left_on_with_no_occupancy(
        self, hass, light_turn_off_calls
    ):
        _occupancy(hass, "binary_sensor.occ", "off")
        _light(hass, "light.a", "on")
        await hass.async_block_till_done()

        await _setup_room_automation(
            hass, room_target={"entity_id": ["light.a", "binary_sensor.occ"]}, reconcile_interval="/5"
        )

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
        await hass.async_block_till_done()

        assert light_turn_off_calls and "light.a" in light_turn_off_calls[-1].data["entity_id"]

    async def test_reconcile_does_nothing_while_occupied(self, hass, light_turn_off_calls):
        _occupancy(hass, "binary_sensor.occ", "on")
        _light(hass, "light.a", "on")
        await hass.async_block_till_done()

        await _setup_room_automation(
            hass, room_target={"entity_id": ["light.a", "binary_sensor.occ"]}, reconcile_interval="/5"
        )

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
        await hass.async_block_till_done()

        assert light_turn_off_calls == []
