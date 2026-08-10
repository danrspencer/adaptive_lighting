"""Config flow for Adaptive Lighting Helpers.

Single instance. The compute_lighting_groups/compute_curve/
compute_scene_coverage services need no configuration at all - the
five time fields below are optional and only matter if you also want
the day-phase/curve sensors and the phase-override select (see
sensor.py, select.py, coordinator.py). Leave them blank to get just
the services.

Values are plain HH:MM:SS times stored directly on the config entry -
no separate input_datetime helpers to create first (an earlier version
of this integration required that; TIME_FIELDS replaced the
EntitySelector(domain="input_datetime") fields it used to have).
Editable later via Settings -> Devices & Services -> Adaptive Lighting
Helpers -> Configure, which re-runs async_step_reconfigure below with
the current values pre-filled.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import DOMAIN

TIME_FIELDS = {
    vol.Optional("morning_time"): selector.TimeSelector(),
    vol.Optional("day_time"): selector.TimeSelector(),
    vol.Optional("evening_earliest_time"): selector.TimeSelector(),
    vol.Optional("evening_latest_time"): selector.TimeSelector(),
    vol.Optional("night_time"): selector.TimeSelector(),
    # Only meaningful alongside the times above (it governs
    # select.adaptive_lighting_phase - see select.py). Default False:
    # an override self-clears at the next phase boundary, matching the
    # old Jinja system's behaviour. True keeps an override pinned until
    # manually set back to Auto.
    vol.Optional("sticky_phase_override", default=False): selector.BooleanSelector(),
}


class AdaptiveLightingHelpersConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title="Adaptive Lighting Helpers", data=user_input)

        return self.async_show_form(step_id="user", data_schema=vol.Schema(TIME_FIELDS))

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_update_reload_and_abort(self._get_reconfigure_entry(), data=user_input)

        current = self._get_reconfigure_entry().data
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(TIME_FIELDS), current),
        )
