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
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import CURVE_KEYS, ScheduleCoordinator, schedule_instances
from .curve import phase_at, targets_for_phase
from .grouping import EntityLookup, Group, build_groups
from .override_protection import classify, is_blocked
from .scenes import SceneLookup, compute_scene_coverage
from .two_step_check import async_start_watching
from .write_tracking import PRUNE_CHECK_INTERVAL, LastWriteTracker

SCHEDULE_PLATFORMS = [Platform.SENSOR, Platform.SELECT, Platform.NUMBER, Platform.TIME, Platform.SWITCH]


COMPUTE_LIGHTING_GROUPS_SCHEMA = vol.Schema(
    {
        vol.Required("entities"): [cv.entity_id],
        vol.Optional("brightness_multipliers", default=dict): dict,
        vol.Required("brightness"): vol.Coerce(int),
        vol.Required("color_temp_kelvin"): vol.Coerce(int),
        vol.Optional("brightness_tolerance", default=2): vol.Coerce(int),
        vol.Optional("color_temp_tolerance", default=10): vol.Coerce(int),
        vol.Optional("two_step_label", default="no_combined_transition"): cv.string,
        vol.Optional("prefer_rgb_color", default=False): cv.boolean,
        # vol.Any(None, ...) rather than a bare vol.All(...) - a caller
        # templating this from a sensor attribute that may not exist
        # (e.g. the blueprint's own adaptive_sensor, for a "bring your
        # own sensor" entity that doesn't populate rgb_color) renders an
        # explicit None, not an omitted key. A bare vol.All([...],
        # vol.Length(...)) rejects None outright as "not a list" -
        # confirmed live as a real gap, not hypothetical, once this
        # exact call shape was worked through for the blueprint change.
        vol.Optional("rgb_color"): vol.Any(None, vol.All([vol.Coerce(int)], vol.Length(min=3, max=3))),
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
        vol.Required("brightness"): vol.Coerce(int),
        vol.Required("color_temp_kelvin"): vol.Coerce(int),
        vol.Required("transition"): vol.Coerce(float),
        vol.Optional("brightness_tolerance", default=2): vol.Coerce(int),
        vol.Optional("color_temp_tolerance", default=10): vol.Coerce(int),
        vol.Optional("two_step_label", default="no_combined_transition"): cv.string,
        vol.Optional("prefer_rgb_color", default=False): cv.boolean,
        # See COMPUTE_LIGHTING_GROUPS_SCHEMA's own rgb_color comment for
        # why vol.Any(None, ...) rather than a bare vol.All(...).
        vol.Optional("rgb_color"): vol.Any(None, vol.All([vol.Coerce(int)], vol.Length(min=3, max=3))),
        vol.Optional("rgb_color_tolerance", default=10): vol.Coerce(int),
        vol.Optional("owner_id"): cv.string,
        vol.Optional("force", default=False): cv.boolean,
    }
)

CHECK_OWNERSHIP_SCHEMA = vol.Schema(
    {
        vol.Required("entities"): [cv.entity_id],
        vol.Optional("owner_id"): cv.string,
        vol.Optional("force", default=False): cv.boolean,
        vol.Optional("brightness_tolerance", default=2): vol.Coerce(int),
        vol.Optional("color_temp_tolerance", default=10): vol.Coerce(int),
        vol.Optional("rgb_color_tolerance", default=10): vol.Coerce(int),
    }
)

RECORD_OWNERSHIP_SCHEMA = vol.Schema(
    {
        vol.Required("entities"): [cv.entity_id],
        vol.Optional("owner_id"): cv.string,
        vol.Optional("targets", default=dict): dict,
    }
)

CLEAR_OWNERSHIP_SCHEMA = vol.Schema({vol.Required("entities"): [cv.entity_id]})


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
        pending_target=tracker.pending_target,
    )


