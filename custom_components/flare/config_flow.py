"""Config flow for FLARE.

The main entry needs no configuration at all - adding it just registers
the compute_lighting_groups/compute_curve/compute_scene_coverage/
apply_lighting services (see __init__.py). No sensor is auto-created;
every day-phase/curve sensor + phase-override select is a "sensor"
subentry (SensorSubentryFlow below), added explicitly from this
integration's own page (Add Sensor) - one mechanism for every sensor,
you name it yourself from the start.

Deliberately does NOT auto-seed a first sensor the way earlier versions
did (used to be hardcoded to the name "Default", regardless of what a
user typed anywhere, since there was nowhere to type a name at all for
it). Two real problems with that, not just a naming quibble: (1) HA's
own "integration added" dialog shows an unconditional rename+area-picker
form whenever a config flow creates a device - not something an
integration can suppress - so adding this integration always popped up
that form for a device the user hadn't asked to create yet. (2)
Renaming that device later only ever changes its *displayed* name
(HA's own entity-id auto-rename only happens once, in that first-run
dialog, never again) - so the "Default" entity_id prefix was permanent
regardless of what the device got renamed to, which is exactly the
confusion this change avoids by never creating anything unnamed in the
first place.

A subentry only ever asks for one thing: a name. It becomes both the
sensor's device name (Settings -> Devices, renamable later) and its
entity_id prefix (sensor.living_room_flare etc) - see
coordinator.py's ScheduleInstance/schedule_instances(). Everything else
- the five schedule times and the eight brightness/Kelvin curve values
- is a real HA entity on that device instead of a config-flow field
(time.py/number.py), each starting at a sensible default
(curve.DEFAULT_SCHEDULE_HOURS/DEFAULT_CURVE_VALUES) and adjustable at
any time with no reconfigure flow needed - direct, discoverable, and
automatable, rather than hidden behind a form only reachable via
Configure.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigSubentryFlow, SubentryFlowResult
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.util import slugify

from .const import (
    CONF_ENTRY_TYPE,
    CONF_TARGET,
    CONF_TWO_STEP_MODELS,
    DOMAIN,
    ENTRY_TYPE_SCHEDULES,
    ENTRY_TYPE_TRACKING,
    SUBENTRY_TYPE_SENSOR,
    SUBENTRY_TYPE_STATE,
)
from .two_step import DEFAULT_TWO_STEP_MODEL_PATTERNS

SUBENTRY_FIELDS = {vol.Required("name"): selector.TextSelector()}


class AdaptiveLightingHelpersConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 3

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Two entries, not one: schedules and tracking are different
        kinds of thing, and an entry is the only level at which that
        distinction can be shown. HA's integration page renders one
        section per subentry with no way to group them by type, so
        keeping both under a single entry flattens them into one long
        list of siblings."""
        return self.async_show_menu(step_id="user", menu_options=[ENTRY_TYPE_SCHEDULES, ENTRY_TYPE_TRACKING])

    async def async_step_schedules(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Nothing to ask for. Add the day-phase/curve sensors
        themselves afterwards from this entry's own page."""
        await self.async_set_unique_id(f"{DOMAIN}_{ENTRY_TYPE_SCHEDULES}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="FLARE Schedules", data={CONF_ENTRY_TYPE: ENTRY_TYPE_SCHEDULES}
        )

    async def async_step_tracking(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Offers a state device per area that currently contains a
        light, pre-selected, and creates them with the entry.

        Pre-selected rather than empty because a room is the unit almost
        everyone wants to track by, and an unticked list is a wall of
        work before anything does anything. Trimmable, and skippable
        entirely - nothing here is required, and more can be added later
        from this entry's own page.

        Areas with no lights are left out: a state device that can never
        resolve anything is just an empty device to wonder about."""
        await self.async_set_unique_id(f"{DOMAIN}_{ENTRY_TYPE_TRACKING}")
        self._abort_if_unique_id_configured()

        areas = _areas_with_lights(self.hass)
        if user_input is not None or not areas:
            chosen = (user_input or {}).get("areas", [])
            return self._create_tracking_entry([(a, n) for a, n in areas if a in chosen])

        return self.async_show_form(
            step_id="tracking",
            data_schema=vol.Schema(
                {
                    vol.Optional("areas", default=[area_id for area_id, _ in areas]): selector.AreaSelector(
                        selector.AreaSelectorConfig(multiple=True)
                    )
                }
            ),
        )

    async def async_step_import(self, import_data: dict[str, Any]) -> FlowResult:
        """Creates the tracking entry during the v2 -> v3 split, where
        there is nobody to show a form to - see __init__.py's
        async_migrate_entry."""
        await self.async_set_unique_id(f"{DOMAIN}_{ENTRY_TYPE_TRACKING}")
        self._abort_if_unique_id_configured()
        return self._create_tracking_entry(_areas_with_lights(self.hass))

    def _create_tracking_entry(self, areas: list[tuple[str, str]]) -> FlowResult:
        return self.async_create_entry(
            title="FLARE Tracking",
            data={CONF_ENTRY_TYPE: ENTRY_TYPE_TRACKING},
            subentries=[
                {
                    "subentry_type": SUBENTRY_TYPE_STATE,
                    "title": name,
                    "unique_id": slugify(name),
                    "data": {CONF_TARGET: {"area_id": [area_id]}},
                }
                for area_id, name in areas
            ],
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        # Each entry offers only its own kind, which is the whole point
        # of splitting them.
        if config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_TRACKING:
            return {SUBENTRY_TYPE_STATE: StateSubentryFlow}
        return {SUBENTRY_TYPE_SENSOR: SensorSubentryFlow}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> config_entries.OptionsFlow:
        return AdaptiveLightingHelpersOptionsFlow()


class AdaptiveLightingHelpersOptionsFlow(config_entries.OptionsFlow):
    """The install-wide setting: which bulb models need two-step
    transitions.

    Kept on the main entry rather than per sensor because it describes
    hardware, not a schedule - which bulbs in this house can't take a
    combined brightness+colour command. Nothing here affects the curve
    or any room's behaviour; it only decides what the missing-label
    repair looks for (see two_step.py).

    The field is pre-populated with the shipped defaults rather than
    being an "extras" box layered on top of a hidden list, so what's in
    the box is exactly what runs: a pattern can be removed as easily as
    added, with no invisible half to reason about."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        # Shows the user's own list once saved, otherwise seeds the box
        # with the shipped defaults so the first thing they see is the
        # real, complete list rather than an empty field.
        current = self.config_entry.options.get(CONF_TWO_STEP_MODELS)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Optional(CONF_TWO_STEP_MODELS, default=""): selector.TextSelector(
                            selector.TextSelectorConfig(multiline=True)
                        ),
                    }
                ),
                {
                    CONF_TWO_STEP_MODELS: current or "\n".join(DEFAULT_TWO_STEP_MODEL_PATTERNS),
                },
            ),
        )


