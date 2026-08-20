"""
Adaptive Lighting Helpers.

Standalone Home Assistant services for adaptive-lighting computation:
brightness/colour-temperature curve math (curve.py), per-light
grouping - reachability, multiplier bucketing, tolerance checks,
externally-set protection (leaving a light alone once something other
than our own last write has touched it - see write_tracking.py), two-
step transition routing, RGB-vs-colour-temp routing (grouping.py) - and
scene-coverage gap filling, for "apply a scene, then a default for
whatever it doesn't cover" (scenes.py).
`compute_lighting_groups` is a pure planner (returns groups, doesn't
touch any light); `apply_lighting` wraps the same grouping logic and
actually dispatches light.turn_on/turn_off, reading its brightness/
colour target off any sensor entity you point it at - see
docs/HELPERS.md's "Bring your own sensor" section. Optionally also sets up day-phase/curve
sensors (sensor.py), a phase-override select (select.py), and the
schedule/curve config as live entities (time.py, number.py, switch.py)
per named sensor added afterwards (Settings -> Devices & Services ->
Adaptive Lighting Helpers -> Add Sensor) - a native replacement for a
Jinja packages/*.yaml setup - see config_flow.py.

Designed to work with the adaptive_lighting blueprint in this repo,
but not coupled to it: call any of the services directly from your own
automations/scripts if that's more useful to you. See README.md and
services.yaml (visible in Developer Tools -> Actions) for the full
contract of each service on its own terms.

curve.py, grouping.py, and scenes.py have no Home Assistant
dependency - this file, sensor.py, select.py, number.py, time.py,
switch.py, and coordinator.py are the only places that touch `hass`,
translating between real HA state/registries and the plain functions
those modules expose.
"""

from __future__ import annotations

import asyncio
# Aliased - this package also has its own time.py (the HA `time` platform
# module, forwarded via SCHEDULE_PLATFORMS below). Importing a submodule
# unconditionally rebinds it as an attribute of the parent package, which
# is this module's own global namespace - a bare `import time` here gets
# silently clobbered the moment anything imports adaptive_lighting_helpers.time
# (any entry with an "at"-less compute_curve call, e.g. every real
# install with at least one schedule instance, plus now every entry
# regardless of instances - see async_forward_entry_setups below), and
# every later time.time() call in this module then raises AttributeError
# against the wrong module. Caught live via a test that unconditionally
# forwards SCHEDULE_PLATFORMS for the first time on a zero-instance entry.
import time as time_module
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Context, HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import CURVE_KEYS, ScheduleCoordinator, schedule_instances
from .curve import phase_at, targets_for_phase
from .grouping import EntityLookup, Group, build_groups
from .scenes import SceneLookup, compute_scene_coverage
from .two_step_check import async_start_watching
from .write_tracking import LastWriteTracker

SCHEDULE_PLATFORMS = [Platform.SENSOR, Platform.SELECT, Platform.NUMBER, Platform.TIME, Platform.SWITCH]


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
        vol.Optional("owner_id"): cv.string,
        vol.Optional("force", default=False): cv.boolean,
    }
)

COMPUTE_CURVE_SCHEMA = vol.Schema(
    {
        vol.Required("morning"): vol.Coerce(float),
        vol.Required("day"): vol.Coerce(float),
        vol.Required("evening"): vol.Coerce(float),
        vol.Required("night"): vol.Coerce(float),
        vol.Optional("at"): vol.Coerce(float),
        # Same eight curve fields a sensor's number.* entities expose
        # (see number.py), built from the same CURVE_KEYS (coordinator.py)
        # rather than listing the names again - left unset,
        # targets_for_phase's own defaults apply, matching this
        # service's original behaviour.
        **{vol.Optional(key): vol.Coerce(int) for key in CURVE_KEYS},
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
        vol.Optional("owner_id"): cv.string,
        vol.Optional("force", default=False): cv.boolean,
    }
)


