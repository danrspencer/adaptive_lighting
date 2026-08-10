"""Config flow for Adaptive Lighting Helpers.

The compute_lighting_groups/compute_curve/compute_scene_coverage/
apply_lighting services need no configuration at all - every field below
is optional on the main entry and only matters if you also want the
day-phase/curve sensors and the phase-override select (see sensor.py,
select.py, coordinator.py). Leave them all blank to get just the
services.

Values are plain HH:MM:SS times/plain numbers stored directly on the
config entry - no separate input_datetime helpers to create first (an
earlier version of this integration required that; TIME_FIELDS replaced
the EntitySelector(domain="input_datetime") fields it used to have).
Editable later via Settings -> Devices & Services -> Adaptive Lighting
Helpers -> Configure, which re-runs async_step_reconfigure below with
the current values pre-filled.

Beyond the single main entry, this integration also supports adding any
number of additional named sensors via a config *subentry*
(SensorSubentryFlow below) - each with its own schedule and its own
brightness/Kelvin curve, producing a sensor + phase-override select
prefixed with the sensor's name (see coordinator.py's
ScheduleInstance/schedule_instances()).
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

TIME_FIELDS = {
    vol.Optional("morning_time"): selector.TimeSelector(),
    vol.Optional("day_time"): selector.TimeSelector(),
    vol.Optional("evening_earliest_time"): selector.TimeSelector(),
    vol.Optional("evening_latest_time"): selector.TimeSelector(),
    vol.Optional("night_time"): selector.TimeSelector(),
}

# The 5 time fields above, but required - used by the subentry flow,
# where a user has deliberately chosen to add a named sensor (unlike the
# main entry, where leaving everything blank is a real "services only"
# mode - see module docstring).
REQUIRED_TIME_FIELDS = {
    vol.Required("morning_time"): selector.TimeSelector(),
    vol.Required("day_time"): selector.TimeSelector(),
    vol.Required("evening_earliest_time"): selector.TimeSelector(),
    vol.Required("evening_latest_time"): selector.TimeSelector(),
    vol.Required("night_time"): selector.TimeSelector(),
}

_BRIGHTNESS_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(min=0, max=255, mode=selector.NumberSelectorMode.BOX)
)
_KELVIN_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(min=1000, max=10000, unit_of_measurement="K", mode=selector.NumberSelectorMode.BOX)
)

# Optional on both the main entry and subentries - a schedule (times)
# with no curve customization just reuses curve.py's own defaults.
CURVE_AND_BEHAVIOR_FIELDS = {
    # Only meaningful alongside the time fields (it governs the
    # phase-override select - see select.py). Default False: an override
    # self-clears at the next phase boundary, matching the old Jinja
    # system's behaviour. True keeps an override pinned until manually
    # set back to Auto.
    vol.Optional("sticky_phase_override", default=False): selector.BooleanSelector(),
    vol.Optional("day_brightness"): _BRIGHTNESS_SELECTOR,
    vol.Optional("evening_brightness"): _BRIGHTNESS_SELECTOR,
    vol.Optional("night_brightness"): _BRIGHTNESS_SELECTOR,
    vol.Optional("morning_kelvin"): _KELVIN_SELECTOR,
    vol.Optional("day_end_kelvin"): _KELVIN_SELECTOR,
    vol.Optional("evening_kelvin"): _KELVIN_SELECTOR,
    vol.Optional("night_kelvin"): _KELVIN_SELECTOR,
}

MAIN_ENTRY_FIELDS = {**TIME_FIELDS, **CURVE_AND_BEHAVIOR_FIELDS}
# Schedule/curve fields only, no "name" - shared by both the initial
# subentry form (which adds "name" on top, see SUBENTRY_FIELDS below)
# and the reconfigure form (which deliberately excludes it - renaming
# would change the slugified entity_id prefix, not something a
# reconfigure form should do silently).
SUBENTRY_SCHEDULE_FIELDS = {**REQUIRED_TIME_FIELDS, **CURVE_AND_BEHAVIOR_FIELDS}
SUBENTRY_FIELDS = {vol.Required("name"): selector.TextSelector(), **SUBENTRY_SCHEDULE_FIELDS}


class AdaptiveLightingHelpersConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title="Adaptive Lighting Helpers", data=user_input)

        return self.async_show_form(step_id="user", data_schema=vol.Schema(MAIN_ENTRY_FIELDS))

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            # Not *_reload_and_abort - __init__.py registers its own
            # entry.add_update_listener that reloads on any entry/subentry
            # change (needed regardless for subentry add/remove, which
            # don't go through this flow at all), so the reload happens
            # from there instead. Calling both would double-reload for
            # the main entry, and the subentry equivalent of this method
            # actively raises if an update listener is registered.
            return self.async_update_and_abort(self._get_reconfigure_entry(), data=user_input)

        current = self._get_reconfigure_entry().data
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(MAIN_ENTRY_FIELDS), current),
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_TYPE_SENSOR: SensorSubentryFlow}


class SensorSubentryFlow(ConfigSubentryFlow):
    """Adds one named adaptive lighting sensor - its own schedule and
    (optionally) its own brightness/Kelvin curve. Produces a
    sensor.<slug>_adaptive_lighting + select.<slug>_adaptive_lighting_phase
    pair, namespaced by the slugified name so multiple sensors can
    coexist - see coordinator.py's schedule_instances()."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input["name"]
            unique_id = slugify(name)
            for subentry in self._get_entry().subentries.values():
                if subentry.unique_id == unique_id:
                    errors["name"] = "already_configured"
                    break
            if not errors:
                # "name" becomes the subentry's title, not part of its
                # data - coordinator.py only ever reads the time/curve
                # keys, so storing it twice would just be a stray, unused
                # field a future maintainer could mistake for meaningful.
                data = {k: v for k, v in user_input.items() if k != "name"}
                return self.async_create_entry(title=name, unique_id=unique_id, data=data)

        return self.async_show_form(step_id="user", data_schema=vol.Schema(SUBENTRY_FIELDS), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if user_input is not None:
            # async_update_and_abort, not *_reload_and_abort - see the
            # matching comment on the main entry's async_step_reconfigure.
            return self.async_update_and_abort(self._get_entry(), self._get_reconfigure_subentry(), data=user_input)

        current = self._get_reconfigure_subentry().data
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(SUBENTRY_SCHEDULE_FIELDS), current),
        )
