"""
Shared schedule coordinator for the optional day-phase/curve sensors
(sensor.py) and the phase-override select (select.py) - the two
platforms share one coordinator instance (created once in __init__.py
and stashed in hass.data) rather than each computing independently.

Boundary computation mirrors what used to be the live
packages/adaptive_lighting.yaml Jinja setup: morning/day/night are
today's configured time-of-day; evening is sunset (sun.sun's
next_setting), clamped between earliest/latest bounds. The five times
themselves now live directly on the config entry (plain HH:MM:SS
strings from a TimeSelector - see config_flow.py) rather than being
read from separate input_datetime helpers the user had to create
first.

Override: select.adaptive_lighting_phase (PHASE_OVERRIDE_ENTITY_ID) can
pin the phase used for "right now" - _phase_override() reads its
*current* live state on every update, the same "check fresh, don't
remember" style grouping.py's manually_set() uses, so there's nothing
to expire or persist here beyond what the select entity itself already
does (see select.py's RestoreEntity use). curve.py's phase-taking
functions don't care where the phase string came from, so overriding
needed no changes there.

Deliberately asymmetric: the override affects the "right now" phase/
brightness/kelvin, but NOT the precomputed curve (`points`) - the curve
is a full-day schedule/forecast, and pinning "right now" to Evening
doesn't mean the schedule would have looked different at 9am. This was
previously an accidental inconsistency in the live Jinja version
(noted in phase_at()'s docstring); here it's the same behaviour but
deliberate.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
import homeassistant.util.dt as dt_util

from .const import PHASE_OVERRIDE_ENTITY_ID
from .curve import brightness_for_phase, kelvin_for_phase, kelvin_to_rgb, phase_at

UPDATE_INTERVAL = timedelta(seconds=60)

TIME_KEYS = (
    "morning_time",
    "day_time",
    "evening_earliest_time",
    "evening_latest_time",
    "night_time",
)


def _time_str_to_today_timestamp(time_str: str | None) -> float | None:
    """A TimeSelector value ("HH:MM:SS") -> today's timestamp for that
    time-of-day, in the local timezone."""
    if not time_str:
        return None
    t = dt_util.parse_time(time_str)
    if t is None:
        return None
    now_local = dt_util.now()
    return now_local.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0).timestamp()


def _compute_boundaries(hass: HomeAssistant, config: dict[str, Any]) -> dict[str, float | None]:
    morning_ts = _time_str_to_today_timestamp(config.get("morning_time"))
    day_ts = _time_str_to_today_timestamp(config.get("day_time"))
    night_ts = _time_str_to_today_timestamp(config.get("night_time"))
    earliest_ts = _time_str_to_today_timestamp(config.get("evening_earliest_time"))
    latest_ts = _time_str_to_today_timestamp(config.get("evening_latest_time"))

    sun_state = hass.states.get("sun.sun")
    next_setting = sun_state.attributes.get("next_setting") if sun_state else None
    sunset_dt = dt_util.parse_datetime(next_setting) if next_setting else None
    sunset_ts = sunset_dt.timestamp() if sunset_dt else latest_ts

    if earliest_ts is not None and latest_ts is not None and sunset_ts is not None:
        evening_ts = max(earliest_ts, min(sunset_ts, latest_ts))
    else:
        evening_ts = None

    return {
        "morning_ts": morning_ts,
        "day_ts": day_ts,
        "evening_ts": evening_ts,
        "night_ts": night_ts,
        "evening_earliest_ts": earliest_ts,
        "evening_latest_ts": latest_ts,
    }


def _compute_curve_points(boundaries: dict[str, float | None], night_floor_kelvin: int) -> list[dict[str, Any]]:
    """kelvin_rgb mirrors kelvin but with night_floor_kelvin applied -
    identical to kelvin whenever night_floor_kelvin is left at 2700 (the
    default), diverging only in Evening's final hour + Night when it's
    been set lower. Always computed (cheap pure arithmetic, same
    reasoning as recomputing the whole 289-point curve every 60s) so the
    dashboard card can show the divergence only when there actually is
    one, without needing to know the configured floor itself."""
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
                "kelvin_rgb": kelvin_for_phase(phase, t, evening_ts, day_ts, night_ts, night_floor=night_floor_kelvin),
            }
        )
    return points


def _phase_override(hass: HomeAssistant) -> str | None:
    state = hass.states.get(PHASE_OVERRIDE_ENTITY_ID)
    if state is None or state.state in ("Auto", "unknown", "unavailable"):
        return None
    return state.state


class ScheduleCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, __name__, name="Adaptive Lighting schedule", update_interval=UPDATE_INTERVAL)
        self._config = entry.data

    async def _async_update_data(self) -> dict[str, Any]:
        boundaries = _compute_boundaries(self.hass, self._config)
        night_floor_kelvin = self._config.get("night_floor_kelvin") or 2700
        now_ts = time.time()
        computed_phase = phase_at(now_ts, boundaries["morning_ts"], boundaries["day_ts"], boundaries["evening_ts"], boundaries["night_ts"])
        phase = _phase_override(self.hass) or computed_phase
        brightness = brightness_for_phase(phase, now_ts, boundaries["night_ts"])
        kelvin = kelvin_for_phase(phase, now_ts, boundaries["evening_ts"], boundaries["day_ts"], boundaries["night_ts"])
        kelvin_rgb = kelvin_for_phase(
            phase, now_ts, boundaries["evening_ts"], boundaries["day_ts"], boundaries["night_ts"], night_floor=night_floor_kelvin
        )
        return {
            **boundaries,
            "phase": phase,
            "computed_phase": computed_phase,
            "brightness": brightness,
            "kelvin": kelvin,
            "rgb_color": kelvin_to_rgb(kelvin_rgb),
            "points": _compute_curve_points(boundaries, night_floor_kelvin),
        }
