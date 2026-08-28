"""
Schedule boundary times, as live time entities - one per coordinator.py's
TIME_KEYS, per schedule instance. Left at a representative default
(curve.py's DEFAULT_SCHEDULE_HOURS) on first creation; changing one
persists (RestoreEntity - the `time` domain has no built-in RestoreTime
equivalent to number's, so this is hand-rolled the same way select.py's
phase override already restores its own state) and immediately
refreshes that instance's coordinator rather than waiting up to 60s for
the next poll.

entity_category=CONFIG groups these under the device's "Configuration"
section - same reasoning as number.py's curve entities.
"""

from __future__ import annotations

import datetime

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
import homeassistant.util.dt as dt_util

from .const import DOMAIN
from .coordinator import TIME_KEYS, ScheduleCoordinator, ScheduleInstance, schedule_instances
from .curve import DEFAULT_SCHEDULE_HOURS

_LABELS = {
    "morning_time": "Morning Start",
    "day_time": "Day Start",
    "evening_earliest_time": "Evening Earliest",
    "evening_latest_time": "Evening Latest",
    "night_time": "Night Start",
}

# TIME_KEYS entries are literally "<hour_key>_time" for the matching
# DEFAULT_SCHEDULE_HOURS key (e.g. "morning_time" -> "morning").
_DEFAULTS = {key: datetime.time(hour=DEFAULT_SCHEDULE_HOURS[key[: -len("_time")]]) for key in TIME_KEYS}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    for instance in schedule_instances(entry):
        coordinator: ScheduleCoordinator = hass.data[DOMAIN][instance.subentry_id]
        entities = [_BoundaryTime(coordinator, instance, key) for key in TIME_KEYS]
        async_add_entities(entities, config_subentry_id=instance.subentry_id)


class _BoundaryTime(TimeEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ScheduleCoordinator, instance: ScheduleInstance, key: str) -> None:
        self._coordinator = coordinator
        self._key = key
        self._attr_unique_id = f"{instance.subentry_id}_{key}"
        self.entity_id = instance.time_entity_id(key)
        self._attr_device_info = instance.device_info
        self._attr_name = _LABELS[key]
        self._attr_native_value = _DEFAULTS[key]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            restored = dt_util.parse_time(last_state.state)
            if restored is not None:
                self._attr_native_value = restored

    async def async_set_value(self, value: datetime.time) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
        # Apply immediately rather than waiting for the 60s poll - the
        # whole point of exposing this as an entity is that changing it
        # takes effect right away.
        await self._coordinator.async_request_refresh()
