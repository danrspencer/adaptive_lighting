"""
Brightness/Kelvin curve config, as live number entities - one per
coordinator.py's CURVE_KEYS, per schedule instance. Left at their
default (curve.py's DEFAULT_CURVE_VALUES) on first creation; changing
one persists (RestoreNumber) and immediately refreshes that instance's
coordinator (see async_set_native_value below) rather than waiting up
to 60s for the next poll.

entity_category=CONFIG groups these under the device's "Configuration"
section in the UI, separate from the primary sensor/curve/phase-select
entities - eight extra always-visible entities per sensor just to
occasionally tweak one number would be exactly the kind of noise the
old boundary sensors were criticised for; CONFIG keeps them present
(and dashboard/automation-usable) without cluttering the main view.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import CURVE_KEYS, ScheduleCoordinator, ScheduleInstance, schedule_instances
from .curve import DEFAULT_CURVE_VALUES

_LABELS = {
    "morning_brightness": "Morning Brightness",
    "morning_kelvin": "Morning Colour Temperature",
    "day_brightness": "Day Brightness",
    "day_end_kelvin": "Day End Colour Temperature",
    "evening_brightness": "Evening Brightness",
    "evening_kelvin": "Evening Colour Temperature",
    "night_brightness": "Night Brightness",
    "night_kelvin": "Night Colour Temperature",
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    for instance in schedule_instances(entry):
        coordinator: ScheduleCoordinator = hass.data[DOMAIN][instance.subentry_id]
        entities = [_CurveNumber(coordinator, instance, key) for key in CURVE_KEYS]
        async_add_entities(entities, config_subentry_id=instance.subentry_id)


class _CurveNumber(RestoreNumber, NumberEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = "box"

    def __init__(self, coordinator: ScheduleCoordinator, instance: ScheduleInstance, key: str) -> None:
        self._coordinator = coordinator
        self._key = key
        self._attr_unique_id = f"{instance.subentry_id}_{key}"
        self.entity_id = instance.number_entity_id(key)
        self._attr_device_info = instance.device_info
        self._attr_name = _LABELS[key]
        if key.endswith("_brightness"):
            self._attr_native_min_value = 0
            self._attr_native_max_value = 255
            self._attr_native_step = 1
        else:
            self._attr_native_min_value = 1000
            self._attr_native_max_value = 10000
            self._attr_native_unit_of_measurement = "K"
            self._attr_native_step = 1
        self._attr_native_value = DEFAULT_CURVE_VALUES[key]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_data = await self.async_get_last_number_data()
        if last_data is not None and last_data.native_value is not None:
            self._attr_native_value = last_data.native_value

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
        # Apply immediately rather than waiting for the 60s poll - the
        # whole point of exposing this as an entity is that changing it
        # takes effect right away.
        await self._coordinator.async_request_refresh()