class SensorSubentryFlow(ConfigSubentryFlow):
    """Adds one adaptive lighting sensor. Produces a device named after
    it, containing sensor.<slug>_flare +
    sensor.<slug>_flare_curve + select.<slug>_flare_phase
    + the schedule/curve config entities (time.py/number.py/switch.py),
    namespaced by the slugified name so multiple sensors can coexist -
    see coordinator.py's schedule_instances(). No reconfigure flow -
    there's nothing left to reconfigure once the name is set; the
    schedule/curve entities are edited directly, live."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input["name"].strip()
            slug = slugify(name)
            # Compares slugified titles rather than trusting stored
            # unique_ids directly - matches coordinator.py's own prefix
            # derivation exactly, so this can't disagree with what
            # schedule_instances() would actually consider a collision.
            for subentry in self._get_entry().subentries.values():
                if slugify(subentry.title) == slug:
                    errors["name"] = "already_configured"
                    break
            if not errors:
                return self.async_create_entry(title=name, unique_id=slug or None, data={})

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(SUBENTRY_FIELDS), user_input or {}),
            errors=errors,
        )


def _areas_with_lights(hass) -> list[tuple[str, str]]:
    """(area_id, name) for every area a light entity resolves to, by the
    entity's own area or its device's - the same fallback
    write_tracking.py's scope_for uses, so what's offered here is
    exactly what would resolve later."""
    from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    area_ids = set()
    for entry in entity_registry.entities.values():
        if entry.domain != "light":
            continue
        area_id = entry.area_id
        if area_id is None and entry.device_id:
            device = device_registry.async_get(entry.device_id)
            area_id = device.area_id if device else None
        if area_id:
            area_ids.add(area_id)
    area_registry = ar.async_get(hass)
    named = []
    for area_id in area_ids:
        area = area_registry.async_get_area(area_id)
        if area is not None:
            named.append((area_id, area.name))
    return sorted(named, key=lambda pair: pair[1])


class StateSubentryFlow(ConfigSubentryFlow):
    """Adds one state device - a named tracking scope owning the
    override-protection claims for whatever lights its target covers.

    The target is the same area/device/entity shape the blueprint's
    room_target uses, so "the kitchen" means the same thing in both
    places. Most specific match wins when scopes overlap; a light
    matching nothing is simply not tracked - see write_tracking.py's
    scope_for."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self._async_form(user_input)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Retargeting matters more here than for a schedule: rooms get
        rearranged, and a scope you can't repoint would have to be
        deleted and recreated, losing its history."""
        return await self._async_form(user_input, reconfigure=self._get_reconfigure_subentry())

    async def _async_form(self, user_input, reconfigure=None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input["name"].strip()
            slug = slugify(name)
            for subentry_id, subentry in self._get_entry().subentries.items():
                if reconfigure is not None and subentry_id == reconfigure.subentry_id:
                    continue
                if slugify(subentry.title) == slug:
                    errors["name"] = "already_configured"
                    break
            if not errors:
                data = {CONF_TARGET: user_input.get(CONF_TARGET) or {}}
                if reconfigure is not None:
                    return self.async_update_and_abort(
                        self._get_entry(), reconfigure, title=name, data=data
                    )
                return self.async_create_entry(title=name, unique_id=slug or None, data=data)

        suggested = user_input or (
            {"name": reconfigure.title, CONF_TARGET: reconfigure.data.get(CONF_TARGET, {})}
            if reconfigure
            else {}
        )
        return self.async_show_form(
            step_id="reconfigure" if reconfigure else "user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        **SUBENTRY_FIELDS,
                        # Lesson 14: under a target selector the filter
                        # list goes directly under `entity:`, with no
                        # nested `filter:` key.
                        vol.Optional(CONF_TARGET): selector.TargetSelector(
                            selector.TargetSelectorConfig(entity=[{"domain": "light"}])
                        ),
                    }
                ),
                suggested,
            ),
            errors=errors,
        )
