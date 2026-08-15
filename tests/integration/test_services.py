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

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.adaptive_lighting_helpers import async_setup_entry

DOMAIN = "adaptive_lighting_helpers"


async def _setup_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Calls async_setup_entry directly rather than going through
    hass.config_entries.async_setup(), which would also resolve the
    manifest's http/frontend dependencies (needed in production for the
    dashboard card's add_extra_js_url - see __init__.py's async_setup)
    and pull in the separate, large home-assistant-frontend package for
    something these tests never touch. The services themselves don't
    depend on async_setup() having run at all."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
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


async def test_override_protection_survives_a_real_write_tracking_round_trip(setup_integration: HomeAssistant):
    """End-to-end version of what tests/test_grouping.py already proves
    at the pure-function level - this time through the real
    write_tracking.py Store and __init__.py's context propagation, not
    a fake. A light manually changed (a different context.id) after our
    own write must be left alone on the next non-forced call with the
    same owner_id."""
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
