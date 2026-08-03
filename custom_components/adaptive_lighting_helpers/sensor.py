"""
Day-phase/curve sensors, computed with curve.py - a native replacement
for a Jinja `packages/*.yaml` template-sensor setup (see CLAUDE.md for
the live version this ports). Only set up if the config entry has
schedule entities configured (see config_flow.py) - the
compute_lighting_groups/compute_curve services work without any of
this.

Entity IDs are forced to match the sensor names the original Jinja
setup used (sensor.morning_start, sensor.day_phase, etc.) rather than
the usual integration-prefixed auto-generated ones, so this is a
drop-in replacement for anything already pointed at those names (the
dashboard card's DEFAULT_ENTITIES, in particular). If those entity_ids
are already taken by the Jinja package you're migrating away from,
remove that package first - HA will otherwise suffix these with _2.

Boundary computation mirrors the ported Jinja exactly: morning/day/
night are "the configured time-of-day, today"; evening is sunset
(sun.sun's next_setting), clamped between earliest/latest bounds.
Everything recomputes every 60 seconds AND immediately whenever a
configured input_datetime or sun.sun changes - curve.py's functions
are cheap pure arithmetic, so there's no reason to stagger update
cadences the way the original Jinja did to reduce template re-renders.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator
import homeassistant.util.dt as dt_util

from .const import DOMAIN
from .curve import brightness_for_phase, kelvin_for_phase, phase_at

UPDATE_INTERVAL = timedelta(seconds=60)


def _today_at_timestamp(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Equivalent of Jinja's today_at(states(entity_id)) | as_timestamp -
    entity_id is an input_datetime storing a time-of-day (HH:MM:SS)."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    t = dt_util.parse_time(state.state)
    if t is None:
        return None
    now_local = dt_util.now()
    return now_local.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0).timestamp()


def _compute_boundaries(hass: HomeAssistant, config: dict[str, Any]) -> dict[str, float | None]:
    morning_ts = _today_at_timestamp(hass, config.get("morning_entity"))
    day_ts = _today_at_timestamp(hass, config.get("day_entity"))
    night_ts = _today_at_timestamp(hass, config.get("night_entity"))
    earliest_ts = _today_at_timestamp(hass, config.get("evening_earliest_entity"))
    latest_ts = _today_at_timestamp(hass, config.get("evening_latest_entity"))

    sun_state = hass.states.get("sun.sun")
    next_setting = sun_state.attributes.get("next_setting") if sun_state else None
    sunset_dt = dt_util.parse_datetime(next_setting) if next_setting else None
    sunset_ts = sunset_dt.timestamp() if sunset_dt else latest_ts

    if earliest_ts is not None and latest_ts is not None and sunset_ts is not None:
        evening_ts = max(earliest_ts, min(sunset_ts, latest_ts))
    else:
        evening_ts = None

    return {"morning_ts": morning_ts, "day_ts": day_ts, "evening_ts": evening_ts, "night_ts": night_ts}


def _compute_curve_points(boundaries: dict[str, float | None]) -> list[dict[str, Any]]:
    morning_ts, day_ts, evening_ts, night_ts = (
        boundaries["morning_ts"],
        boundaries["day_ts"],
        boundaries["evening_ts"],
        boundaries["night_ts"],
    )
    midnight = dt_util.start_of_local_day().timestamp()
    points = []
    for i in range(289):
        t = midnight + i * 300
        phase = phase_at(t, morning_ts, day_ts, evening_ts, night_ts)
        points.append(
            {
                "t": int(t),
                "brightness": brightness_for_phase(phase, t, night_ts),
                "kelvin": kelvin_for_phase(phase, t, evening_ts, day_ts, night_ts),
            }
        )
    return points


class _ScheduleCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, __name__, name="Adaptive Lighting schedule", update_interval=UPDATE_INTERVAL)
        self._config = entry.data

    async def _async_update_data(self) -> dict[str, Any]:
        boundaries = _compute_boundaries(self.hass, self._config)
        now_ts = time.time()
        phase = phase_at(now_ts, boundaries["morning_ts"], boundaries["day_ts"], boundaries["evening_ts"], boundaries["night_ts"])
        brightness = brightness_for_phase(phase, now_ts, boundaries["night_ts"])
        kelvin = kelvin_for_phase(phase, now_ts, boundaries["evening_ts"], boundaries["day_ts"], boundaries["night_ts"])
        return {
            **boundaries,
            "phase": phase,
            "brightness": brightness,
            "kelvin": kelvin,
            "points": _compute_curve_points(boundaries),
        }


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = _ScheduleCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    tracked = [
        entry.data[key]
        for key in ("morning_entity", "day_entity", "evening_earliest_entity", "evening_latest_entity", "night_entity")
        if entry.data.get(key)
    ] + ["sun.sun"]

    def _handle_tracked_change(event) -> None:
        hass.async_create_task(coordinator.async_request_refresh())

    entry.async_on_unload(async_track_state_change_event(hass, tracked, _handle_tracked_change))

    entities = [
        _BoundarySensor(coordinator, entry, "morning_ts", "Morning Start", "morning_start"),
        _BoundarySensor(coordinator, entry, "day_ts", "Day Start", "day_start"),
        _BoundarySensor(coordinator, entry, "evening_ts", "Evening Start", "evening_start"),
        _BoundarySensor(coordinator, entry, "night_ts", "Night Start", "night_start"),
        _PhaseSensor(coordinator, entry),
        _BrightnessSensor(coordinator, entry),
        _ColorTemperatureSensor(coordinator, entry),
        _CurveSensor(coordinator, entry),
    ]
    async_add_entities(entities)


class _ScheduleSensorBase(CoordinatorEntity[_ScheduleCoordinator], SensorEntity):
    _attr_has_entity_name = False

    def __init__(self, coordinator: _ScheduleCoordinator, entry: ConfigEntry, unique_id_suffix: str, forced_object_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{unique_id_suffix}"
        self.entity_id = f"sensor.{forced_object_id}"


class _BoundarySensor(_ScheduleSensorBase):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: _ScheduleCoordinator, entry: ConfigEntry, key: str, name: str, object_id: str) -> None:
        super().__init__(coordinator, entry, key, object_id)
        self._key = key
        self._attr_name = name

    @property
    def native_value(self):
        ts = self.coordinator.data.get(self._key)
        return dt_util.utc_from_timestamp(ts) if ts is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Kept as a plain "timestamp" attribute (not just the native
        # TIMESTAMP device_class value) because the dashboard card and
        # compute_curve's inputs both read state_attr(..., 'timestamp')
        # directly, matching the Jinja version this replaces.
        return {"timestamp": self.coordinator.data.get(self._key)}


class _PhaseSensor(_ScheduleSensorBase):
    _attr_name = "Day Phase"
    _attr_icon = "mdi:sun-clock"

    def __init__(self, coordinator: _ScheduleCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "day_phase", "day_phase")

    @property
    def native_value(self):
        return self.coordinator.data.get("phase")


class _BrightnessSensor(_ScheduleSensorBase):
    _attr_name = "Solar Adaptive Lighting Brightness"
    _attr_icon = "mdi:brightness-6"

    def __init__(self, coordinator: _ScheduleCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "brightness", "solar_adaptive_lighting_brightness")

    @property
    def native_value(self):
        return self.coordinator.data.get("brightness")


class _ColorTemperatureSensor(_ScheduleSensorBase):
    _attr_name = "Solar Adaptive Lighting Color Temperature"
    _attr_icon = "mdi:thermometer"
    _attr_native_unit_of_measurement = "K"

    def __init__(self, coordinator: _ScheduleCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "kelvin", "solar_adaptive_lighting_color_temperature")

    @property
    def native_value(self):
        return self.coordinator.data.get("kelvin")


class _CurveSensor(_ScheduleSensorBase):
    _attr_name = "Adaptive Lighting Curve"
    _attr_icon = "mdi:chart-bell-curve"

    def __init__(self, coordinator: _ScheduleCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "curve", "adaptive_lighting_curve")

    @property
    def native_value(self):
        # The state itself isn't meaningful (mirrors the Jinja version,
        # which just used now().isoformat()) - attributes.points is the
        # actual payload the dashboard card reads.
        return dt_util.now().isoformat()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"points": self.coordinator.data.get("points")}
