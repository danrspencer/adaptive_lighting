"""
Fix flow for the "bulbs missing their no_combined_transition label"
repair raised by two_step_check.py.

Home Assistant looks for this module by name (`repairs.py`) on the
integration and calls async_create_fix_flow() when the user presses Fix
on the issue - there's no registration step for it beyond existing.

The flow is one confirmation step rather than a silent auto-apply. The
fix writes into the label registry, which is the user's own data and
shared with everything else in their install, so it asks first - but the
asking is the only manual part: the user never has to find the right
label, type its id correctly, or work out which bulbs need it, which is
the whole failure mode the repair exists to catch.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .two_step import TWO_STEP_LABEL_NAME, describe
from .two_step_check import ISSUE_ID, async_apply_label, unlabelled_lights


class MissingTwoStepLabelRepairFlow(RepairsFlow):
    """Applies the label to every bulb currently detected as missing it."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            # The integration was removed between the issue being raised
            # and the user pressing Fix - nothing left to label against.
            return self.async_abort(reason="entry_gone")

        if user_input is not None:
            labelled = async_apply_label(self.hass, entry)
            return self.async_create_entry(title="", data={"labelled": labelled})

        missing = unlabelled_lights(self.hass, entry)
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "count": str(len(missing)),
                "lights": describe(missing),
                "label": TWO_STEP_LABEL_NAME,
            },
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Called by Home Assistant when the user presses Fix on our issue."""
    entries = hass.config_entries.async_entries(DOMAIN)
    entry_id = entries[0].entry_id if entries else ""
    if issue_id == ISSUE_ID:
        return MissingTwoStepLabelRepairFlow(entry_id)
    raise ValueError(f"Unknown repair issue for {DOMAIN}: {issue_id}")
