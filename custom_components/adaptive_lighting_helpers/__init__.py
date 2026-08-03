"""
Adaptive Lighting Helpers.

Standalone Home Assistant services for adaptive-lighting computation:
brightness/colour-temperature curve math (curve.py), per-light
grouping - reachability, multiplier bucketing, tolerance checks,
manual-override protection, two-step transition routing (grouping.py) -
and scene-coverage gap filling, for "apply a scene, then a default for
whatever it doesn't cover" (scenes.py). Optionally also sets up
day-phase/curve sensors (sensor.py) as a native replacement for a
Jinja packages/*.yaml setup, if the config entry has schedule entities
configured - see config_flow.py.

Designed to work with the adaptive_lighting blueprint in this repo,
but not coupled to it: call any of the services directly from your own
automations/scripts if that's more useful to you. See README.md and
services.yaml (visible in Developer Tools -> Actions) for the full
contract of each service on its own terms.

curve.py, grouping.py, and scenes.py have no Home Assistant
dependency - this file (and sensor.py) are the only places that touch
`hass`, translating between real HA state/registries and the plain
functions those modules expose.
"""

from __future__ import annotations

import time
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .curve import brightness_for_phase, kelvin_for_phase, phase_at
from .grouping import EntityLookup, build_groups
from .scenes import SceneLookup, compute_scene_coverage

SCHEDULE_ENTITY_KEYS = (
    "morning_entity",
    "day_entity",
    "evening_earliest_entity",
    "evening_latest_entity",
    "night_entity",
)


def _has_schedule_config(entry: ConfigEntry) -> bool:
    return any(entry.data.get(key) for key in SCHEDULE_ENTITY_KEYS)


COMPUTE_LIGHTING_GROUPS_SCHEMA = vol.Schema(
    {
        vol.Required("entities"): [cv.entity_id],
        vol.Optional("brightness_multipliers", default=dict): dict,
        vol.Required("sensor_brightness"): vol.Coerce(int),
        vol.Required("sensor_color_temp_kelvin"): vol.Coerce(int),
        vol.Optional("brightness_tolerance", default=2): vol.Coerce(int),
        vol.Optional("color_temp_tolerance", default=10): vol.Coerce(int),
        vol.Optional("two_step_label", default="no_combined_transition"): cv.string,
    }
)

COMPUTE_CURVE_SCHEMA = vol.Schema(
    {
        vol.Required("morning"): vol.Coerce(float),
        vol.Required("day"): vol.Coerce(float),
        vol.Required("evening"): vol.Coerce(float),
        vol.Required("night"): vol.Coerce(float),
        vol.Optional("at"): vol.Coerce(float),
    }
)

COMPUTE_SCENE_COVERAGE_SCHEMA = vol.Schema(
    {
        vol.Optional("scene_entity_id"): cv.entity_id,
        vol.Required("scope_entities"): [cv.entity_id],
        vol.Required("target_entities"): [cv.entity_id],
    }
)


def _build_lookup(hass: HomeAssistant) -> EntityLookup:
    """Adapts real HA state/registries to the plain EntityLookup
    interface grouping.py expects - the only HA-specific piece of this
    integration, everything else is the pure modules doing the work."""

    def is_state(entity_id: str, state: str) -> bool:
        s = hass.states.get(entity_id)
        return s is not None and s.state == state

    def state_attr(entity_id: str, attr: str) -> Any:
        s = hass.states.get(entity_id)
        return s.attributes.get(attr) if s else None

    def device_id(entity_id: str) -> str | None:
        entry = er.async_get(hass).async_get(entity_id)
        return entry.device_id if entry else None

    def labels(id_: str | None) -> list:
        # id_ may be an entity_id or a device_id - EntityLookup.tags()
        # calls this with both, mirroring how HA's own `labels()`
        # template global is polymorphic over either.
        if not id_:
            return []
        entity_entry = er.async_get(hass).async_get(id_)
        if entity_entry is not None:
            return list(entity_entry.labels)
        device_entry = dr.async_get(hass).async_get(id_)
        if device_entry is not None:
            return list(device_entry.labels)
        return []

    def context_user_id(entity_id: str) -> str | None:
        s = hass.states.get(entity_id)
        return s.context.user_id if s else None

    return EntityLookup(
        is_state=is_state,
        state_attr=state_attr,
        device_id=device_id,
        labels=labels,
        context_user_id=context_user_id,
    )


