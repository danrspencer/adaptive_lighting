"""
Manual override for the computed day phase - see coordinator.py for
how this feeds back into brightness/colour-temperature.

select.<prefix>flare_phase, options Auto/Morning/Day/Evening/Night,
default Auto - one per schedule instance (coordinator.py's
ScheduleInstance/schedule_instances), set up alongside that instance's
day-phase/curve sensors (sensor.py).

This is the write side of what used to be a single dual-purpose entity
in the live Jinja setup (input_select.day_phase, which downstream
sensors trusted blindly and a person could also click to override) -
splitting read (sensor.adaptive_lighting's phase) from write (this
entity) is deliberate: the old design's docstring-noted wart ("the
precomputed curve doesn't follow a manual override, so they can
disagree") was a direct symptom of one entity trying to be both.

RestoreEntity so an override survives a restart rather than silently
reverting to Auto - matching the rest of this integration's "check
live state fresh, don't invent hidden expiry" style (see
grouping.py's externally_set()), just for the select's own state instead
of a light's.

Self-correcting by default, matching the old system's behaviour: pin
the phase to something other than what's currently computed (e.g.
override Evening -> Day) and it holds there, but only until the
*schedule itself* next moves on (i.e. when computed_phase next changes
from whatever it was at override time) - at that point the override
clears back to Auto on its own, rather than staying pinned forever, so
overriding "Day" during Evening still ends up at Night once Evening
would naturally have ended, not stuck on Day indefinitely. Toggle
switch.<prefix>sticky_phase_override (see switch.py) to disable this
and keep an override until manually cleared instead - read fresh off
that entity's live state each time, not a cached value, the same "check
now, don't remember" style everything else here already uses.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, PHASE_OPTIONS
from .coordinator import ScheduleCoordinator, ScheduleInstance, schedule_instances


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    for instance in schedule_instances(entry):
        coordinator: ScheduleCoordinator = hass.data[DOMAIN][instance.subentry_id]
        async_add_entities([_PhaseOverrideSelect(coordinator, instance)], config_subentry_id=instance.subentry_id)


class _PhaseOverrideSelect(CoordinatorEntity[ScheduleCoordinator], SelectEntity, RestoreEntity):
    _attr_icon = "mdi:sun-clock"
    _attr_options = PHASE_OPTIONS
    _attr_has_entity_name = True
    _attr_name = "Phase"

    def __init__(self, coordinator: ScheduleCoordinator, instance: ScheduleInstance) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{instance.subentry_id}_phase_override"
        self.entity_id = f"select.{instance.prefix}flare_phase"
        # Every instance gets a device (coordinator.py's
        # ScheduleInstance.device_info) - HA prefixes the plain name
        # above with the device's name for display - see sensor.py's
        # _ScheduleSensorBase for the full reasoning.
        self._attr_device_info = instance.device_info
        self._attr_current_option = "Auto"
        self._sticky_entity_id = instance.sticky_entity_id
        # The computed (non-override) phase at the moment this was last
        # pinned to something other than Auto - once computed_phase
        # moves on from this, the override has "seen its boundary" and
        # self-clears (unless sticky). None whenever current_option is
        # Auto.
        self._baseline_phase: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in PHASE_OPTIONS:
            self._attr_current_option = last_state.state
        if self._attr_current_option != "Auto":
            # We don't know what computed_phase was when this override
            # was originally set (that wasn't persisted) - treat "now"
            # as the baseline instead, so a restart doesn't accidentally
            # make a non-sticky override outlive the next boundary by
            # more than one restart's worth of slack.
            self._baseline_phase = self.coordinator.data.get("computed_phase")
            # The coordinator's first refresh ran before this entity
            # existed in the state machine, so its data doesn't reflect
            # the restored override yet. async_request_refresh is
            # debounced, so this lands after the entity's initial state
            # write rather than racing it.
            await self.coordinator.async_request_refresh()

    @property
    def _sticky(self) -> bool:
        state = self.hass.states.get(self._sticky_entity_id)
        return state is not None and state.state == "on"

    def _handle_coordinator_update(self) -> None:
        if (
            not self._sticky
            and self._attr_current_option != "Auto"
            and self._baseline_phase is not None
            and self.coordinator.data.get("computed_phase") != self._baseline_phase
        ):
            self._attr_current_option = "Auto"
            self._baseline_phase = None
            # The data this very update already reflects the (now-stale)
            # override - request another refresh so phase/brightness/
            # color_temp catch up to Auto immediately rather than
            # waiting up to 60s for the next poll.
            self.hass.async_create_task(self.coordinator.async_request_refresh())
        super()._handle_coordinator_update()

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self._baseline_phase = self.coordinator.data.get("computed_phase") if option != "Auto" else None
        self.async_write_ha_state()
        # Apply immediately rather than waiting for the 60s poll or the
        # next sun.sun change - the whole point of an override is that
        # it takes effect right away.
        await self.coordinator.async_request_refresh()
