"""
Day-phase/curve sensors, backed by coordinator.py - a native
replacement for a Jinja `packages/*.yaml` template-sensor setup (see
CLAUDE.md for the live version this ports). One set of these per
schedule instance (see coordinator.py's ScheduleInstance/
schedule_instances) - one per named "sensor" subentry.

Entity IDs are prefixed with the sensor's slugified name (e.g.
sensor.living_room_adaptive_lighting) - see coordinator.py's
schedule_instances(). Every sensor also gets its own device (see
ScheduleInstance.device_info).
has_entity_name=True plus a short per-entity name ("Curve") lets HA
prefix it with the device's own name for display ("Upstairs Curve", or
"Adaptive Lighting Curve" for the default device), so renaming the
sensor is one action (Settings -> Devices -> rename) rather than us
reconstructing a name via string concatenation. The main sensor sets
its own name to None - the idiomatic HA pattern for "the entity that
represents the device" - so it displays as just the device's name
alone ("Upstairs", or "Adaptive Lighting" by default), not a repeat of
it.

Day-phase, the brightness/colour-temperature "right now" values, and
today's four phase-boundary timestamps are all combined into a single
sensor.adaptive_lighting (state = phase, attributes = phase/brightness/
color_temp/morning_start/day_start/evening_start/night_start/
evening_earliest/evening_latest) rather than separate sensors per
value - `brightness`/`color_temp` are exactly the attribute names the
blueprint's `adaptive_sensor` input already reads via state_attr(),
matching the shape the old packages/adaptive_lighting.yaml
`sensor.solar_adaptive_lighting` sensor used, so this is a drop-in for
that role. The four boundary timestamps used to be their own
sensor.morning_start/day_start/evening_start/night_start entities -
folded into attributes here instead (four extra always-on entities per
sensor that exist just to be read as one-off attribute lookups was
judged not worth it; a phase-change automation reads
sensor.adaptive_lighting's phase attribute directly, and anything that
specifically wants a boundary time - the dashboard card, in particular
- reads it off this same entity's attributes). A standalone day-phase
entity was considered and dropped for the same reason - anything that
wants to react to just the phase changing can use a
`platform: state, attribute: phase` trigger on this entity, no
separate entity required.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
import homeassistant.util.dt as dt_util

from .const import DOMAIN
from .coordinator import ScheduleCoordinator, ScheduleInstance, schedule_instances


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    for instance in schedule_instances(entry):
        coordinator: ScheduleCoordinator = hass.data[DOMAIN][instance.subentry_id]
        entities = [
            _AdaptiveLightingSensor(coordinator, instance),
            _CurveSensor(coordinator, instance),
        ]
        async_add_entities(entities, config_subentry_id=instance.subentry_id)


class _ScheduleSensorBase(CoordinatorEntity[ScheduleCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: ScheduleCoordinator, instance: ScheduleInstance, unique_id_suffix: str, forced_object_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{instance.subentry_id}_{unique_id_suffix}"
        self.entity_id = f"sensor.{instance.prefix}{forced_object_id}"
        self._attr_device_info = instance.device_info


class _AdaptiveLightingSensor(_ScheduleSensorBase):
    _attr_icon = "mdi:home-lightbulb"
    _attr_name = None  # the entity that represents the device - displays as just the device's own name

    def __init__(self, coordinator: ScheduleCoordinator, instance: ScheduleInstance) -> None:
        super().__init__(coordinator, instance, "adaptive_lighting", "adaptive_lighting")

    @property
    def native_value(self):
        return self.coordinator.data.get("phase")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        rgb_color = data.get("rgb_color")
        return {
            "phase": data.get("phase"),
            "brightness": data.get("brightness"),
            "color_temp": data.get("kelvin"),
            # list, not tuple - matches what apply_lighting/
            # compute_lighting_groups's rgb_color field and HA's own
            # color_rgb selector expect (see README's "Bring your own
            # sensor" section for the full attribute contract).
            "rgb_color": list(rgb_color) if rgb_color is not None else None,
            # Today's four phase-boundary timestamps, plus the two
            # configured bounds evening_start was actually clamped
            # between - the dashboard card reads all six of these
            # directly off this entity (see www/adaptive-lighting-curve-card.js).
            "morning_start": data.get("morning_ts"),
            "day_start": data.get("day_ts"),
            "evening_start": data.get("evening_ts"),
            "night_start": data.get("night_ts"),
            "evening_earliest": data.get("evening_earliest_ts"),
            "evening_latest": data.get("evening_latest_ts"),
        }


class _CurveSensor(_ScheduleSensorBase):
    _attr_icon = "mdi:chart-bell-curve"
    _attr_name = "Curve"
    # 289 samples is comfortably over the recorder's 16384-byte attribute
    # limit (it was warning and silently dropping this attribute in
    # storage every update) - "points" is only ever read live off
    # coordinator.data by the dashboard card, never needed from history,
    # so excluding it from the recorder entirely is strictly better than
    # a warning-then-drop every 60s.
    _unrecorded_attributes = frozenset({"points"})

    def __init__(self, coordinator: ScheduleCoordinator, instance: ScheduleInstance) -> None:
        super().__init__(coordinator, instance, "curve", "adaptive_lighting_curve")

    @property
    def native_value(self):
        # The state itself isn't meaningful (mirrors the Jinja version,
        # which just used now().isoformat()) - attributes.points is the
        # actual payload the dashboard card reads. Deliberately not
        # phase/brightness-override-aware - see coordinator.py's
        # module docstring on why the curve stays a pure schedule.
        return dt_util.now().isoformat()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"points": self.coordinator.data.get("points")}
