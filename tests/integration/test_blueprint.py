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
from freezegun import freeze_time
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
def frozen_time():
    """Every test in this file sets up a real automation carrying a live
    adaptive_tick (time_pattern, every 1 minute - see
    TestAdaptiveScheduleAndTransitions.test_a_flat_curve_still_gets_a_tick_from_the_time_pattern)
    among its other real triggers. Without controlling wall-clock time,
    any test whose setup-plus-assertion window happens to straddle a real
    minute boundary can have that trigger fire for real mid-test - not a
    hypothetical, reproduced locally at roughly 5% failure rate for
    TestRecoveredTrigger.test_a_plain_off_to_on_transition_does_not_fire_it,
    which asserts apply_lighting_calls == [] and has no way to distinguish
    "nothing fired" from "the periodic tick happened to fire too". Freezing
    time file-wide (not just in the handful of tests that explicitly
    advance it) closes this off for every test, not only the one that
    happened to get caught. A test that needs elapsed time to actually
    pass takes this same fixture by name and calls `.tick()`/`.move_to()`
    on it directly, rather than opening its own nested freeze_time (see
    TestSelfHealing) - freezegun's nesting support is real, but there's no
    reason to rely on it when the outer freeze is already right here."""
    with freeze_time(dt_util.utcnow()) as frozen:
        yield frozen


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

    async def test_a_flat_curve_still_gets_a_tick_from_the_time_pattern(self, hass, apply_lighting_calls):
        """Morning and Night are flat stretches of the curve, so the
        sensor re-reports identical state *and* attributes and Home
        Assistant raises state_reported rather than state_changed -
        which a platform: state trigger never sees. Found live: the
        sensor's last_updated sat unmoved for ~56 minutes while
        last_reported kept advancing, and no room logged a single
        adaptive-triggered run in that window. The time_pattern floor is
        what guarantees a tick regardless."""
        _light(hass, "light.a", "on", brightness=190, color_temp_kelvin=4000)
        hass.states.async_set("sensor.test_adaptive", "Morning", {"brightness": 255, "color_temp": 6667})
        await hass.async_block_till_done()
        await _setup_room_automation(hass, room_target={"entity_id": "light.a"})
        apply_lighting_calls.clear()

        # Deliberately no sensor write at all - the flat-curve case is
        # precisely "nothing changes", so anything that fires here can
        # only have come from the periodic trigger.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
        await hass.async_block_till_done()

        assert apply_lighting_calls, "a flat curve must still produce a tick"
        assert apply_lighting_calls[-1].data["entities"] == ["light.a"]

    async def test_the_periodic_tick_does_not_reactivate_a_scene(self, hass, apply_lighting_calls, scene_turn_on_calls):
        """The floor tick carries no phase information to compare, so
        treating it as "recheck due" would re-fire scene.turn_on every
        single minute - exactly what scene_recheck_due exists to stop."""
        _light(hass, "light.a", "on", brightness=190, color_temp_kelvin=4000)
        hass.states.async_set("scene.test_evening", "unknown", {"entity_id": ["light.a"]})
        hass.states.async_set("sensor.test_adaptive", "Evening", {"brightness": 180, "color_temp": 3000})
        await hass.async_block_till_done()
        await _setup_room_automation(
            hass, room_target={"entity_id": "light.a"}, evening_scene="scene.test_evening"
        )
        scene_turn_on_calls.clear()

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
        await hass.async_block_till_done()

        assert scene_turn_on_calls == []

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

    async def test_real_occupancy_sensor_reporting_unoccupied_still_updates_an_already_on_light(
        self, hass, apply_lighting_calls
    ):
        """The core simplification: occupancy only gates turning lights
        *on* and *off* (see TestAllowTurnOn and the motion_off/reconcile
        tests below) - it has no say over whether an already-on light
        keeps tracking the curve. Before this, a room *with* a real
        occupancy sensor stopped updating on-lights the instant it read
        "unoccupied", while a room with no sensor never had that
        restriction at all (its own occupied fallback is just "is
        anything already on"). This test is the case that used to behave
        differently depending on whether a sensor happened to be
        present at all - now both are consistent."""
        _occupancy(hass, "binary_sensor.occ", "off")
        _light(hass, "light.a", "on", brightness=190, color_temp_kelvin=4000)
        await hass.async_block_till_done()

        await _setup_room_automation(hass, room_target={"entity_id": ["light.a", "binary_sensor.occ"]})

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
    and its follow-up bugs and eventual redesign, came from.

    As of the redesign, `recovered` carries no special handling of its
    own at all - no force, no scoped call, no occupancy bypass. Its only
    job is promptness: causing an ordinary tick to run right away
    instead of waiting for the room's next unrelated trigger. The actual
    "a recovered light is free to manage again" guarantee now lives in
    write_tracking.py (LastWriteTracker.async_start_listening, which
    clears an entity's record the moment it's seen going unavailable) -
    tested directly against the real service in
    tests/integration/test_services.py, not here, since apply_lighting
    is mocked in this file and so never actually runs that logic."""

    async def test_fires_and_resyncs_a_light_that_reconnects_on(self, hass, apply_lighting_calls):
        """Regression test for the trigger's original dead-on-arrival bug:
        before the fix, this scenario produced zero automation runs at
        all (confirmed live against Jacob's real pendant) - the
        value_template could never become true because it referenced
        `trigger` inside its own arming evaluation, which is never in
        scope there. No `force` involved any more (see class docstring)
        - it's included because it's on, in a room that's (trivially)
        occupied by virtue of being the only light and now being on."""
        _light(hass, "light.a", "unavailable")
        await hass.async_block_till_done()
        await _setup_room_automation(hass, room_target={"entity_id": "light.a"})

        _light(hass, "light.a", "on", brightness=255)
        await hass.async_block_till_done()

        assert any(c.data["entities"] == ["light.a"] and c.data["force"] is False for c in apply_lighting_calls)

    async def test_does_not_turn_on_a_light_that_reconnects_off_in_a_dark_room(self, hass, apply_lighting_calls):
        """The bug found immediately after fixing the trigger above:
        Jacob's only light, off at bedtime, turned on purely from a
        network blip. A light reconnecting *off*, in a room with
        nothing else on, must stay off - the same allow_turn_on rule
        every other light in every other room is subject to, not
        anything specific to recovery."""
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
        unavailable" aggregate at all, so it must not cause the
        automation to run at all (nothing else in this room's trigger
        list reacts to a plain light state change either)."""
        _light(hass, "light.a", "off")
        await hass.async_block_till_done()
        await _setup_room_automation(hass, room_target={"entity_id": "light.a"})

        _light(hass, "light.a", "on", brightness=255)
        await hass.async_block_till_done()

        assert apply_lighting_calls == []

    async def test_recovery_joins_the_ordinary_room_wide_tick_not_a_separate_call(self, hass, apply_lighting_calls):
        """No more dedicated scoped call (see class docstring) - a
        recovered light is just part of the same single apply_lighting
        call every other trigger already makes, alongside whatever else
        in the room happens to be on.

        Whole room goes dark and comes back, which is what the aggregate
        actually arms on."""
        _light(hass, "light.recovering", "unavailable")
        _light(hass, "light.sibling", "unavailable")
        await hass.async_block_till_done()
        await _setup_room_automation(hass, room_target={"entity_id": ["light.recovering", "light.sibling"]})

        _light(hass, "light.recovering", "on", brightness=255)
        _light(hass, "light.sibling", "on", brightness=90, color_temp_kelvin=4000)
        await hass.async_block_till_done()

        assert apply_lighting_calls
        assert set(apply_lighting_calls[-1].data["entities"]) == {"light.recovering", "light.sibling"}
        assert apply_lighting_calls[-1].data["force"] is False

    async def test_an_orphaned_permanently_unavailable_entity_does_not_veto_recovery(
        self, hass, apply_lighting_calls
    ):
        """Found live in the Ensuite. light.ensuite_spots was a Zigbee
        group that no longer existed, so its entity sat unavailable
        forever - and under the old "none of our lights are
        unavailable" aggregate that held the trigger false permanently,
        silently disabling recovery for the entire room. The aggregate
        now asks whether *anything* is reachable, so a dead entity is
        just one more member of the dark set rather than a veto."""
        _light(hass, "light.orphan", "unavailable")  # never comes back
        _light(hass, "light.real", "unavailable")
        await hass.async_block_till_done()
        await _setup_room_automation(hass, room_target={"entity_id": ["light.orphan", "light.real"]})

        _light(hass, "light.real", "on", brightness=255)
        await hass.async_block_till_done()

        assert apply_lighting_calls, "orphaned entity must not block the room from ever recovering"
        assert "light.real" in apply_lighting_calls[-1].data["entities"]

    async def test_one_bulb_recovering_beside_available_siblings_does_not_fire(
        self, hass, apply_lighting_calls
    ):
        """The accepted blind spot of the aggregate shape: a single bulb
        dropping and returning while its siblings stay up never moves
        "is anything reachable", so `recovered` doesn't fire. Left to the
        periodic adaptive_tick to mop up - which is exactly why that
        trigger exists. Asserted so the trade-off is visible rather than
        discovered."""
        _light(hass, "light.flaky", "unavailable")
        _light(hass, "light.steady", "on", brightness=90, color_temp_kelvin=4000)
        await hass.async_block_till_done()
        await _setup_room_automation(hass, room_target={"entity_id": ["light.flaky", "light.steady"]})

        _light(hass, "light.flaky", "on", brightness=255)
        await hass.async_block_till_done()

        assert apply_lighting_calls == []


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

    async def test_a_null_multiplier_light_is_not_turned_off_when_occupancy_clears(
        self, hass, light_turn_off_calls
    ):
        """A null multiplier means "something else owns this light" - so
        unlike a 0 (which means "turn this one off"), the room emptying
        must leave it alone. Caught live: a kitchen strip offloaded to its
        own gradient automation was still being switched off on every
        motion-clear."""
        _occupancy(hass, "binary_sensor.occ", "on")
        _light(hass, "light.a", "on")
        _light(hass, "light.handed_off", "on")
        await hass.async_block_till_done()

        await _setup_room_automation(
            hass,
            room_target={"entity_id": ["light.a", "light.handed_off", "binary_sensor.occ"]},
            brightness_multiplier_template="{{ {'light.handed_off': None} }}",
            no_motion_wait=0,
        )

        _occupancy(hass, "binary_sensor.occ", "off")
        await hass.async_block_till_done()

        assert light_turn_off_calls, "motion_off should still turn off the lights it does own"
        turned_off = light_turn_off_calls[-1].data["entity_id"]
        assert "light.a" in turned_off
        assert "light.handed_off" not in turned_off

    async def test_a_zero_multiplier_light_is_still_turned_off_when_occupancy_clears(
        self, hass, light_turn_off_calls
    ):
        """The other half of the distinction - 0 is not null. In Jinja, as
        in Python, `0 == false`, so a naive membership test would wrongly
        treat every 0 as handed-off too."""
        _occupancy(hass, "binary_sensor.occ", "on")
        _light(hass, "light.a", "on")
        _light(hass, "light.dimmed_out", "on")
        await hass.async_block_till_done()

        await _setup_room_automation(
            hass,
            room_target={"entity_id": ["light.a", "light.dimmed_out", "binary_sensor.occ"]},
            brightness_multiplier_template="{{ {'light.dimmed_out': 0} }}",
            no_motion_wait=0,
        )

        _occupancy(hass, "binary_sensor.occ", "off")
        await hass.async_block_till_done()

        assert light_turn_off_calls
        turned_off = light_turn_off_calls[-1].data["entity_id"]
        assert "light.a" in turned_off
        assert "light.dimmed_out" in turned_off

    async def test_reconcile_does_not_retry_turning_off_a_null_multiplier_light(
        self, hass, light_turn_off_calls
    ):
        """The second turn-off path. With the handed-off light the only
        thing still lit, reconcile should find nothing to do at all rather
        than switching it off every interval."""
        _occupancy(hass, "binary_sensor.occ", "off")
        _light(hass, "light.a", "off")
        _light(hass, "light.handed_off", "on")
        await hass.async_block_till_done()

        await _setup_room_automation(
            hass,
            room_target={"entity_id": ["light.a", "light.handed_off", "binary_sensor.occ"]},
            brightness_multiplier_template="{{ {'light.handed_off': None} }}",
            reconcile_interval="/5",
        )

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
        await hass.async_block_till_done()

        assert light_turn_off_calls == []


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
        self, hass, light_turn_off_calls, frozen_time
    ):
        """reconcile's own condition is a hand-written template
        comparing `now() - states[e].last_changed` against Wait time
        (no_motion_wait), not occupancy.is_not_detected's native `for:`
        option - see the blueprint's own comment on that condition for
        why (recorder/history-priming dependency, unreliable right
        after a restart). That means real elapsed time has to pass
        between clearing occupancy and the reconcile tick for this to
        pass honestly - `async_fire_time_changed` only fires the event
        that trips the time_pattern trigger, it does not advance
        `now()` itself. Time is frozen file-wide (see the `frozen_time`
        fixture) and explicitly ticked forward here so `now()` inside
        the template genuinely reflects the passed Wait time, rather
        than the real (near-zero) wall-clock gap between the two
        calls."""
        _light(hass, "light.a", "on")
        await hass.async_block_till_done()
        await _setup_room_automation(
            hass, room_target={"entity_id": ["light.a", "binary_sensor.occ"]}, reconcile_interval="/5"
        )
        _occupancy(hass, "binary_sensor.occ", "off")
        await hass.async_block_till_done()

        frozen_time.tick(timedelta(minutes=6))
        async_fire_time_changed(hass, dt_util.utcnow())
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

    async def test_reconcile_ignores_a_momentary_occupancy_blip_shorter_than_wait_time(
        self, hass, light_turn_off_calls, frozen_time
    ):
        """Live incident, 2026-08-21: a noisy-but-genuinely-occupied
        room's occupancy sensor flapped off for a few seconds at a time,
        continuously, and reconcile's own bare "not occupancy.is_detected"
        check - unlike motion_off's trigger, which has its own `for:`
        clause - had no debounce of its own, so a reconcile tick landing
        on one of those brief gaps turned the light off immediately,
        bypassing Wait time (no_motion_wait) entirely. Reproduced here by
        clearing occupancy only 30s before a reconcile boundary, with
        Wait time set well above that."""
        _light(hass, "light.a", "on")
        await hass.async_block_till_done()
        await _setup_room_automation(
            hass,
            room_target={"entity_id": ["light.a", "binary_sensor.occ"]},
            reconcile_interval="/5",
            no_motion_wait=120,
        )

        now = dt_util.utcnow()
        next_boundary = now.replace(second=0, microsecond=0) + timedelta(
            minutes=5 - (now.minute % 5), seconds=0
        )

        frozen_time.move_to(next_boundary - timedelta(seconds=30))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        _occupancy(hass, "binary_sensor.occ", "off")
        await hass.async_block_till_done()

        # Occupancy has only been clear for ~30s here - well under the
        # 120s Wait time - when reconcile's own tick fires.
        frozen_time.move_to(next_boundary + timedelta(seconds=1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert light_turn_off_calls == []
