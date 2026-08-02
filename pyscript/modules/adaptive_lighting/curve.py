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
) -> int:
    """Target colour temperature (Kelvin) for the given phase/instant."""
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
            return round(2700 + (500 * t))
        hold_start_ts = min(evening_ts + 3600, fade_start_ts)
        if now_ts < hold_start_ts:
            ramp_len = hold_start_ts - evening_ts
            t = ((now_ts - evening_ts) / ramp_len) if ramp_len > 0 else 1
            t = min(max(t, 0), 1)
            return round(4000 - (800 * t))
        return 3200
    return 2700  # Night
