"""
Shared schedule coordinator for the optional day-phase/curve sensors
(sensor.py) and the phase-override select (select.py) - the two
platforms share one coordinator instance per schedule instance (see
ScheduleInstance below), created once in __init__.py and stashed in
hass.data, rather than each computing independently.

Boundary computation mirrors what used to be the live
packages/adaptive_lighting.yaml Jinja setup: morning/day/night are
today's configured time-of-day; evening is sunset (sun.sun's
next_setting), clamped between earliest/latest bounds. The five times
themselves live directly on the config entry or subentry (plain
HH:MM:SS strings from a TimeSelector - see config_flow.py) rather than
being read from separate input_datetime helpers the user had to create
first.

Override: each instance's own select.<prefix>adaptive_lighting_phase
can pin the phase used for "right now" - _phase_override() reads its
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

Schedule instances: the config entry itself never carries a schedule -
it only registers the services (see __init__.py). Every schedule is a
"sensor" subentry, added via the "Add Sensor" flow (config_flow.py's
SensorSubentryFlow) - so there's exactly one mechanism for adding a
schedule, not "the first one is special". A subentry's name is
optional, though: leave it blank for bare, unprefixed entity IDs
(sensor.adaptive_lighting etc, matching the original single-sensor
naming) or give it a name for prefixed ones (sensor.living_room_
adaptive_lighting) - at most one blank-named subentry is allowed,
enforced the same way a duplicate name is (see SensorSubentryFlow).
schedule_instances() is the one place that enumerates all of them -
__init__.py, sensor.py, and select.py all iterate its output rather
than each re-deriving the subentry lookup.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import slugify
import homeassistant.util.dt as dt_util

from .const import DOMAIN, SUBENTRY_TYPE_SENSOR
from .curve import phase_at, targets_for_phase

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(seconds=60)

# The five required time fields every "sensor" subentry has - see
# config_flow.py's TIME_FIELDS.
TIME_KEYS = (
    "morning_time",
    "day_time",
    "evening_earliest_time",
    "evening_latest_time",
    "night_time",
)

# The optional brightness/Kelvin curve fields - see config_flow.py's
# CURVE_AND_BEHAVIOR_FIELDS. Left unset, targets_for_phase's own
# defaults (curve.py's DEFAULT_CURVE_VALUES) apply. Grouped by phase
# (brightness then Kelvin, Morning through Night) rather than by
# attribute type - this order also drives the config_flow.py form and
# the compute_curve service schema, both built from this tuple.
CURVE_KEYS = (
    "morning_brightness",
    "morning_kelvin",
    "day_brightness",
    "day_end_kelvin",
    "evening_brightness",
    "evening_kelvin",
    "night_brightness",
    "night_kelvin",
)


@dataclass
class ScheduleInstance:
    """One sensor's schedule/curve setup, derived from a "sensor"
    subentry - named (prefixed) or blank (bare names)."""

    key: str  # hass.data storage key: the subentry_id
    prefix: str  # "<slug>_" (named) or "" (blank name - bare entity IDs)
    config: Mapping[str, Any]  # subentry.data
    subentry_id: str  # passed to async_add_entities(config_subentry_id=...)
    override_entity_id: str  # select.<prefix>adaptive_lighting_phase
    title: str  # "" (blank name) or the subentry's name

    @property
    def device_info(self) -> DeviceInfo | None:
        """None for a blank-named (bare-entity-ID) instance - those stay
        standalone entities, unchanged from before devices existed here.
        A named instance gets one device, named exactly what the user
        typed - has_entity_name=True on its entities (see sensor.py/
        select.py) then lets HA prefix their displayed names with this
        automatically, so renaming the sensor is a single action
        (Settings -> Devices -> rename) instead of us reconstructing a
        name via string concatenation (which used to just lowercase-
        concatenate whatever was typed, e.g. "upstairs Adaptive
        Lighting" - the actual complaint that prompted this)."""
        if not self.title:
            return None
        return DeviceInfo(
            identifiers={(DOMAIN, self.subentry_id)},
            name=self.title,
            entry_type=DeviceEntryType.SERVICE,
        )


def schedule_instances(entry: ConfigEntry) -> list[ScheduleInstance]:
    """Every schedule instance this entry should set up sensors/select
    for - one per "sensor" subentry (see config_flow.py's
    SensorSubentryFlow). The entry itself never carries a schedule -
    it only registers the services (see __init__.py)."""
    instances = []
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TYPE_SENSOR:
            continue
        slug = slugify(subentry.title)
        prefix = f"{slug}_" if slug else ""
        instances.append(
            ScheduleInstance(
                key=subentry_id,
                prefix=prefix,
                config=subentry.data,
                subentry_id=subentry_id,
                override_entity_id=f"select.{prefix}adaptive_lighting_phase",
                title=subentry.title,
            )
        )
    return instances


def _curve_kwargs(config: Mapping[str, Any]) -> dict[str, int]:
    return {key: config[key] for key in CURVE_KEYS if config.get(key) is not None}


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


def _compute_boundaries(hass: HomeAssistant, config: Mapping[str, Any]) -> dict[str, float | None]:
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


def _compute_curve_points(boundaries: dict[str, float | None], curve_kwargs: dict[str, int]) -> list[dict[str, Any]]:
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
        targets = targets_for_phase(phase, t, evening_ts, day_ts, night_ts, **curve_kwargs)
        points.append(
            {
                "t": int(t),
                "brightness": targets["brightness"],
                "kelvin": targets["kelvin"],
                "kelvin_rgb": targets["kelvin_rgb"],
            }
        )
    return points


def _phase_override(hass: HomeAssistant, override_entity_id: str) -> str | None:
    state = hass.states.get(override_entity_id)
    if state is None or state.state in ("Auto", "unknown", "unavailable"):
        return None
    return state.state


class ScheduleCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, instance: ScheduleInstance) -> None:
        super().__init__(hass, _LOGGER, name=f"Adaptive Lighting schedule ({instance.title})", update_interval=UPDATE_INTERVAL)
        self._config = instance.config
        self._override_entity_id = instance.override_entity_id

    async def _async_update_data(self) -> dict[str, Any]:
        boundaries = _compute_boundaries(self.hass, self._config)
        curve_kwargs = _curve_kwargs(self._config)
        now_ts = time.time()
        computed_phase = phase_at(now_ts, boundaries["morning_ts"], boundaries["day_ts"], boundaries["evening_ts"], boundaries["night_ts"])
        phase = _phase_override(self.hass, self._override_entity_id) or computed_phase
        targets = targets_for_phase(
            phase, now_ts, boundaries["evening_ts"], boundaries["day_ts"], boundaries["night_ts"], **curve_kwargs
        )
        return {
            **boundaries,
            "phase": phase,
            "computed_phase": computed_phase,
            "brightness": targets["brightness"],
            "kelvin": targets["kelvin"],
            "rgb_color": targets["rgb_color"],
            "points": _compute_curve_points(boundaries, curve_kwargs),
        }
