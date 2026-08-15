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

Doesn't re-prove grouping.py's own tolerance/two-step/RGB logic (see
tests/test_grouping.py) or curve.py's brightness/Kelvin math (see
tests/test_curve.py) - `adaptive_lighting_helpers.apply_lighting` is
mocked here via async_mock_service, so these tests are entirely about
what the blueprint decides to call it *with*, for a given trigger and
room state.
"""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

BLUEPRINT_PATH = "danspencer/adaptive_lighting.yaml"


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


async def _setup_room_automation(hass: HomeAssistant, *, entity_id: str, room_target: dict, alias: str = "room"):
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "alias": alias,
                    "use_blueprint": {
                        "path": BLUEPRINT_PATH,
                        "input": {
                            "adaptive_sensor": "sensor.test_adaptive",
                            "room_target": room_target,
                        },
                    },
                }
            ]
        },
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id) is not None, f"automation.{entity_id} failed to set up from the blueprint"


@pytest.fixture
def apply_lighting_calls(hass: HomeAssistant):
    return async_mock_service(hass, "adaptive_lighting_helpers", "apply_lighting")


@pytest.fixture(autouse=True)
def _sensor(hass: HomeAssistant):
    """The adaptive sensor every test automation points at - a plain
    state, no real adaptive_lighting_helpers entity needed since the
    blueprint only ever reads its state (for phase/rgb_phases) and
    passes its entity_id straight through to apply_lighting, which is
    mocked in these tests."""
    hass.states.async_set("sensor.test_adaptive", "Day", {"brightness": 200, "color_temp": 4000})


async def test_recovered_trigger_fires_and_resyncs_a_light_that_reconnects_on(
    hass: HomeAssistant, apply_lighting_calls
):
    """Regression test for the trigger's original dead-on-arrival bug:
    before the fix, this scenario produced zero automation runs at all
    (confirmed live against Jacob's real pendant) - the value_template
    could never become true because it referenced `trigger` inside its
    own arming evaluation, which is never in scope there."""
    hass.states.async_set(
        "light.test_light", "unavailable", {"supported_color_modes": ["color_temp"]}
    )
    await hass.async_block_till_done()

    await _setup_room_automation(
        hass, entity_id="automation.room", room_target={"entity_id": "light.test_light"}
    )

    hass.states.async_set(
        "light.test_light", "on", {"supported_color_modes": ["color_temp"], "brightness": 255}
    )
    await hass.async_block_till_done()

    forced_calls = [c for c in apply_lighting_calls if c.data.get("force")]
    assert any(c.data["entities"] == ["light.test_light"] for c in forced_calls), (
        f"expected a forced resync of light.test_light, got: {[c.data for c in apply_lighting_calls]}"
    )


async def test_recovered_trigger_does_not_turn_on_a_light_that_reconnects_off_in_a_dark_room(
    hass: HomeAssistant, apply_lighting_calls
):
    """The bug found immediately after fixing the trigger above: Jacob's
    only light, off at bedtime, turned on purely from a network blip.
    A light reconnecting *off*, in a room with nothing else on, must
    stay off - allow_turn_on is false (no motion, no manual run, room
    not occupied), so neither the main call nor the scoped recovered
    resync may include it."""
    hass.states.async_set(
        "light.test_light", "unavailable", {"supported_color_modes": ["color_temp"]}
    )
    await hass.async_block_till_done()

    await _setup_room_automation(
        hass, entity_id="automation.room", room_target={"entity_id": "light.test_light"}
    )

    hass.states.async_set(
        "light.test_light", "off", {"supported_color_modes": ["color_temp"]}
    )
    await hass.async_block_till_done()

    for call in apply_lighting_calls:
        assert "light.test_light" not in call.data["entities"], (
            f"a light that reconnected off must never be turned on: {call.data}"
        )


async def test_a_plain_off_to_on_transition_does_not_fire_the_recovered_trigger(
    hass: HomeAssistant, apply_lighting_calls
):
    """recovered is specifically for unavailable/unknown -> real state -
    an ordinary off -> on (someone/something just turning the light on
    normally) never touches the "is anything unavailable" aggregate at
    all, so it must not produce a forced, unconditional resync."""
    hass.states.async_set(
        "light.test_light", "off", {"supported_color_modes": ["color_temp"]}
    )
    await hass.async_block_till_done()

    await _setup_room_automation(
        hass, entity_id="automation.room", room_target={"entity_id": "light.test_light"}
    )

    hass.states.async_set(
        "light.test_light", "on", {"supported_color_modes": ["color_temp"], "brightness": 255}
    )
    await hass.async_block_till_done()

    assert not any(c.data.get("force") for c in apply_lighting_calls), (
        f"a plain off->on transition should never produce a forced call: "
        f"{[c.data for c in apply_lighting_calls]}"
    )


async def test_manual_run_forces_the_tick_and_turns_on_off_lights(hass: HomeAssistant, apply_lighting_calls):
    """Manual run is one of the two things allowed to turn a light on
    unconditionally (the other is motion) - and forces past override
    protection too."""
    hass.states.async_set(
        "light.test_light", "off", {"supported_color_modes": ["color_temp"]}
    )
    await hass.async_block_till_done()

    await _setup_room_automation(
        hass, entity_id="automation.room", room_target={"entity_id": "light.test_light"}
    )

    await hass.services.async_call("automation", "trigger", {"entity_id": "automation.room"}, blocking=True)
    await hass.async_block_till_done()

    main_calls = [c for c in apply_lighting_calls if c.data["entities"] == ["light.test_light"]]
    assert main_calls, f"expected a main apply_lighting call, got: {[c.data for c in apply_lighting_calls]}"
    assert main_calls[0].data["force"] is True


async def test_occupancy_detected_turns_on_off_lights_in_the_room(hass: HomeAssistant, apply_lighting_calls):
    """motion_on (occupancy.detected) is the other thing allowed to turn
    a light on unconditionally, regardless of what else in the room is
    on or off."""
    hass.states.async_set(
        "binary_sensor.test_occupancy", "off", {"device_class": "occupancy"}
    )
    hass.states.async_set(
        "light.test_light", "off", {"supported_color_modes": ["color_temp"]}
    )
    await hass.async_block_till_done()

    await _setup_room_automation(
        hass,
        entity_id="automation.room",
        room_target={"entity_id": ["light.test_light", "binary_sensor.test_occupancy"]},
    )

    hass.states.async_set(
        "binary_sensor.test_occupancy", "on", {"device_class": "occupancy"}
    )
    await hass.async_block_till_done()

    main_calls = [c for c in apply_lighting_calls if c.data["entities"] == ["light.test_light"]]
    assert main_calls, f"expected motion to turn on light.test_light, got: {[c.data for c in apply_lighting_calls]}"


async def test_occupied_room_lets_a_periodic_tick_turn_on_a_different_off_light(
    hass: HomeAssistant, apply_lighting_calls
):
    """The corrected (non-per-light-only) allow_turn_on rule: with the
    room already occupied (one light already on), a periodic adaptive
    tick may bring a *different*, currently-off light in the same room
    up to target too - not just update the one that's already on."""
    hass.states.async_set(
        "binary_sensor.test_occupancy", "on", {"device_class": "occupancy"}
    )
    hass.states.async_set(
        "light.on_light", "on", {"supported_color_modes": ["color_temp"], "brightness": 200, "color_temp_kelvin": 4000}
    )
    hass.states.async_set(
        "light.off_light", "off", {"supported_color_modes": ["color_temp"]}
    )
    await hass.async_block_till_done()

    await _setup_room_automation(
        hass,
        entity_id="automation.room",
        room_target={"entity_id": ["light.on_light", "light.off_light", "binary_sensor.test_occupancy"]},
    )

    hass.states.async_set(
        "sensor.test_adaptive", "Day", {"brightness": 210, "color_temp": 4000}
    )
    await hass.async_block_till_done()

    main_calls = [c for c in apply_lighting_calls if not c.data.get("force")]
    assert main_calls, f"expected a periodic adaptive tick to run, got: {[c.data for c in apply_lighting_calls]}"
    assert set(main_calls[-1].data["entities"]) == {"light.on_light", "light.off_light"}


async def test_fully_dark_room_periodic_tick_does_not_turn_anything_on(
    hass: HomeAssistant, apply_lighting_calls
):
    """The complement of the previous test: with nothing in the room on
    and no occupancy detected, a periodic adaptive tick either doesn't
    run at all (condition: gates on occupied for the no-sensor case) or,
    if it does run, must not include the off light."""
    hass.states.async_set(
        "light.test_light", "off", {"supported_color_modes": ["color_temp"]}
    )
    await hass.async_block_till_done()

    await _setup_room_automation(
        hass, entity_id="automation.room", room_target={"entity_id": "light.test_light"}
    )

    hass.states.async_set(
        "sensor.test_adaptive", "Day", {"brightness": 210, "color_temp": 4000}
    )
    await hass.async_block_till_done()

    for call in apply_lighting_calls:
        assert "light.test_light" not in call.data["entities"], (
            f"a fully dark, unoccupied room's periodic tick must never turn a light on: {call.data}"
        )
