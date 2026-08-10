"""
Adaptive Lighting Helpers.

Standalone Home Assistant services for adaptive-lighting computation:
brightness/colour-temperature curve math (curve.py), per-light
grouping - reachability, multiplier bucketing, tolerance checks,
manual-override protection, two-step transition routing, RGB-vs-colour-
temp routing (grouping.py) - and scene-coverage gap filling, for "apply
a scene, then a default for whatever it doesn't cover" (scenes.py).
`compute_lighting_groups` is a pure planner (returns groups, doesn't
touch any light); `apply_lighting` wraps the same grouping logic and
actually dispatches light.turn_on/turn_off, reading its brightness/
colour target off any sensor entity you point it at - see README's
"Bring your own sensor" section. Optionally also sets up day-phase/curve
sensors (sensor.py) and a phase-override select (select.py) as a native
replacement for a Jinja packages/*.yaml setup, if the config entry has
schedule times configured - see config_flow.py.

Designed to work with the adaptive_lighting blueprint in this repo,
but not coupled to it: call any of the services directly from your own
automations/scripts if that's more useful to you. See README.md and
services.yaml (visible in Developer Tools -> Actions) for the full
contract of each service on its own terms.

curve.py, grouping.py, and scenes.py have no Home Assistant
dependency - this file, sensor.py, select.py, and coordinator.py are
the only places that touch `hass`, translating between real HA
state/registries and the plain functions those modules expose.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN
from .coordinator import CURVE_KEYS, ScheduleCoordinator, schedule_instances
from .curve import phase_at, targets_for_phase
from .grouping import EntityLookup, Group, build_groups
from .scenes import SceneLookup, compute_scene_coverage

SCHEDULE_PLATFORMS = [Platform.SENSOR, Platform.SELECT]


COMPUTE_LIGHTING_GROUPS_SCHEMA = vol.Schema(
    {
        vol.Required("entities"): [cv.entity_id],
        vol.Optional("brightness_multipliers", default=dict): dict,
        vol.Required("sensor_brightness"): vol.Coerce(int),
        vol.Required("sensor_color_temp_kelvin"): vol.Coerce(int),
        vol.Optional("brightness_tolerance", default=2): vol.Coerce(int),
        vol.Optional("color_temp_tolerance", default=10): vol.Coerce(int),
        vol.Optional("two_step_label", default="no_combined_transition"): cv.string,
        vol.Optional("prefer_rgb_color", default=False): cv.boolean,
        vol.Optional("rgb_color"): vol.All([vol.Coerce(int)], vol.Length(min=3, max=3)),
        vol.Optional("rgb_color_tolerance", default=10): vol.Coerce(int),
    }
)

COMPUTE_CURVE_SCHEMA = vol.Schema(
    {
        vol.Required("morning"): vol.Coerce(float),
        vol.Required("day"): vol.Coerce(float),
        vol.Required("evening"): vol.Coerce(float),
        vol.Required("night"): vol.Coerce(float),
        vol.Optional("at"): vol.Coerce(float),
        # Same optional curve fields as config_flow.py's
        # CURVE_AND_BEHAVIOR_FIELDS - left unset, targets_for_phase's own
        # defaults apply, matching this service's original behaviour.
        vol.Optional("day_brightness"): vol.Coerce(int),
        vol.Optional("evening_brightness"): vol.Coerce(int),
        vol.Optional("night_brightness"): vol.Coerce(int),
        vol.Optional("morning_kelvin"): vol.Coerce(int),
        vol.Optional("day_end_kelvin"): vol.Coerce(int),
        vol.Optional("evening_kelvin"): vol.Coerce(int),
        vol.Optional("night_kelvin"): vol.Coerce(int),
    }
)

COMPUTE_SCENE_COVERAGE_SCHEMA = vol.Schema(
    {
        vol.Optional("scene_entity_id"): cv.entity_id,
        vol.Required("scope_entities"): [cv.entity_id],
        vol.Required("target_entities"): [cv.entity_id],
    }
)

APPLY_LIGHTING_SCHEMA = vol.Schema(
    {
        vol.Required("entities"): [cv.entity_id],
        vol.Optional("brightness_multipliers", default=dict): dict,
        vol.Required("sensor_entity_id"): cv.entity_id,
        vol.Required("transition"): vol.Coerce(float),
        vol.Optional("brightness_tolerance", default=2): vol.Coerce(int),
        vol.Optional("color_temp_tolerance", default=10): vol.Coerce(int),
        vol.Optional("two_step_label", default="no_combined_transition"): cv.string,
        vol.Optional("prefer_rgb_color", default=False): cv.boolean,
        vol.Optional("rgb_color_tolerance", default=10): vol.Coerce(int),
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


def _read_sensor_targets(hass: HomeAssistant, sensor_entity_id: str) -> tuple[int, int, tuple | None]:
    """Reads brightness/color_temp/rgb_color off sensor_entity_id for
    apply_lighting - fully generic, works with any entity exposing those
    attribute names, not hardcoded to this integration's own
    sensor.adaptive_lighting. See README's "Bring your own sensor"
    section for the exact contract and a template-sensor example.
    Defaults (0 brightness, 3000K) match what the blueprint's own
    sensor_brightness/sensor_color_temp_kelvin variables used before this
    service existed."""
    state = hass.states.get(sensor_entity_id)
    if state is None:
        raise ServiceValidationError(f"Sensor entity not found: {sensor_entity_id}")

    def _as_int(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    brightness = _as_int(state.attributes.get("brightness"), 0)
    color_temp_kelvin = _as_int(state.attributes.get("color_temp"), 3000)
    rgb = state.attributes.get("rgb_color")
    rgb_color = tuple(rgb) if isinstance(rgb, (list, tuple)) and len(rgb) == 3 else None
    return brightness, color_temp_kelvin, rgb_color


def _groups_response(groups: list[Group]) -> ServiceResponse:
    return {
        "groups": [
            {
                "multiplier": g.multiplier,
                "brightness": g.brightness,
                "needing_off": g.needing_off,
                "combined": g.combined,
                "two_step": g.two_step,
                "combined_rgb": g.combined_rgb,
                "two_step_rgb": g.two_step_rgb,
            }
            for g in groups
        ]
    }


async def _two_step_turn_on(
    hass: HomeAssistant,
    entity_ids: list,
    brightness: int,
    half_transition: float,
    *,
    color_temp_kelvin: int | None = None,
    rgb_color: list | None = None,
) -> None:
    """Brightness-only call, wait, then brightness + colour - for bulbs
    that can't transition both together (no_combined_transition label).
    Works the same for either colour representation; only the second
    call's colour field differs."""
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": entity_ids, "transition": half_transition, "brightness": brightness},
        blocking=True,
    )
    await asyncio.sleep(half_transition)
    data = {"entity_id": entity_ids, "transition": half_transition, "brightness": brightness}
    if color_temp_kelvin is not None:
        data["color_temp_kelvin"] = color_temp_kelvin
    else:
        data["rgb_color"] = rgb_color
    await hass.services.async_call("light", "turn_on", data, blocking=True)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    async def compute_lighting_groups(call: ServiceCall) -> ServiceResponse:
        """adaptive_lighting_helpers.compute_lighting_groups

        Returns: {"groups": [{"multiplier", "brightness", "needing_off",
        "combined", "two_step", "combined_rgb", "two_step_rgb"}, ...]} -
        see services.yaml for field docs.
        """
        rgb_color = call.data.get("rgb_color")
        groups = build_groups(
            entities=call.data["entities"],
            brightness_multipliers=call.data["brightness_multipliers"],
            sensor_brightness=call.data["sensor_brightness"],
            sensor_color_temp_kelvin=call.data["sensor_color_temp_kelvin"],
            lookup=_build_lookup(hass),
            brightness_tolerance=call.data["brightness_tolerance"],
            color_temp_tolerance=call.data["color_temp_tolerance"],
            two_step_label=call.data["two_step_label"],
            prefer_rgb_color=call.data["prefer_rgb_color"],
            rgb_color=tuple(rgb_color) if rgb_color else None,
            rgb_color_tolerance=call.data["rgb_color_tolerance"],
        )
        return _groups_response(groups)

    async def compute_curve(call: ServiceCall) -> ServiceResponse:
        """adaptive_lighting_helpers.compute_curve

        Returns: {"phase", "brightness", "kelvin", "rgb_color"} for the
        given instant (or now) - see services.yaml for field docs.
        """
        at = call.data.get("at", time.time())
        morning, day, evening, night = (
            call.data["morning"],
            call.data["day"],
            call.data["evening"],
            call.data["night"],
        )
        curve_kwargs = {key: call.data[key] for key in CURVE_KEYS if key in call.data}
        phase = phase_at(at, morning, day, evening, night)
        targets = targets_for_phase(phase, at, evening, day, night, **curve_kwargs)
        return {
            "phase": phase,
            "brightness": targets["brightness"],
            "kelvin": targets["kelvin"],
            "rgb_color": list(targets["rgb_color"]),
        }

    async def apply_lighting(call: ServiceCall) -> ServiceResponse:
        """adaptive_lighting_helpers.apply_lighting

        Reads brightness/color_temp/rgb_color off sensor_entity_id (any
        entity with those attributes - see README's "Bring your own
        sensor") and actually turns entities on/off via
        light.turn_on/turn_off, handling reachability, tolerance,
        manual-override protection, two-step transitions, and RGB-vs-
        colour-temp dispatch internally rather than leaving it to the
        caller. Returns the same {"groups": [...]} shape as
        compute_lighting_groups for introspection, but nothing requires
        capturing it - see services.yaml for field docs.
        """
        brightness, color_temp_kelvin, rgb_color = _read_sensor_targets(hass, call.data["sensor_entity_id"])
        groups = build_groups(
            entities=call.data["entities"],
            brightness_multipliers=call.data["brightness_multipliers"],
            sensor_brightness=brightness,
            sensor_color_temp_kelvin=color_temp_kelvin,
            lookup=_build_lookup(hass),
            brightness_tolerance=call.data["brightness_tolerance"],
            color_temp_tolerance=call.data["color_temp_tolerance"],
            two_step_label=call.data["two_step_label"],
            prefer_rgb_color=call.data["prefer_rgb_color"],
            rgb_color=rgb_color,
            rgb_color_tolerance=call.data["rgb_color_tolerance"],
        )

        transition = call.data["transition"]
        half_transition = round(transition / 2, 1)
        rgb_color_list = list(rgb_color) if rgb_color is not None else None

        tasks = []
        for g in groups:
            if g.needing_off:
                tasks.append(
                    hass.services.async_call(
                        "light", "turn_off", {"entity_id": g.needing_off, "transition": transition}, blocking=True
                    )
                )
            if g.combined:
                tasks.append(
                    hass.services.async_call(
                        "light",
                        "turn_on",
                        {
                            "entity_id": g.combined,
                            "transition": transition,
                            "brightness": g.brightness,
                            "color_temp_kelvin": color_temp_kelvin,
                        },
                        blocking=True,
                    )
                )
            if g.combined_rgb:
                tasks.append(
                    hass.services.async_call(
                        "light",
                        "turn_on",
                        {
                            "entity_id": g.combined_rgb,
                            "transition": transition,
                            "brightness": g.brightness,
                            "rgb_color": rgb_color_list,
                        },
                        blocking=True,
                    )
                )
            if g.two_step:
                tasks.append(
                    _two_step_turn_on(
                        hass, g.two_step, g.brightness, half_transition, color_temp_kelvin=color_temp_kelvin
                    )
                )
            if g.two_step_rgb:
                tasks.append(
                    _two_step_turn_on(hass, g.two_step_rgb, g.brightness, half_transition, rgb_color=rgb_color_list)
                )

        if tasks:
            await asyncio.gather(*tasks)

        return _groups_response(groups)

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
    hass.services.async_register(
        DOMAIN,
        "apply_lighting",
        apply_lighting,
        schema=APPLY_LIGHTING_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    # Reloads the entry on any entry/subentry change - covers the main
    # entry's reconfigure flow, and (since adding/removing/reconfiguring a
    # subentry doesn't otherwise trigger a reload on its own) named
    # sensors added via the "Add Sensor" subentry flow too. config_flow.py
    # deliberately uses async_update_and_abort rather than
    # *_reload_and_abort so this is the only thing that reloads - the
    # subentry version of *_reload_and_abort raises if an update listener
    # is registered, and duplicating it here would double-reload anyway.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    instances = schedule_instances(entry)
    if instances:
        for instance in instances:
            coordinator = ScheduleCoordinator(hass, instance)
            await coordinator.async_config_entry_first_refresh()
            hass.data.setdefault(DOMAIN, {})[instance.key] = coordinator

        def _refresh_all(event) -> None:
            for instance in instances:
                hass.async_create_task(hass.data[DOMAIN][instance.key].async_request_refresh())

        # Evening tracks sun.sun; each instance's phase-override select
        # can change at any moment and should take effect immediately
        # rather than waiting for the next 60s poll. The five schedule
        # times themselves don't need tracking - they're static config,
        # only changed via the reconfigure flow, which reloads the entry
        # (rebuilding every coordinator from scratch) on its own. Every
        # instance's coordinator is refreshed on any tracked change
        # rather than mapping specific entities to specific coordinators
        # - simpler, and refreshing extra ones is cheap pure computation.
        entry.async_on_unload(
            async_track_state_change_event(
                hass, ["sun.sun", *(instance.override_entity_id for instance in instances)], _refresh_all
            )
        )

        await hass.config_entries.async_forward_entry_setups(entry, SCHEDULE_PLATFORMS)

    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.services.async_remove(DOMAIN, "compute_lighting_groups")
    hass.services.async_remove(DOMAIN, "compute_curve")
    hass.services.async_remove(DOMAIN, "compute_scene_coverage")
    hass.services.async_remove(DOMAIN, "apply_lighting")

    instances = schedule_instances(entry)
    if instances:
        unloaded = await hass.config_entries.async_unload_platforms(entry, SCHEDULE_PLATFORMS)
        for instance in instances:
            hass.data.get(DOMAIN, {}).pop(instance.key, None)
        return unloaded

    return True
