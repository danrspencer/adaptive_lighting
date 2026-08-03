"""Config flow for Adaptive Lighting Helpers.

Single-instance, no options - there's nothing to configure, this just
registers the services (see __init__.py). The flow exists only because
HACS/modern HA expect an integration to be set up via Settings ->
Devices & Services rather than a bare YAML domain key.
"""

from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN


class AdaptiveLightingHelpersConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title="Adaptive Lighting Helpers", data={})

        return self.async_show_form(step_id="user")
