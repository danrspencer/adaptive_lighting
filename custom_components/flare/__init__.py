"""
FLARE.

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
actually dispatches light.turn_on/turn_off, taking brightness/colour
targets as plain values - it doesn't read any sensor entity itself; see
docs/blueprint.md's "Bring your own sensor" section for how the
blueprint in this repo extracts those values before calling it.
Optionally also sets up day-phase/curve
sensors (sensor.py), a phase-override select (select.py), and the
schedule/curve config as live entities (time.py, number.py, switch.py)
per named sensor added afterwards (Settings -> Devices & Services ->
FLARE Schedules -> Add schedule sensor) - a native replacement for a
Jinja packages/*.yaml setup - see config_flow.py.

Designed to work with the FLARE blueprint in this repo,
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
# silently clobbered the moment anything imports flare.time
# (any entry with an "at"-less compute_curve call, e.g. every real
# install with at least one schedule instance, plus now every entry
# regardless of instances - see async_forward_entry_setups below), and
# every later time.time() call in this module then raises AttributeError
# against the wrong module. Caught live via a test that unconditionally
# forwards SCHEDULE_PLATFORMS for the first time on a zero-instance entry.
import time as time_module
from pathlib import Path
from types import MappingProxyType
from typing import Any

import voluptuous as vol
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry, ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import Context, HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import slugify

from .const import (
    CONF_ENTRY_TYPE,
    CONF_TARGET,
    DOMAIN,
    ENTRY_TYPE_SCHEDULES,
    ENTRY_TYPE_TRACKING,
    SUBENTRY_TYPE_STATE,
)
from .config_flow import _areas_with_lights
from .coordinator import CURVE_KEYS, ScheduleCoordinator, schedule_instances
from .curve import phase_at, targets_for_phase
from .grouping import EntityLookup, Group, build_groups
from .override_protection import classify, is_blocked
from .scenes import SceneLookup, compute_scene_coverage
from .two_step_check import async_start_watching
from .write_tracking import PRUNE_CHECK_INTERVAL, ClaimRegistry

# One list per entry type - see const.py's CONF_ENTRY_TYPE for why this
# integration installs as two entries rather than one. Both use the
# sensor platform; each platform module decides which entities it owns
# by checking the entry's type.
SCHEDULE_PLATFORMS = [Platform.SENSOR, Platform.SELECT, Platform.NUMBER, Platform.TIME, Platform.SWITCH]
TRACKING_PLATFORMS = [Platform.SENSOR, Platform.BUTTON]


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
        vol.Optional("force", default=False): cv.boolean,
        # None (the default) means "write, but track nothing" - this
        # call's light(s) get no claim and nothing is excluded as
        # already externally-set. vol.Any(None, ...), same reasoning as
        # rgb_color above: the blueprint renders an explicit Jinja None
        # when it can't resolve a scope, not an omitted key.
        vol.Optional("tracking_device_id"): vol.Any(None, cv.string),
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
        # Required, not vol.Optional: this service answers a question
        # about one specific scene - with no candidate scene, the caller
        # already knows the answer (nothing's covered, everything falls
        # to their own default) without asking, the same reasoning as
        # CLAIMS_CHECK_SCHEMA/CLAIMS_RECORD_SCHEMA/CLAIMS_CLEAR_SCHEMA
        # below for tracking_device_id.
        vol.Required("scene_entity_id"): cv.entity_id,
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
        vol.Optional("force", default=False): cv.boolean,
        # None (the default) means "write, but track nothing" - this
        # call's light(s) get no claim and nothing is excluded as
        # already externally-set. vol.Any(None, ...), same reasoning as
        # rgb_color above: the blueprint renders an explicit Jinja None
        # when it can't resolve a scope, not an omitted key.
        vol.Optional("tracking_device_id"): vol.Any(None, cv.string),
    }
)

# These three (claims_check/claims_record/claims_clear - prefixed so
# they sort and group together in Developer Tools -> Actions) exist for
# no reason other than to read or write tracking claims - unlike
# apply_lighting/compute_lighting_groups, which still do something
# useful (dispatch/plan lights) with no scope at all, there is no
# meaningful reason to call any of these three without one. Required,
# not vol.Any(None, ...): a caller with nothing to name shouldn't be
# calling these services in the first place, and a schema-level failure
# is a much louder signal than the previous "always empty, silently" was.
CLAIMS_CHECK_SCHEMA = vol.Schema(
    {
        vol.Required("entities"): [cv.entity_id],
        vol.Required("tracking_device_id"): cv.string,
        vol.Optional("brightness_tolerance", default=2): vol.Coerce(int),
        vol.Optional("color_temp_tolerance", default=10): vol.Coerce(int),
        vol.Optional("rgb_color_tolerance", default=10): vol.Coerce(int),
    }
)

CLAIMS_RECORD_SCHEMA = vol.Schema(
    {
        vol.Required("entities"): [cv.entity_id],
        vol.Required("tracking_device_id"): cv.string,
        vol.Optional("targets", default=dict): dict,
    }
)

CLAIMS_CLEAR_SCHEMA = vol.Schema(
    {
        vol.Required("entities"): [cv.entity_id],
        vol.Required("tracking_device_id"): cv.string,
    }
)


def _build_lookup(hass: HomeAssistant, tracker: ClaimRegistry, subentry_id: str | None) -> EntityLookup:
    """Adapts real HA state/registries to the plain EntityLookup
    interface grouping.py expects - the only HA-specific piece of this
    integration, everything else is the pure modules doing the work.

    subentry_id is the caller's own resolved scope (see
    resolve_scope_device on the tracker), bound into each claim accessor
    as a closure here - the one place a whole call's scope gets fixed
    once and threaded through every entity it looks up. EntityLookup
    itself stays entity_id-only; it has no notion of scope at all."""

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
        observed_context_id=lambda eid: tracker.observed_context_id(subentry_id, eid),
        latest_context_id=lambda eid: tracker.latest_context_id(subentry_id, eid),
        latest_target=lambda eid: tracker.latest_target(subentry_id, eid),
        observed_target=lambda eid: tracker.observed_target(subentry_id, eid),
        latest_secondary_context_id=lambda eid: tracker.latest_secondary_context_id(subentry_id, eid),
        observed_secondary_context_id=lambda eid: tracker.observed_secondary_context_id(subentry_id, eid),
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
) -> tuple[str, str]:
    """Brightness-only call, wait, then brightness + colour - for bulbs
    that can't transition both together (no_combined_transition label).
    Works the same for either colour representation; only the second
    call's colour field differs.

    Each step now gets its own real Context() (parented to the
    triggering apply_lighting call's own context, for logbook
    traceability via that context chain) rather than sharing one - a
    two-step transition genuinely is two separate light.turn_on calls,
    and forcing them to share a single context.id meant a device
    reporting the brightness-only step on its own (a real, expected
    intermediate state for these bulbs, not an anomaly) did so under a
    context that matched neither the final target nor anything else
    write_tracking.py recognised - indistinguishable from a genuine
    external touch. Returns (brightness_context_id, color_context_id)
    so the caller can record *both* against this entity's write-tracking
    claim - see write_tracking.py's async_record docstring for how
    either one landing is recognised as ours."""
    brightness_context = Context(parent_id=context.id)
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": entity_ids, "transition": half_transition, "brightness": brightness},
        blocking=True,
        context=brightness_context,
    )
    await asyncio.sleep(half_transition)
    color_context = Context(parent_id=context.id)
    data = {"entity_id": entity_ids, "transition": half_transition, "brightness": brightness}
    if color_temp_kelvin is not None:
        data["color_temp_kelvin"] = color_temp_kelvin
    else:
        data["rgb_color"] = rgb_color
    await hass.services.async_call("light", "turn_on", data, blocking=True, context=color_context)
    return brightness_context.id, color_context.id


CARD_URL_BASE = "/flare_static"
CARD_JS_PATH = "flare-curve-card.js"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve www/flare-curve-card.js and auto-load it on
    every frontend page - runs once for the whole domain, regardless of
    how many config entries/subentries exist, so the cards ship and
    update with the integration itself (via HACS) rather than needing
    a separate manual Lovelace resource registration step that can
    silently drift out of sync with them (see CLAUDE.md for the live
    incident this replaced). One static path serves the whole www/
    directory, so a second card would need only a second
    add_extra_js_url call, not a second StaticPathConfig.
    cache_headers=False deliberately - neither file has a versioned URL,
    so aggressive caching here would just trade a stale-deployed-file
    bug for a stale-browser-cache one."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL_BASE, str(Path(__file__).parent / "www"), cache_headers=False)]
    )
    add_extra_js_url(hass, f"{CARD_URL_BASE}/{CARD_JS_PATH}")
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """v2 -> v3: split the single entry into schedules and tracking.

    Both kinds of thing used to live under one entry as sibling
    subentries, which HA's integration page renders as one section each
    - so a house with a scope per room got a wall of peers with no
    grouping. An entry is the only level at which the distinction can be
    expressed (see const.py's CONF_ENTRY_TYPE).

    The existing entry becomes the **schedules** one, keeping its sensor
    subentries and, with them, every schedule time and curve value the
    user has set - those are real configuration and are worth preserving.
    Its state subentries are dropped and the tracking entry re-seeds
    equivalents from the same areas: a scope carries only a target, and
    claims aren't persisted at all, so there is nothing there to lose.

    (v1 -> v2 seeded state devices onto the single entry. That still runs
    first for anyone upgrading from v1, and this step then moves them;
    running it is harmless either way, since the seeding is idempotent
    per area and the subentries are about to be replaced.)"""
    if entry.version >= 3:
        return True

    if entry.version < 2:
        for area_id, name in _areas_with_lights(hass):
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType({CONF_TARGET: {"area_id": [area_id]}}),
                    subentry_type=SUBENTRY_TYPE_STATE,
                    title=name,
                    unique_id=slugify(name),
                ),
            )

    for subentry in list(entry.subentries.values()):
        if subentry.subentry_type == SUBENTRY_TYPE_STATE:
            hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)

    hass.config_entries.async_update_entry(
        entry,
        version=3,
        title="FLARE Schedules",
        unique_id=f"{DOMAIN}_{ENTRY_TYPE_SCHEDULES}",
        data={**entry.data, CONF_ENTRY_TYPE: ENTRY_TYPE_SCHEDULES},
    )
    # Created as its own entry rather than here, so it goes through the
    # same flow a fresh install uses and can't drift from it.
    hass.async_create_task(
        hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_IMPORT}, data={})
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    is_tracking = entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_TRACKING

    # Shared across every apply_lighting call, from whichever automation
    # made it - see write_tracking.py for why (grouping.py's
    # externally_set() check only cares "did adaptive control write this
    # most recently", not which specific caller). Deliberately not
    # persisted - claims live on the state devices' tracking entities and
    # die with a restart, which leaves every light manageable. See
    # write_tracking.py's module docstring.
    write_tracker = ClaimRegistry(hass, entry)
    # An entity deleted from HA outright (not just restarting - e.g. a
    # Zigbee2MQTT group removed at the source) never triggers the
    # unavailable-transition cleanup async_start_listening watches for,
    # since hass.states.get(...) just returns None forever with nothing
    # left to observe - see async_prune_stale's own docstring for the
    # live incident (light.extension_spots_left) that prompted this.
    # Called once here (catches anything that went stale while HA was
    # down) and again every PRUNE_CHECK_INTERVAL below (keeps the
    # promise current while running, not just at the next restart).
    if is_tracking:
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
    if is_tracking:
        entry.async_on_unload(async_start_watching(hass, entry))

    async def compute_lighting_groups(call: ServiceCall) -> ServiceResponse:
        """flare.compute_lighting_groups

        Returns: {"groups": [{"multiplier", "brightness", "needing_off",
        "combined", "two_step", "combined_rgb", "two_step_rgb"}, ...]} -
        see services.yaml for field docs.
        """
        rgb_color = call.data.get("rgb_color")
        scope = write_tracker.resolve_scope_device(call.data.get("tracking_device_id"))
        groups = build_groups(
            entities=call.data["entities"],
            brightness_multipliers=call.data["brightness_multipliers"],
            sensor_brightness=call.data["brightness"],
            sensor_color_temp_kelvin=call.data["color_temp_kelvin"],
            lookup=_build_lookup(hass, write_tracker, scope),
            brightness_tolerance=call.data["brightness_tolerance"],
            color_temp_tolerance=call.data["color_temp_tolerance"],
            two_step_label=call.data["two_step_label"],
            prefer_rgb_color=call.data["prefer_rgb_color"],
            rgb_color=tuple(rgb_color) if rgb_color else None,
            rgb_color_tolerance=call.data["rgb_color_tolerance"],
            force=call.data["force"],
        )
        return _groups_response(groups)

    async def compute_curve(call: ServiceCall) -> ServiceResponse:
        """flare.compute_curve

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
        targets = targets_for_phase(phase, at, evening, day, night, morning, **curve_kwargs)
        return {
            "phase": phase,
            "brightness": targets["brightness"],
            "kelvin": targets["kelvin"],
            "rgb_color": list(targets["rgb_color"]),
        }

    async def apply_lighting(call: ServiceCall) -> ServiceResponse:
        """flare.apply_lighting

        Takes brightness/color_temp_kelvin/rgb_color as plain values -
        the same three fields compute_lighting_groups already takes,
        this being the one service in the pair that actually dispatches
        - and turns entities on/off via light.turn_on/turn_off, handling
        reachability, tolerance, externally-set protection, two-step
        transitions, and RGB-vs-colour-temp dispatch internally rather
        than leaving it to the caller. Returns the same {"groups": [...]}
        shape as compute_lighting_groups for introspection, but nothing
        requires capturing it - see services.yaml for field docs.

        tracking_device_id (optional): which FLARE tracking scope this
        write belongs to, for override protection. Omitting it writes
        the light(s) without recording anything - no claim, and nothing
        excluded as already externally-set.

        force (optional, default false): bypasses externally-set
        protection outright for this call. The write is still recorded
        against tracking_device_id if one was given, so a later, non-forced
        call against that same scope correctly recognises it as its own
        rather than finding an orphaned record - the right way to force
        through *and* keep protection working normally afterward. See
        grouping.py's EntityLookup.externally_set() for the full
        semantics of both parameters together.
        """
        force = call.data["force"]
        brightness = call.data["brightness"]
        color_temp_kelvin = call.data["color_temp_kelvin"]
        rgb_color_raw = call.data.get("rgb_color")
        rgb_color = tuple(rgb_color_raw) if rgb_color_raw else None
        scope = write_tracker.resolve_scope_device(call.data.get("tracking_device_id"))
        lookup = _build_lookup(hass, write_tracker, scope)
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
        # records {"state": "off"}: turning a light off is a write like
        # any other, and without a target of its own there would be
        # nothing to tell "we turned this off" apart from "someone else
        # did" once the write's context expires.
        write_targets: dict = {}
        # Two-step entities get their own pair of contexts (see
        # _two_step_turn_on), not call.context - populated below once
        # asyncio.gather resolves, since the contexts don't exist until
        # the calls actually run. context_id_overrides is the colour
        # step's context (the final, complete state); secondary_context_ids
        # is the brightness step's (see write_tracking.py's async_record).
        # Both stay empty for every non-two-step entity, which keeps
        # using call.context.id alone, unchanged.
        context_id_overrides: dict = {}
        secondary_context_ids: dict = {}
        tasks = []
        # (index into tasks, entities) for each two-step dispatch, so the
        # matching (brightness_context_id, color_context_id) can be
        # pulled back out of asyncio.gather's own same-order results -
        # tasks itself is a flat mix of turn_off/turn_on/two-step
        # coroutines, only the latter return anything meaningful.
        two_step_dispatches: list[tuple[int, list]] = []
        for g in groups:
            if g.needing_off:
                written_entities.extend(g.needing_off)
                for e in g.needing_off:
                    write_targets[e] = {"state": "off"}
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
                two_step_dispatches.append((len(tasks), g.two_step))
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
                two_step_dispatches.append((len(tasks), g.two_step_rgb))
                tasks.append(
                    _two_step_turn_on(
                        hass, g.two_step_rgb, g.brightness, half_transition, call.context, rgb_color=rgb_color_list
                    )
                )

        # Snapshotted *before* any of the writes above are dispatched -
        # nothing async has run yet since build_groups() returned (the
        # gather below is the first await point), so this is a true
        # walking-in value. write_tracker needs it to tell whether the
        # *previous* latest write actually landed, which can only be
        # judged against state as it was before this call's own writes -
        # reading it after would risk comparing a light's context against
        # the very write about to be recorded, if it happened to land
        # synchronously. See write_tracking.py's async_record docstring.
        live_context_before_write = {e: lookup.context_id(e) for e in written_entities}

        if tasks:
            results = await asyncio.gather(*tasks)
            for index, entities in two_step_dispatches:
                brightness_context_id, color_context_id = results[index]
                for e in entities:
                    context_id_overrides[e] = color_context_id
                    secondary_context_ids[e] = brightness_context_id
        if written_entities:
            await write_tracker.async_record(
                scope,
                written_entities,
                live_context_before_write,
                call.context.id,
                targets=write_targets,
                secondary_context_ids=secondary_context_ids,
                context_id_overrides=context_id_overrides,
            )

        return _groups_response(groups)

    async def claims_check(call: ServiceCall) -> ServiceResponse:
        """flare.claims_check

        For each of `entities`, decides whether a write should currently
        be blocked - the exact same override-protection mechanism
        apply_lighting uses internally (grouping.py's
        EntityLookup.externally_set(), itself a thin wrapper over
        override_protection.classify()/is_blocked()), exposed standalone
        so any caller can ask "should I write this entity" without any
        of this integration's brightness/curve logic at all - see
        docs/advanced/reference.md's "Override protection" section.

        Takes no `force`: forcing is something a *write* does, and as a
        question it has only one possible answer (is_blocked returns
        False for everything when force is set), so asking it is never
        informative.

        Returns: {"results": {entity_id: {"blocked": bool, "status":
        str, "matched_via": str|None, "scope": str}, ...}}.
        "status" is one of "off", "untracked", "controlled",
        "overridden"; "matched_via" is "latest-context",
        "latest-value", "observed-context" or "observed-value" for a
        "controlled" status and null otherwise; "scope" echoes back
        tracking_device_id's own title. See override_protection.classify()
        and services.yaml.

        tracking_device_id (required): which FLARE tracking scope to check
        against - this service exists only to answer questions about
        tracking, so unlike apply_lighting there's nothing useful to do
        without one.
        """
        brightness_tolerance = call.data["brightness_tolerance"]
        color_temp_tolerance = call.data["color_temp_tolerance"]
        rgb_color_tolerance = call.data["rgb_color_tolerance"]
        scope = write_tracker.resolve_scope_device(call.data["tracking_device_id"])
        scope_title = write_tracker.title_for_scope(scope)
        results: dict[str, Any] = {}
        for entity_id in call.data["entities"]:
            state = hass.states.get(entity_id)
            observed_ctx = write_tracker.observed_context_id(scope, entity_id)
            observed = (
                {
                    "context_id": observed_ctx,
                    "secondary_context_id": write_tracker.observed_secondary_context_id(scope, entity_id),
                    "target": write_tracker.observed_target(scope, entity_id),
                }
                if observed_ctx is not None
                else None
            )
            latest_ctx = write_tracker.latest_context_id(scope, entity_id)
            latest = (
                {
                    "context_id": latest_ctx,
                    "secondary_context_id": write_tracker.latest_secondary_context_id(scope, entity_id),
                    "target": write_tracker.latest_target(scope, entity_id),
                }
                if latest_ctx is not None
                else None
            )
            status, matched_via = classify(
                state is not None and state.state == "on",
                observed,
                latest,
                state.context.id if state is not None else None,
                state.attributes.get("brightness") if state is not None else None,
                state.attributes.get("color_temp_kelvin") if state is not None else None,
                state.attributes.get("rgb_color") if state is not None else None,
                brightness_tolerance,
                color_temp_tolerance,
                rgb_color_tolerance,
            )
            results[entity_id] = {
                "blocked": is_blocked(status),
                "status": status,
                "matched_via": matched_via,
                "scope": scope_title,
            }
        return {"results": results}

    async def claims_record(call: ServiceCall) -> ServiceResponse:
        """flare.claims_record

        Records that this call's own context just wrote `entities`,
        optionally with what each write actually asked for (`targets`) -
        a thin wrapper around write_tracker.async_record(), exposed
        standalone so a caller using claims_check on its own can
        participate in the same bookkeeping apply_lighting uses, without
        going through apply_lighting's brightness/curve logic. Call this
        *after* actually issuing whatever write you decided on, the same
        way apply_lighting does - recording a write that didn't happen
        would make a later claims_check see a claim with nothing behind
        it.

        Returns: {"recorded": [...]} - the entity_ids that were actually
        recorded, which is **not** necessarily everything passed in: the
        scope's tracking entity might not be up yet (see
        write_tracker.async_record's own docstring). Reporting the
        request back verbatim would tell a caller their write was
        tracked when nothing had happened.

        tracking_device_id (required): which FLARE tracking scope to record
        into - this service exists only to write tracking claims, so
        unlike apply_lighting there's nothing useful to do without one.
        """
        entities = call.data["entities"]
        targets = call.data.get("targets", {})
        scope = write_tracker.resolve_scope_device(call.data["tracking_device_id"])
        live_context_before_write = {
            e: (state.context.id if (state := hass.states.get(e)) is not None else None) for e in entities
        }
        await write_tracker.async_record(scope, entities, live_context_before_write, call.context.id, targets=targets)
        tracked = write_tracker.records_for_scope(scope)
        return {"recorded": [e for e in entities if e in tracked]}

    async def claims_clear(call: ServiceCall) -> ServiceResponse:
        """flare.claims_clear

        Discards `entities`' tracked observed/latest claims within
        tracking_device_id - the manual escape hatch for a light stuck
        "overridden" with no other way back (see write_tracking.py's
        async_clear docstring for why that can happen on its own for a
        light that never actually went unavailable). The next write to a
        cleared entity, from anyone, is treated exactly like a
        brand-new entity's first write - free to manage, no conflict
        check possible yet.

        Returns: {"cleared": [...]} - the entity_ids passed through.

        tracking_device_id (required): which FLARE tracking scope to clear
        entities out of - this service exists only to discard tracking
        claims, so unlike apply_lighting there's nothing useful to do
        without one.
        """
        entities = call.data["entities"]
        scope = write_tracker.resolve_scope_device(call.data["tracking_device_id"])
        await write_tracker.async_clear(scope, entities)
        return {"cleared": entities}

    async def compute_scene_coverage_service(call: ServiceCall) -> ServiceResponse:
        """flare.compute_scene_coverage

        Returns: {"scene_active", "scene_valid", "covered_entities",
        "uncovered_entities"} - see services.yaml for field docs.
        """
        result = compute_scene_coverage(
            scene_entity_id=call.data["scene_entity_id"],
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

    # The services belong to the tracking entry: every one of them is
    # about which lights are being driven and by whom, and they need the
    # claim registry this entry owns.
    if is_tracking:
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
            "claims_check",
            claims_check,
            schema=CLAIMS_CHECK_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            DOMAIN,
            "claims_record",
            claims_record,
            schema=CLAIMS_RECORD_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN,
            "claims_clear",
            claims_clear,
            schema=CLAIMS_CLEAR_SCHEMA,
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

    if is_tracking:
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = write_tracker
        _async_remove_legacy_owner_devices(hass, entry)
        await hass.config_entries.async_forward_entry_setups(entry, TRACKING_PLATFORMS)
        return True

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
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_TRACKING:
        for service in (
            "compute_lighting_groups",
            "compute_curve",
            "compute_scene_coverage",
            "apply_lighting",
            "claims_check",
            "claims_record",
            "claims_clear",
        ):
            hass.services.async_remove(DOMAIN, service)
        unloaded = await hass.config_entries.async_unload_platforms(entry, TRACKING_PLATFORMS)
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        return unloaded

    instances = schedule_instances(entry)
    unloaded = await hass.config_entries.async_unload_platforms(entry, SCHEDULE_PLATFORMS)
    for instance in instances:
        hass.data.get(DOMAIN, {}).pop(instance.subentry_id, None)
    return unloaded


def _owner_devices(hass: HomeAssistant, entry: ConfigEntry) -> list[dr.DeviceEntry]:
    """Every per-owner device on this entry - i.e. everything except the
    schedule-instance devices (coordinator.py's ScheduleInstance.
    device_info), which are keyed on a subentry_id instead."""
    return [
        device
        for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        if any(domain == DOMAIN and key.startswith("owner_") for domain, key in device.identifiers)
    ]


@callback
def _async_remove_legacy_owner_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Removes the devices from when tracking scopes were derived from
    the calling automation's entity_id rather than configured.

    Those were created on the fly from a caller-supplied string, which
    is exactly the "devices appearing by magic" this replaced. Nothing
    is lost by deleting them: claims are no longer persisted at all, so
    there is no state in them to preserve, and removing a device takes
    its entities with it."""
    registry = dr.async_get(hass)
    for device in _owner_devices(hass, entry):
        registry.async_remove_device(device.id)
