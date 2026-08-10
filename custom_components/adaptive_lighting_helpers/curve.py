"""
Solar adaptive-lighting brightness/colour-temperature schedule.

Pure functions, no Home Assistant dependency - a direct port of the
Jinja macros that used to live in custom_templates/adaptive_lighting.jinja.
Same inputs, same outputs, just testable with plain pytest instead of
having to render templates to check the math.

All timestamps are unix seconds. Boundary timestamps (morning/day
start/evening start/night start) are today's, computed elsewhere from
the user's input_datetime helpers plus sunset.
"""

import math


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(max(v, lo), hi)


def kelvin_to_rgb(kelvin: float) -> tuple:
    """Tanner Helland's Kelvin -> RGB approximation - same algorithm as
    www/adaptive-lighting-curve-card.js's kelvinToRgb(), so the dashboard
    card and whatever RGB actually gets sent to a light agree exactly.

    Uses round-half-up (math.floor(x + 0.5)) rather than Python's
    round(), which rounds half-to-even - JS's Math.round (what the card
    uses) always rounds .5 up, so plain round() would silently disagree
    with the card on tie values."""

    def _round(x: float) -> int:
        return int(math.floor(x + 0.5))

    temp = kelvin / 100
    r = 255 if temp <= 66 else _clamp(329.698727446 * (temp - 60) ** -0.1332047592, 0, 255)
    if temp <= 66:
        g = _clamp(99.4708025861 * math.log(temp) - 161.1195681661, 0, 255)
    else:
        g = _clamp(288.1221695283 * (temp - 60) ** -0.0755148492, 0, 255)
    if temp >= 66:
        b = 255
    elif temp <= 19:
        b = 0
    else:
        b = _clamp(138.5177312231 * math.log(temp - 10) - 305.0447927307, 0, 255)
    return (_round(r), _round(g), _round(b))


def phase_at(t: float, morning_ts: float, day_start_ts: float, evening_ts: float, night_ts: float) -> str:
    """Which phase a given instant falls in, given today's boundaries.

    Used to precompute the full-day curve for the dashboard graph -
    there's no real day_phase for past/future instants, only "what would
    it have been". If input_select.day_phase is ever manually overridden,
    the live sensors follow the override while this does not, so the
    curve and the "now" marker can disagree briefly.
    """
    if t < morning_ts:
        return "Night"
    if t < day_start_ts:
        return "Morning"
    if t < evening_ts:
        return "Day"
    if t < night_ts:
        return "Evening"
    return "Night"


def brightness_for_phase(day_phase: str, now_ts: float, night_ts: float) -> int:
    """Target brightness (0-255) for the given phase/instant."""
    if day_phase in ("Morning", "Day"):
        return 255
    if day_phase == "Evening":
        fade_start_ts = night_ts - 3600
        if now_ts < fade_start_ts:
            return 180
        t = (night_ts - now_ts) / 3600
        b = 80 + (160 * t)
        return round(min(max(b, 80), 180))
    return 80  # Night


def kelvin_for_phase(
    day_phase: str,
    now_ts: float,
    evening_ts: float,
    day_start_ts: float,
    night_ts: float,
    *,
    night_floor: int = 2700,
) -> int:
    """Target colour temperature (Kelvin) for the given phase/instant.

    night_floor is the Kelvin value Night sits at (and what Evening's
    final hour fades toward) - defaults to 2700, matching every bulb's
    native color_temp range. Callers computing an RGB target (which can
    represent colours beyond a bulb's native color_temp minimum) can pass
    a lower value here to get a deeper-amber Night/late-Evening than
    color_temp_kelvin could ever reach - see coordinator.py's
    night_floor_kelvin config field. Morning/Day and Evening's earlier
    ramp (4000K->3200K hold) are unaffected either way."""
    if day_phase == "Morning":
        return 6667
    if day_phase == "Day":
        total_day = evening_ts - day_start_ts
        t_day = (now_ts - day_start_ts) / total_day if total_day > 0 else 0
        t_day = min(max(t_day, 0), 1)
        return round(6667 - (2667 * t_day))
    if day_phase == "Evening":
        fade_start_ts = night_ts - 3600
        if now_ts >= fade_start_ts:
            t = (night_ts - now_ts) / 3600
            return round(night_floor + (500 * t))
        hold_start_ts = min(evening_ts + 3600, fade_start_ts)
        if now_ts < hold_start_ts:
            ramp_len = hold_start_ts - evening_ts
            t = ((now_ts - evening_ts) / ramp_len) if ramp_len > 0 else 1
            t = min(max(t, 0), 1)
            return round(4000 - (800 * t))
        return 3200
    return night_floor  # Night
