"""Config flow for Adaptive Lighting Helpers.

The main entry needs no configuration at all - adding it just registers
the compute_lighting_groups/compute_curve/compute_scene_coverage/
apply_lighting services (see __init__.py), plus a single default
"sensor" subentry named "Default" (DEFAULT_SENSOR_TIMES, curve.py's own
brightness/Kelvin defaults) so there's something usable immediately
rather than an empty integration you have to remember to add a sensor
to. Every day-phase/curve sensor + phase-override select beyond that is
a "sensor" subentry too (SensorSubentryFlow below), added afterwards
from this integration's own page - one mechanism for every sensor, not
a separate main-entry path alongside named ones. A subentry's name is
required and becomes both its device's name (Settings -> Devices,
renamable later) and its entity_id prefix (sensor.living_room_adaptive_lighting
etc) - see coordinator.py's ScheduleInstance/schedule_instances().

Subentry values are plain HH:MM:SS times/plain numbers stored directly
on the subentry - no separate input_datetime helpers to create first
(an earlier version of this integration required that; TIME_FIELDS
replaced the EntitySelector(domain="input_datetime") fields it used to
have).
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
from .coordinator import CURVE_KEYS, TIME_KEYS
from .curve import DEFAULT_CURVE_VALUES, DEFAULT_SCHEDULE_HOURS

# Required - a subentry only exists because a user deliberately chose to
# add a named sensor, so there's no "leave blank to skip" mode here the
# way the old main-entry schedule had. Built from TIME_KEYS (coordinator.py)
# rather than listing the 5 names again, so the schema and the internal
# key list can't drift apart.
TIME_FIELDS = {vol.Required(key): selector.TimeSelector() for key in TIME_KEYS}

_BRIGHTNESS_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(min=0, max=255, mode=selector.NumberSelectorMode.BOX)
)
_KELVIN_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(min=1000, max=10000, unit_of_measurement="K", mode=selector.NumberSelectorMode.BOX)
)

# Optional - a schedule with no curve customization just reuses
# curve.py's own defaults, shown here (default=...) rather than left
# blank, so the form always shows what a field left untouched actually
# does. Built from CURVE_KEYS (coordinator.py) rather than listing the
# 8 names again - same reasoning as TIME_FIELDS above; CURVE_KEYS's own
# order (grouped by phase: Morning brightness+Kelvin, Day, Evening,
# Night) is what drives the field order here too. sticky_phase_override
# goes last - it's a behaviour toggle, not part of the curve itself.
CURVE_AND_BEHAVIOR_FIELDS = {
    **{
        vol.Optional(key, default=DEFAULT_CURVE_VALUES[key]): (
            _BRIGHTNESS_SELECTOR if key.endswith("_brightness") else _KELVIN_SELECTOR
        )
        for key in CURVE_KEYS
    },
    # Governs the phase-override select - see select.py. Default False:
    # an override self-clears at the next phase boundary, matching the
    # old Jinja system's behaviour. True keeps an override pinned until
    # manually set back to Auto.
    vol.Optional("sticky_phase_override", default=False): selector.BooleanSelector(),
}

# Schedule/curve fields only, no "name" - shared by both the initial
# subentry form (which adds "name" on top, see SUBENTRY_FIELDS below)
# and the reconfigure form (which deliberately excludes it - renaming
# would change the slugified entity_id prefix, not something a
# reconfigure form should do silently).
SUBENTRY_SCHEDULE_FIELDS = {**TIME_FIELDS, **CURVE_AND_BEHAVIOR_FIELDS}
SUBENTRY_FIELDS = {vol.Required("name"): selector.TextSelector(), **SUBENTRY_SCHEDULE_FIELDS}

DEFAULT_SENSOR_NAME = "Default"

# Seeded onto the single default sensor subentry created alongside the
# main entry (see async_step_user below). Derived from curve.py's
# DEFAULT_SCHEDULE_HOURS - the same numbers dashboard/generate_preview_data.py
# uses for its synthetic preview, both reading the one shared constant
# rather than each keeping its own copy of "6, 8, 17, 20, 22". Curve
# values are deliberately left unset here so they fall through to
# curve.py's own defaults, same as any other sensor with nothing
# configured.
DEFAULT_SENSOR_TIMES = {f"{key}_time": f"{hour:02d}:00:00" for key, hour in DEFAULT_SCHEDULE_HOURS.items()}


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
                    "data": DEFAULT_SENSOR_TIMES,
                }
            ],
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_TYPE_SENSOR: SensorSubentryFlow}


class SensorSubentryFlow(ConfigSubentryFlow):
    """Adds one adaptive lighting sensor - its own schedule and
    (optionally) its own brightness/Kelvin curve. Produces a device
    named after it, containing sensor.<slug>_adaptive_lighting +
    sensor.<slug>_adaptive_lighting_curve + select.<slug>_adaptive_lighting_phase,
    namespaced by the slugified name so multiple sensors can coexist -
    see coordinator.py's schedule_instances()."""

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
                # "name" becomes the subentry's title, not part of its
                # data - coordinator.py only ever reads the time/curve
                # keys, so storing it twice would just be a stray, unused
                # field a future maintainer could mistake for meaningful.
                data = {k: v for k, v in user_input.items() if k != "name"}
                return self.async_create_entry(title=name, unique_id=slug or None, data=data)

        # Pre-fill schedule/curve values from the most recently added
        # sibling sensor, if any - a new sensor most often wants "the
        # same schedule as my other one, just named", not a blank form.
        # Re-showing after a validation error (a clashing name) instead
        # re-suggests whatever was just submitted, so fixing the name
        # doesn't cost the rest of the form.
        if user_input is not None:
            suggested = user_input
        else:
            existing = list(self._get_entry().subentries.values())
            suggested = existing[-1].data if existing else {}
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(SUBENTRY_FIELDS), suggested),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if user_input is not None:
            # async_update_and_abort, not *_reload_and_abort - __init__.py
            # registers its own entry.add_update_listener that reloads on
            # any entry/subentry change (needed regardless for subentry
            # add/remove, which don't go through this flow at all), so the
            # reload happens from there instead. Calling both would
            # double-reload, and this method actively raises if an update
            # listener is already registered.
            return self.async_update_and_abort(self._get_entry(), self._get_reconfigure_subentry(), data=user_input)

        current = self._get_reconfigure_subentry().data
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(SUBENTRY_SCHEDULE_FIELDS), current),
        )
