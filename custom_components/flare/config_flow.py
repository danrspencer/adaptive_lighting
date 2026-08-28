"""Config flow for FLARE.

Adding the integration once creates both entries - Schedules and
Tracking (async_step_user explains why it is still two entries, and
why only one of them is created visibly). Neither asks for anything
beyond which rooms to track. No sensor is auto-created; every
day-phase/curve sensor + phase-override select is a "sensor" subentry
(SensorSubentryFlow below), added explicitly from the Schedules entry's
own page (Add Sensor) - one mechanism for every sensor, you name it
yourself from the start.

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
from homeassistant.config_entries import (
    SOURCE_IMPORT,
    ConfigEntry,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
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
        """One "Add FLARE" creates both entries.

        They stay two entries, not one: schedules and tracking are
        different kinds of thing, and an entry is the only level at
        which that distinction can be drawn - HA's integration page
        renders one section per subentry with no way to group them by
        type, so a single entry flattens schedule sensors and state
        devices into one long list of siblings. Nothing in that
        argument asked anyone to walk through Add Integration twice to
        get there, though, so this step asks the one thing there is to
        ask - which rooms to track - and creates both.

        Each half is still creatable on its own, so deleting one and
        adding it back works rather than aborting on the other's
        account.

        The entry this flow creates *visibly* is always Schedules,
        because Schedules creates no devices. HA's "integration added"
        dialog (step-flow-create-entry.ts) shows a device-rename + area
        picker for every device belonging to the completing flow's
        entry, and an integration has no way to suppress it - while
        Tracking seeds a state device per room, which is exactly the
        pile of rename prompts for things nobody named that the dialog
        would turn into. Raised through SOURCE_IMPORT instead, it has
        no visible flow for one to attach to.
        """
        configured = {entry.data.get(CONF_ENTRY_TYPE) for entry in self._async_current_entries()}
        needs_schedules = ENTRY_TYPE_SCHEDULES not in configured
        needs_tracking = ENTRY_TYPE_TRACKING not in configured

        # Offers a state device per area that currently holds a light,
        # pre-selected rather than empty because a room is the unit
        # almost everyone wants to track by, and an unticked list is a
        # wall of work before anything does anything. Trimmable, and
        # skippable entirely - nothing here is required, and more can
        # be added later from the entry's own page. Areas with no
        # lights are left out: a state device that can never resolve
        # anything is just an empty device to wonder about.
        areas = _areas_with_lights(self.hass) if needs_tracking else []
        if areas and user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Optional("areas", default=[area_id for area_id, _ in areas]): selector.AreaSelector(
                            selector.AreaSelectorConfig(multiple=True)
                        )
                    }
                ),
            )

        # With nothing left to create both branches below fall through to
        # _create_schedules_entry, whose unique_id guard aborts with
        # already_configured - the same guard that stops either half being
        # duplicated. An explicit abort here would be a second way to reach
        # an outcome that one already covers.
        chosen = [(a, n) for a, n in areas if a in (user_input or {}).get("areas", [])]
        if needs_tracking and not needs_schedules:
            return await self._create_tracking_entry(chosen)
        if needs_tracking:
            await self.hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data={"areas": [area_id for area_id, _ in chosen]},
            )
        return await self._create_schedules_entry()

    async def async_step_import(self, import_data: dict[str, Any]) -> FlowResult:
        """Creates the tracking entry where there is nobody to show a
        form to: from async_step_user (see the dialog note there), and
        from the v2 -> v3 split in __init__.py's async_migrate_entry,
        which passes no "areas" key at all and so seeds every room that
        has a light."""
        areas = _areas_with_lights(self.hass)
        if (chosen := import_data.get("areas")) is not None:
            areas = [(a, n) for a, n in areas if a in chosen]
        return await self._create_tracking_entry(areas)

    async def _create_schedules_entry(self) -> FlowResult:
        """Nothing to ask for. The day-phase/curve sensors themselves
        are added afterwards from this entry's own page."""
        await self.async_set_unique_id(f"{DOMAIN}_{ENTRY_TYPE_SCHEDULES}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="FLARE Schedules", data={CONF_ENTRY_TYPE: ENTRY_TYPE_SCHEDULES}
        )

    async def _create_tracking_entry(self, areas: list[tuple[str, str]]) -> FlowResult:
        await self.async_set_unique_id(f"{DOMAIN}_{ENTRY_TYPE_TRACKING}")
        self._abort_if_unique_id_configured()
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
    entity's own area or its device's - just to size the setup offer to
    areas that actually have something to track; not used to resolve
    claims (see StateSubentryFlow's own docstring)."""
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
    """Adds one state device - a named, empty tracking scope. Nothing
    about which lights it tracks is decided here: a caller (typically
    the blueprint, via room_target) states a scope explicitly on each
    apply_lighting/claims_record/etc call by passing this device's own
    tracking_device_id - see write_tracking.py's module docstring.

    The target asked for below decides only where this device's own
    entry lands in the Area registry (sensor.py's _assign_scope_area,
    area-only, best-effort) - it plays no part in which lights get
    tracked. It's the same area/device/entity shape the blueprint's
    room_target uses only so "the kitchen" reads the same way in both
    places, not because the two are wired together."""

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
