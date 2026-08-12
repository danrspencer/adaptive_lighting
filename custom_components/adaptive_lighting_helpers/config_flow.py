"""Config flow for Adaptive Lighting Helpers.

The main entry needs no configuration at all - adding it just registers
the compute_lighting_groups/compute_curve/compute_scene_coverage/
apply_lighting services (see __init__.py), plus a single default
"sensor" subentry named "Default" so there's something usable
immediately rather than an empty integration you have to remember to
add a sensor to. Every day-phase/curve sensor + phase-override select
beyond that is a "sensor" subentry too (SensorSubentryFlow below),
added afterwards from this integration's own page - one mechanism for
every sensor, not a separate main-entry path alongside named ones.

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

from .const import DOMAIN, SUBENTRY_TYPE_SENSOR

SUBENTRY_FIELDS = {vol.Required("name"): selector.TextSelector()}

DEFAULT_SENSOR_NAME = "Default"


class AdaptiveLightingHelpersConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        # No fields to ask for - see module docstring. Creates
        # immediately rather than showing an empty form to click through,
        # seeding one sensor named "Default" so there's something usable
        # right away.
        return self.async_create_entry(
            title="Adaptive Lighting Helpers",
            data={},
            subentries=[
                {
                    "subentry_type": SUBENTRY_TYPE_SENSOR,
                    "title": DEFAULT_SENSOR_NAME,
                    "unique_id": slugify(DEFAULT_SENSOR_NAME),
                    "data": {},
                }
            ],
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_TYPE_SENSOR: SensorSubentryFlow}


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
