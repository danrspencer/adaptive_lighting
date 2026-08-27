"""Config flow for Adaptive Lighting Helpers.

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
entity_id prefix (sensor.living_room_adaptive_lighting etc) - see
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

from .const import CONF_OWNER_SENSORS, CONF_TWO_STEP_MODELS, DOMAIN, SUBENTRY_TYPE_SENSOR
from .two_step import DEFAULT_TWO_STEP_MODEL_PATTERNS

SUBENTRY_FIELDS = {vol.Required("name"): selector.TextSelector()}


class AdaptiveLightingHelpersConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        # No fields to ask for, no subentry seeded - see module
        # docstring. Creates immediately rather than showing an empty
        # form to click through; add a named sensor afterwards via this
        # integration's own page (Add Sensor).
        return self.async_create_entry(title="Adaptive Lighting Helpers", data={})

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_TYPE_SENSOR: SensorSubentryFlow}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> config_entries.OptionsFlow:
        return AdaptiveLightingHelpersOptionsFlow()


class AdaptiveLightingHelpersOptionsFlow(config_entries.OptionsFlow):
    """The install-wide settings: which bulb models need two-step
    transitions, and whether to create the optional per-owner count
    sensors.

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
                        vol.Optional(CONF_OWNER_SENSORS, default=False): selector.BooleanSelector(),
                    }
                ),
                {
                    CONF_TWO_STEP_MODELS: current or "\n".join(DEFAULT_TWO_STEP_MODEL_PATTERNS),
                    CONF_OWNER_SENSORS: self.config_entry.options.get(CONF_OWNER_SENSORS, False),
                },
            ),
        )


class SensorSubentryFlow(ConfigSubentryFlow):
    """Adds one adaptive lighting sensor. Produces a device named after
    it, containing sensor.<slug>_adaptive_lighting +
    sensor.<slug>_adaptive_lighting_curve + select.<slug>_adaptive_lighting_phase
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
