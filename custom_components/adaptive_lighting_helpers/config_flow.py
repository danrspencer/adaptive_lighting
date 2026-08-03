"""Config flow for Adaptive Lighting Helpers.

Single instance. The compute_lighting_groups/compute_curve services
need no configuration at all - the five schedule fields below are
optional and only matter if you also want the day-phase/curve sensors
(morning_start, day_start, evening_start, night_start, day_phase,
adaptive_lighting_curve, solar_adaptive_lighting_brightness/
color_temperature). Leave them blank to get just the services.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import DOMAIN

SCHEDULE_FIELDS = {
    vol.Optional("morning_entity"): selector.EntitySelector(selector.EntitySelectorConfig(domain="input_datetime")),
    vol.Optional("day_entity"): selector.EntitySelector(selector.EntitySelectorConfig(domain="input_datetime")),
    vol.Optional("evening_earliest_entity"): selector.EntitySelector(
        selector.EntitySelectorConfig(domain="input_datetime")
    ),
    vol.Optional("evening_latest_entity"): selector.EntitySelector(
        selector.EntitySelectorConfig(domain="input_datetime")
    ),
    vol.Optional("night_entity"): selector.EntitySelector(selector.EntitySelectorConfig(domain="input_datetime")),
}


class AdaptiveLightingHelpersConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title="Adaptive Lighting Helpers", data=user_input)

        return self.async_show_form(step_id="user", data_schema=vol.Schema(SCHEDULE_FIELDS))