def _build_lookup(hass: HomeAssistant, tracker: LastWriteTracker) -> EntityLookup:
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

    def context_id(entity_id: str) -> str | None:
        s = hass.states.get(entity_id)
        return s.context.id if s else None

    return EntityLookup(
        is_state=is_state,
        state_attr=state_attr,
        device_id=device_id,
        labels=labels,
        context_id=context_id,
        confirmed_context_id=tracker.confirmed_context_id,
        confirmed_owner_id=tracker.confirmed_owner_id,
        pending_context_id=tracker.pending_context_id,
        pending_owner_id=tracker.pending_owner_id,
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
    sensor.adaptive_lighting. See docs/HELPERS.md's "Bring your own
    sensor" section for the exact contract and a template-sensor example.

    brightness/color_temp are required by that contract, and a sensor
    missing them (wrong entity picked, or the sensor is unavailable and
    its attributes are gone) raises rather than falling back to a quiet
    default - the old fallback (brightness 0, later floored to 1 by
    grouping) meant a broken sensor silently dimmed every light to
    minimum instead of surfacing anywhere."""
    state = hass.states.get(sensor_entity_id)
    if state is None:
        raise ServiceValidationError(f"Sensor entity not found: {sensor_entity_id}")

    def _require_int(attr: str) -> int:
        value = state.attributes.get(attr)
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ServiceValidationError(
                f"{sensor_entity_id} has no usable '{attr}' attribute (got {value!r}) - "
                f"apply_lighting needs brightness and color_temp on the sensor it reads"
            )

    brightness = _require_int("brightness")
    color_temp_kelvin = _require_int("color_temp")
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
    context: Context,
    *,
    color_temp_kelvin: int | None = None,
    rgb_color: list | None = None,
) -> None:
    """Brightness-only call, wait, then brightness + colour - for bulbs
    that can't transition both together (no_combined_transition label).
    Works the same for either colour representation; only the second
    call's colour field differs. `context` is threaded through both
    calls explicitly - hass.services.async_call makes its own fresh,
    unrelated Context() for anything it isn't given (confirmed against
    HA core's core.py), so without this every light we turn on would
    get an unmatched context.id and immediately look externally-set on
    the very next tick (see write_tracking.py)."""
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": entity_ids, "transition": half_transition, "brightness": brightness},
        blocking=True,
        context=context,
    )
    await asyncio.sleep(half_transition)
    data = {"entity_id": entity_ids, "transition": half_transition, "brightness": brightness}
    if color_temp_kelvin is not None:
        data["color_temp_kelvin"] = color_temp_kelvin
    else:
        data["rgb_color"] = rgb_color
    await hass.services.async_call("light", "turn_on", data, blocking=True, context=context)


CARD_URL_BASE = "/adaptive_lighting_helpers_static"
CARD_JS_PATH = "adaptive-lighting-curve-card.js"
WRITE_TRACKING_CARD_JS_PATH = "adaptive-lighting-write-tracking-card.js"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve www/adaptive-lighting-curve-card.js and
    www/adaptive-lighting-write-tracking-card.js and auto-load both on
    every frontend page - runs once for the whole domain, regardless of
    how many config entries/subentries exist, so the cards ship and
    update with the integration itself (via HACS) rather than needing
    a separate manual Lovelace resource registration step that can
    silently drift out of sync with them (see CLAUDE.md for the live
    incident this replaced). One static path already serves the whole
    www/ directory, so a second card needs only a second
    add_extra_js_url call, not a second StaticPathConfig.
    cache_headers=False deliberately - neither file has a versioned URL,
    so aggressive caching here would just trade a stale-deployed-file
    bug for a stale-browser-cache one."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL_BASE, str(Path(__file__).parent / "www"), cache_headers=False)]
    )
    add_extra_js_url(hass, f"{CARD_URL_BASE}/{CARD_JS_PATH}")
    add_extra_js_url(hass, f"{CARD_URL_BASE}/{WRITE_TRACKING_CARD_JS_PATH}")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Shared across every apply_lighting call, from whichever automation
    # made it - see write_tracking.py for why (grouping.py's
    # externally_set() check only cares "did adaptive control write this
    # most recently", not which specific caller). Loaded once here so
    # last-write provenance survives a HA restart.
    write_tracker = LastWriteTracker(hass)
    await write_tracker.async_load()
    entry.async_on_unload(write_tracker.async_start_listening(hass))

    # Raises a fixable repair when a bulb that's known to need two-step
    # transitions isn't carrying the label that routes it there - the
    # one part of this integration's behaviour that depends on registry
    # data a user has to maintain by hand, and which fails silently when
    # they forget (see two_step.py).
    entry.async_on_unload(async_start_watching(hass, entry))

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
            lookup=_build_lookup(hass, write_tracker),
            brightness_tolerance=call.data["brightness_tolerance"],
            color_temp_tolerance=call.data["color_temp_tolerance"],
            two_step_label=call.data["two_step_label"],
            prefer_rgb_color=call.data["prefer_rgb_color"],
            rgb_color=tuple(rgb_color) if rgb_color else None,
            rgb_color_tolerance=call.data["rgb_color_tolerance"],
            owner_id=call.data.get("owner_id"),
            force=call.data["force"],
        )
        return _groups_response(groups)

    async def compute_curve(call: ServiceCall) -> ServiceResponse:
        """adaptive_lighting_helpers.compute_curve

        Returns: {"phase", "brightness", "kelvin", "rgb_color"} for the
        given instant (or now) - see services.yaml for field docs.
        """
        at = call.data.get("at", time_module.time())
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
        externally-set protection, two-step transitions, and RGB-vs-
        colour-temp dispatch internally rather than leaving it to the
        caller. Returns the same {"groups": [...]} shape as
        compute_lighting_groups for introspection, but nothing requires
        capturing it - see services.yaml for field docs.

        owner_id (optional): identifies this caller for externally-set
        protection - e.g. a blueprint automation passing its own
        `this.entity_id`. Omit it entirely to skip that check altogether
        and always write, same as force below, but without claiming the
        write for a later call to recognise; pass it to have a light
        left alone once anything other than *this same* owner_id's own
        last write has touched it since.

        force (optional, default false): bypasses externally-set
        protection outright for this call, regardless of owner_id. The
        write is still recorded under owner_id if one was given, so a
        later, non-forced call with that same owner_id correctly
        recognises it as its own rather than finding an orphaned record
        - the right way to force through *and* keep protection working
        normally afterward. See grouping.py's EntityLookup.externally_set()
        for the full semantics of both parameters together.
        """
        owner_id = call.data.get("owner_id")
        force = call.data["force"]
        brightness, color_temp_kelvin, rgb_color = _read_sensor_targets(hass, call.data["sensor_entity_id"])
        lookup = _build_lookup(hass, write_tracker)
        groups = build_groups(
            entities=call.data["entities"],
            brightness_multipliers=call.data["brightness_multipliers"],
            sensor_brightness=brightness,
            sensor_color_temp_kelvin=color_temp_kelvin,
            lookup=lookup,
            brightness_tolerance=call.data["brightness_tolerance"],
            color_temp_tolerance=call.data["color_temp_tolerance"],
            two_step_label=call.data["two_step_label"],
            prefer_rgb_color=call.data["prefer_rgb_color"],
            rgb_color=rgb_color,
            rgb_color_tolerance=call.data["rgb_color_tolerance"],
            owner_id=owner_id,
            force=force,
        )

        transition = call.data["transition"]
        half_transition = round(transition / 2, 1)
        rgb_color_list = list(rgb_color) if rgb_color is not None else None

        # Every light.turn_on/turn_off call below is given call.context
        # explicitly (rather than left to default to a fresh, unrelated
        # one - see _two_step_turn_on's docstring) so the resulting
        # state's context.id is exactly what write_tracker records below,
        # matching what grouping.py's externally_set() compares against
        # on the next tick.
        written_entities: list = []
        tasks = []
        for g in groups:
            if g.needing_off:
                written_entities.extend(g.needing_off)
                tasks.append(
                    hass.services.async_call(
                        "light",
                        "turn_off",
                        {"entity_id": g.needing_off, "transition": transition},
                        blocking=True,
                        context=call.context,
                    )
                )
            if g.combined:
                written_entities.extend(g.combined)
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
                        context=call.context,
                    )
                )
            if g.combined_rgb:
                written_entities.extend(g.combined_rgb)
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
                        context=call.context,
                    )
                )
            if g.two_step:
                written_entities.extend(g.two_step)
                tasks.append(
                    _two_step_turn_on(
                        hass,
                        g.two_step,
                        g.brightness,
                        half_transition,
                        call.context,
                        color_temp_kelvin=color_temp_kelvin,
                    )
                )
            if g.two_step_rgb:
                written_entities.extend(g.two_step_rgb)
                tasks.append(
                    _two_step_turn_on(
                        hass, g.two_step_rgb, g.brightness, half_transition, call.context, rgb_color=rgb_color_list
                    )
                )

        # Snapshotted *before* any of the writes above are dispatched -
        # nothing async has run yet since build_groups() returned (the
        # gather below is the first await point), so this is a true
        # walking-in value. write_tracker needs it to tell whether the
        # *previous* pending write actually landed, which can only be
        # judged against state as it was before this call's own writes -
        # reading it after would risk comparing a light's context against
        # the very write about to be recorded, if it happened to land
        # synchronously. See write_tracking.py's async_record docstring.
        live_context_before_write = {e: lookup.context_id(e) for e in written_entities}

        if tasks:
            await asyncio.gather(*tasks)
        if written_entities:
            await write_tracker.async_record(written_entities, live_context_before_write, call.context.id, owner_id)

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

    # Entry-scoped (not per-instance), so the write-tracking diagnostic
    # sensor (sensor.py's _WriteTrackingSensor) can look it up regardless
    # of how many schedule instances - possibly zero - exist.
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = write_tracker

    instances = schedule_instances(entry)
    if instances:
        for instance in instances:
            coordinator = ScheduleCoordinator(hass, instance)
            await coordinator.async_config_entry_first_refresh()
            hass.data.setdefault(DOMAIN, {})[instance.subentry_id] = coordinator

        @callback
        def _refresh_all(event) -> None:
            # @callback marks this as event-loop-safe for HA's job
            # dispatcher - without it, a plain function here gets run in
            # the executor thread pool instead (HassJobType.Executor),
            # and hass.async_create_task() is only safe to call from the
            # event loop itself. Confirmed against HA core source
            # (get_hassjob_callable_job_type) after this fired a real
            # "calls hass.async_create_task from a thread other than the
            # event loop" RuntimeError in production - undecorated sync
            # listeners are silently unsafe here, not just a lint nit.
            for instance in instances:
                hass.async_create_task(hass.data[DOMAIN][instance.subentry_id].async_request_refresh())

        # Evening tracks sun.sun, so a sunset update should take effect
        # without waiting for the next 60s poll. This is the only entity
        # tracked here: the schedule/curve config entities and the
        # phase-override select each refresh their own coordinator
        # themselves on change (see time.py/number.py/select.py) - each
        # is the only writer of its own state, so routing their changes
        # back through the state machine here would just be a second,
        # global copy of a refresh the entity already owns.
        entry.async_on_unload(async_track_state_change_event(hass, ["sun.sun"], _refresh_all))

    # Unconditional, not gated on instances - the write-tracking sensor
    # (sensor.py) is entry-scoped and should exist even with zero
    # schedule instances configured. Harmless when instances is empty:
    # each platform's own async_setup_entry loop over schedule_instances()
    # just adds nothing for the per-instance entities.
    await hass.config_entries.async_forward_entry_setups(entry, SCHEDULE_PLATFORMS)

    if instances:
        # The first refresh above ran before the time.*/number.* entities
        # existed (or while a reload had left them "unavailable"), so it
        # computed from defaults - see coordinator._time_ts(). Now that
        # the platforms are loaded and every config entity has restored
        # its real value, recompute once so the sensors reflect the
        # user's actual schedule immediately instead of up to 60s later.
        for instance in instances:
            await hass.data[DOMAIN][instance.subentry_id].async_refresh()

    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.services.async_remove(DOMAIN, "compute_lighting_groups")
    hass.services.async_remove(DOMAIN, "compute_curve")
    hass.services.async_remove(DOMAIN, "compute_scene_coverage")
    hass.services.async_remove(DOMAIN, "apply_lighting")

    instances = schedule_instances(entry)
    unloaded = await hass.config_entries.async_unload_platforms(entry, SCHEDULE_PLATFORMS)
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    for instance in instances:
        hass.data.get(DOMAIN, {}).pop(instance.subentry_id, None)
    return unloaded

    return True