def _build_scene_lookup(hass: HomeAssistant) -> SceneLookup:
    def exists(scene_entity_id: str) -> bool:
        return hass.states.get(scene_entity_id) is not None

    def covered_entities(scene_entity_id: str) -> list:
        s = hass.states.get(scene_entity_id)
        return list(s.attributes.get("entity_id", [])) if s else []

    return SceneLookup(exists=exists, covered_entities=covered_entities)


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
    # A HA restart alone gives every entity a fresh context.id, which
    # otherwise looks identical to a genuine external change - see
    # async_resync_to_live_state's own docstring, and the dated CLAUDE.md
    # entry for the live incident (light.kitchen_1, genuinely on, stuck
    # excluded purely from a restart) that prompted this.
    await write_tracker.async_resync_to_live_state(hass)
    # An entity deleted from HA outright (not just restarting - e.g. a
    # Zigbee2MQTT group removed at the source) never triggers the
    # unavailable-transition cleanup async_start_listening watches for,
    # since hass.states.get(...) just returns None forever with nothing
    # left to observe - see async_prune_stale's own docstring for the
    # live incident (light.extension_spots_left) that prompted this.
    # Called once here (catches anything that went stale while HA was
    # down) and again every PRUNE_CHECK_INTERVAL below (keeps the
    # promise current while running, not just at the next restart).
    await write_tracker.async_prune_stale()

    async def _periodic_prune(now) -> None:
        await write_tracker.async_prune_stale()

    entry.async_on_unload(
        async_track_time_interval(hass, _periodic_prune, PRUNE_CHECK_INTERVAL, cancel_on_shutdown=True)
    )
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
            sensor_brightness=call.data["brightness"],
            sensor_color_temp_kelvin=call.data["color_temp_kelvin"],
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

        Takes brightness/color_temp_kelvin/rgb_color as plain values -
        the same three fields compute_lighting_groups already takes,
        this being the one service in the pair that actually dispatches
        - and turns entities on/off via light.turn_on/turn_off, handling
        reachability, tolerance, externally-set protection, two-step
        transitions, and RGB-vs-colour-temp dispatch internally rather
        than leaving it to the caller. Returns the same {"groups": [...]}
        shape as compute_lighting_groups for introspection, but nothing
        requires capturing it - see services.yaml for field docs.

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
        brightness = call.data["brightness"]
        color_temp_kelvin = call.data["color_temp_kelvin"]
        rgb_color_raw = call.data.get("rgb_color")
        rgb_color = tuple(rgb_color_raw) if rgb_color_raw else None
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
        # What each entity's write actually asked for, keyed the same
        # way as written_entities - passed to write_tracker.async_record
        # below so a later context.id mismatch can be checked against
        # what we intended rather than assumed external. An off-command
        # has no brightness/colour target, so needing_off entities are
        # simply left out (write_targets.get() defaults to None).
        write_targets: dict = {}
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
                for e in g.combined:
                    write_targets[e] = {"brightness": g.brightness, "color_temp_kelvin": color_temp_kelvin}
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
                for e in g.combined_rgb:
                    write_targets[e] = {"brightness": g.brightness, "rgb_color": rgb_color_list}
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
                for e in g.two_step:
                    write_targets[e] = {"brightness": g.brightness, "color_temp_kelvin": color_temp_kelvin}
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
                for e in g.two_step_rgb:
                    write_targets[e] = {"brightness": g.brightness, "rgb_color": rgb_color_list}
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
            await write_tracker.async_record(
                written_entities, live_context_before_write, call.context.id, owner_id, targets=write_targets
            )

        return _groups_response(groups)

    async def check_ownership(call: ServiceCall) -> ServiceResponse:
        """adaptive_lighting_helpers.check_ownership

        For each of `entities`, decides whether a write should currently
        be blocked for `owner_id` - the exact same override-protection
        mechanism apply_lighting uses internally (grouping.py's
        EntityLookup.externally_set(), itself a thin wrapper over
        override_protection.classify()/is_blocked()), exposed standalone
        so any caller can ask "should I write this entity" without any
        of this integration's brightness/curve logic at all - see
        docs/HELPERS.md's "Override protection" section for the full
        contract this mirrors.

        Returns: {"results": {entity_id: {"blocked": bool, "status":
        str, "owner_id": str|None, "matched_via": str|None}, ...}} -
        "status" is one of "off", "unclaimed", "pending", "controlled",
        "overridden" (see override_protection.classify()'s own
        docstring); "owner_id" is whichever claim's owner matched, or
        null if none did; "matched_via" is "context" or "value" for a
        "pending"/"controlled" status (null otherwise) - see
        services.yaml for field docs.
        """
        owner_id = call.data.get("owner_id")
        force = call.data["force"]
        brightness_tolerance = call.data["brightness_tolerance"]
        color_temp_tolerance = call.data["color_temp_tolerance"]
        rgb_color_tolerance = call.data["rgb_color_tolerance"]
        results: dict[str, Any] = {}
        for entity_id in call.data["entities"]:
            state = hass.states.get(entity_id)
            confirmed_ctx = write_tracker.confirmed_context_id(entity_id)
            confirmed = (
                {"context_id": confirmed_ctx, "owner_id": write_tracker.confirmed_owner_id(entity_id)}
                if confirmed_ctx is not None
                else None
            )
            pending_ctx = write_tracker.pending_context_id(entity_id)
            pending = (
                {
                    "context_id": pending_ctx,
                    "owner_id": write_tracker.pending_owner_id(entity_id),
                    "target": write_tracker.pending_target(entity_id),
                }
                if pending_ctx is not None
                else None
            )
            status, claim_owner, matched_via = classify(
                state is not None and state.state == "on",
                confirmed,
                pending,
                state.context.id if state is not None else None,
                state.attributes.get("brightness") if state is not None else None,
                state.attributes.get("color_temp_kelvin") if state is not None else None,
                state.attributes.get("rgb_color") if state is not None else None,
                brightness_tolerance,
                color_temp_tolerance,
                rgb_color_tolerance,
            )
            results[entity_id] = {
                "blocked": is_blocked(status, claim_owner, owner_id, force),
                "status": status,
                "owner_id": claim_owner,
                "matched_via": matched_via,
            }
        return {"results": results}

    async def record_ownership(call: ServiceCall) -> ServiceResponse:
        """adaptive_lighting_helpers.record_ownership

        Records that `owner_id` (this call's own context.id, from
        call.context) just wrote `entities`, optionally with what each
        write actually asked for (`targets`) - a thin wrapper around
        write_tracker.async_record(), exposed standalone so a caller
        using check_ownership on its own can also participate in the
        same override-protection bookkeeping apply_lighting uses,
        without going through apply_lighting's brightness/curve logic
        at all. Call this *after* actually issuing whatever write you
        decided on, the same way apply_lighting itself does - recording
        a write that didn't happen would make a later check_ownership
        call see a claim with nothing behind it.

        Returns: {"recorded": [...]} - the entity_ids actually recorded.
        """
        entities = call.data["entities"]
        owner_id = call.data.get("owner_id")
        targets = call.data.get("targets", {})
        live_context_before_write = {
            e: (state.context.id if (state := hass.states.get(e)) is not None else None) for e in entities
        }
        await write_tracker.async_record(entities, live_context_before_write, call.context.id, owner_id, targets=targets)
        return {"recorded": entities}

    async def clear_ownership(call: ServiceCall) -> ServiceResponse:
        """adaptive_lighting_helpers.clear_ownership

        Discards `entities`' tracked confirmed/pending claims entirely -
        the manual escape hatch for a light stuck "overridden" with no
        other way back (see write_tracking.py's async_clear docstring
        for why that can happen on its own for a light that never
        actually went unavailable). The next write to a cleared entity,
        from anyone, is treated exactly like a brand-new entity's first
        write - free to manage, no owner-conflict check possible yet.

        Returns: {"cleared": [...]} - the entity_ids passed through.
        """
        entities = call.data["entities"]
        await write_tracker.async_clear(entities)
        return {"cleared": entities}

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
    hass.services.async_register(
        DOMAIN,
        "check_ownership",
        check_ownership,
        schema=CHECK_OWNERSHIP_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "record_ownership",
        record_ownership,
        schema=RECORD_OWNERSHIP_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "clear_ownership",
        clear_ownership,
        schema=CLEAR_OWNERSHIP_SCHEMA,
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
    hass.services.async_remove(DOMAIN, "check_ownership")
    hass.services.async_remove(DOMAIN, "record_ownership")
    hass.services.async_remove(DOMAIN, "clear_ownership")

    instances = schedule_instances(entry)
    unloaded = await hass.config_entries.async_unload_platforms(entry, SCHEDULE_PLATFORMS)
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    for instance in instances:
        hass.data.get(DOMAIN, {}).pop(instance.subentry_id, None)
    return unloaded

    return True
