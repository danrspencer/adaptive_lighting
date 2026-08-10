"""
Day-phase/curve sensors, backed by coordinator.py - a native
replacement for a Jinja `packages/*.yaml` template-sensor setup (see
CLAUDE.md for the live version this ports). Only set up if the config
entry has schedule times configured (see config_flow.py) - the
compute_lighting_groups/compute_curve/compute_scene_coverage services
work without any of this.

Entity IDs are forced to match the sensor names the original Jinja
setup used (sensor.morning_start, sensor.adaptive_lighting_curve, etc.)
where a like-for-like equivalent exists, so this is a drop-in
replacement for anything already pointed at those names (the dashboard
card's DEFAULT_ENTITIES, in particular). If those entity_ids are
already taken by the Jinja package you're migrating away from, remove
that package first - HA will otherwise suffix these with _2.

One exception: day-phase and the brightness/colour-temperature "right
now" values are combined into a single sensor.adaptive_lighting here
(state = phase, attributes = phase/brightness/color_temp) rather than
three separate sensors - `brightness`/`color_temp` are exactly the
attribute names the blueprint's `adaptive_sensor` input already reads
via state_attr(), matching the shape the old packages/adaptive_lighting.yaml
`sensor.solar_adaptive_lighting` sensor used, so this is a drop-in for
that role too. A standalone day-phase entity was considered and
dropped in favour of this - anything that wants to react to just the
phase changing can use a `platform: state, attribute: phase` trigger
on this entity (the blueprint already uses the equivalent
whole-attribute-set version of this pattern for its own adaptive_attr
trigger), no separate entity required.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
import homeassistant.util.dt as dt_util

from .const import DOMAIN
from .coordinator import ScheduleCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: ScheduleCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        _BoundarySensor(coordinator, entry, "morning_ts", "Morning Start", "morning_start"),
        _BoundarySensor(coordinator, entry, "day_ts", "Day Start", "day_start"),
        _BoundarySensor(
            coordinator,
            entry,
            "evening_ts",
            "Evening Start",
            "evening_start",
            extra_keys={"earliest": "evening_earliest_ts", "latest": "evening_latest_ts"},
        ),
        _BoundarySensor(coordinator, entry, "night_ts", "Night Start", "night_start"),
        _AdaptiveLightingSensor(coordinator, entry),
        _CurveSensor(coordinator, entry),
    ]
    async_add_entities(entities)


class _ScheduleSensorBase(CoordinatorEntity[ScheduleCoordinator], SensorEntity):
    _attr_has_entity_name = False

    def __init__(self, coordinator: ScheduleCoordinator, entry: ConfigEntry, unique_id_suffix: str, forced_object_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{unique_id_suffix}"
        self.entity_id = f"sensor.{forced_object_id}"


class _BoundarySensor(_ScheduleSensorBase):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self,
        coordinator: ScheduleCoordinator,
        entry: ConfigEntry,
        key: str,
        name: str,
        object_id: str,
        extra_keys: dict[str, str] | None = None,
    ) -> None:
        super().__init__(coordinator, entry, key, object_id)
        self._key = key
        self._attr_name = name
        self._extra_keys = extra_keys or {}

    @property
    def native_value(self):
        ts = self.coordinator.data.get(self._key)
        return dt_util.utc_from_timestamp(ts) if ts is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Kept as a plain "timestamp" attribute (not just the native
        # TIMESTAMP device_class value) because the dashboard card and
        # compute_curve's inputs both read state_attr(..., 'timestamp')
        # directly, matching the Jinja version this replaces. Evening's
        # extra earliest/latest attributes let the dashboard card show
        # why evening landed where it did without a separate entity.
        attrs = {"timestamp": self.coordinator.data.get(self._key)}
        for attr_name, data_key in self._extra_keys.items():
            attrs[attr_name] = self.coordinator.data.get(data_key)
        return attrs


class _AdaptiveLightingSensor(_ScheduleSensorBase):
    _attr_name = "Adaptive Lighting"
    _attr_icon = "mdi:home-lightbulb"

    def __init__(self, coordinator: ScheduleCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "adaptive_lighting", "adaptive_lighting")

    @property
    def native_value(self):
        return self.coordinator.data.get("phase")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        return {
            "phase": data.get("phase"),
            "brightness": data.get("brightness"),
            "color_temp": data.get("kelvin"),
        }


class _CurveSensor(_ScheduleSensorBase):
    _attr_name = "Adaptive Lighting Curve"
    _attr_icon = "mdi:chart-bell-curve"

    def __init__(self, coordinator: ScheduleCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "curve", "adaptive_lighting_curve")

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