def _build_scene_lookup(hass: HomeAssistant) -> SceneLookup:
    def exists(scene_entity_id: str) -> bool:
        return hass.states.get(scene_entity_id) is not None

    def covered_entities(scene_entity_id: str) -> list:
        s = hass.states.get(scene_entity_id)
        return list(s.attributes.get("entity_id", [])) if s else []

    return SceneLookup(exists=exists, covered_entities=covered_entities)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    async def compute_lighting_groups(call: ServiceCall) -> ServiceResponse:
        """adaptive_lighting_helpers.compute_lighting_groups

        Returns: {"groups": [{"multiplier", "brightness", "needing_off",
        "combined", "two_step"}, ...]} - see services.yaml for field docs.
        """
        groups = build_groups(
            entities=call.data["entities"],
            brightness_multipliers=call.data["brightness_multipliers"],
            sensor_brightness=call.data["sensor_brightness"],
            sensor_color_temp_kelvin=call.data["sensor_color_temp_kelvin"],
            lookup=_build_lookup(hass),
            brightness_tolerance=call.data["brightness_tolerance"],
            color_temp_tolerance=call.data["color_temp_tolerance"],
            two_step_label=call.data["two_step_label"],
        )
        return {
            "groups": [
                {
                    "multiplier": g.multiplier,
                    "brightness": g.brightness,
                    "needing_off": g.needing_off,
                    "combined": g.combined,
                    "two_step": g.two_step,
                }
                for g in groups
            ]
        }

    async def compute_curve(call: ServiceCall) -> ServiceResponse:
        """adaptive_lighting_helpers.compute_curve

        Returns: {"phase", "brightness", "kelvin"} for the given
        instant (or now) - see services.yaml for field docs.
        """
        at = call.data.get("at", time.time())
        morning, day, evening, night = (
            call.data["morning"],
            call.data["day"],
            call.data["evening"],
            call.data["night"],
        )
        phase = phase_at(at, morning, day, evening, night)
        return {
            "phase": phase,
            "brightness": brightness_for_phase(phase, at, night),
            "kelvin": kelvin_for_phase(phase, at, evening, day, night),
        }

    async def compute_scene_coverage_service(call: ServiceCall) -> ServiceResponse:
        """adaptive_lighting_helpers.compute_scene_coverage

        Returns: {"scene_active", "scene_valid", "covered_entities",
        "uncovered_entities"} - see services.yaml for field docs.
        """
        result = compute_scene_coverage(
            scene_entity_id=call.data.get("scene_entity_id"),
            scope_entities=call.data["scope_entities"],
            target_entities=call.data["target_entities"],
            lookup=_build_scene_lookup(hass),
        )
        return {
            "scene_active": result.scene_active,
            "scene_valid": result.scene_valid,
            "covered_entities": result.covered_entities,
            "uncovered_entities": result.uncovered_entities,
        }

    hass.services.async_register(
        DOMAIN,
        "compute_lighting_groups",
        compute_lighting_groups,
        schema=COMPUTE_LIGHTING_GROUPS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "compute_curve",
        compute_curve,
        schema=COMPUTE_CURVE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "compute_scene_coverage",
        compute_scene_coverage_service,
        schema=COMPUTE_SCENE_COVERAGE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    if _has_schedule_config(entry):
        await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.services.async_remove(DOMAIN, "compute_lighting_groups")
    hass.services.async_remove(DOMAIN, "compute_curve")
    hass.services.async_remove(DOMAIN, "compute_scene_coverage")

    if _has_schedule_config(entry):
        return await hass.config_entries.async_unload_platforms(entry, [Platform.SENSOR])

    return True
