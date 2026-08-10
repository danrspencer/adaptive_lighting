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

# The Kelvin value Night sits at (and what Evening's final hour fades
# toward) when nothing overrides it - matches every bulb's native
# color_temp range. The one place this number is allowed to be a literal;
# every other file that needs it imports this constant instead of
# repeating "2700" - see kelvin_for_phase's night_kelvin param.
DEFAULT_NIGHT_KELVIN = 2700


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


def brightness_for_phase(
    day_phase: str,
    now_ts: float,
    night_ts: float,
    *,
    day_brightness: int = 255,
    evening_brightness: int = 180,
    night_brightness: int = 80,
) -> int:
    """Target brightness (0-255) for the given phase/instant."""
    if day_phase in ("Morning", "Day"):
        return day_brightness
    if day_phase == "Evening":
        fade_start_ts = night_ts - 3600
        if now_ts < fade_start_ts:
            return evening_brightness
        t = (night_ts - now_ts) / 3600
        # The original hardcoded formula (80 + 160*t) reaches
        # evening_brightness before the fade window's nominal hour is
        # up (at t=0.625, ~37.5 minutes in) and holds there via the
        # clamp below for the rest - 160 is 1.6x the 80->180 span, not
        # the span itself. Preserved as a ratio, not the literal span,
        # so a custom brightness range keeps the same timing shape.
        b = night_brightness + ((evening_brightness - night_brightness) * 1.6 * t)
        lo, hi = sorted((night_brightness, evening_brightness))
        return round(min(max(b, lo), hi))
    return night_brightness  # Night


def kelvin_for_phase(
    day_phase: str,
    now_ts: float,
    evening_ts: float,
    day_start_ts: float,
    night_ts: float,
    *,
    morning_kelvin: int = 6667,
    day_end_kelvin: int = 4000,
    evening_kelvin: int = 3200,
    night_kelvin: int = DEFAULT_NIGHT_KELVIN,
) -> int:
    """Target colour temperature (Kelvin) for the given phase/instant.

    morning_kelvin: Morning's steady value, and where Day's ramp starts
    from. day_end_kelvin: what Day ramps down to by the time Evening
    starts (also where Evening's own ramp starts). evening_kelvin:
    Evening's steady hold, after its opening ramp from day_end_kelvin.
    night_kelvin: what Evening's final hour fades toward, and what Night
    sits at - defaults to DEFAULT_NIGHT_KELVIN, matching every bulb's
    native color_temp range."""
    if day_phase == "Morning":
        return morning_kelvin
    if day_phase == "Day":
        total_day = evening_ts - day_start_ts
        t_day = (now_ts - day_start_ts) / total_day if total_day > 0 else 0
        t_day = min(max(t_day, 0), 1)
        return round(morning_kelvin - ((morning_kelvin - day_end_kelvin) * t_day))
    if day_phase == "Evening":
        fade_start_ts = night_ts - 3600
        if now_ts >= fade_start_ts:
            t = (night_ts - now_ts) / 3600
            return round(night_kelvin + ((evening_kelvin - night_kelvin) * t))
        hold_start_ts = min(evening_ts + 3600, fade_start_ts)
        if now_ts < hold_start_ts:
            ramp_len = hold_start_ts - evening_ts
            t = ((now_ts - evening_ts) / ramp_len) if ramp_len > 0 else 1
            t = min(max(t, 0), 1)
            return round(day_end_kelvin - ((day_end_kelvin - evening_kelvin) * t))
        return evening_kelvin
    return night_kelvin  # Night


def targets_for_phase(
    day_phase: str,
    now_ts: float,
    evening_ts: float,
    day_start_ts: float,
    night_ts: float,
    *,
    day_brightness: int = 255,
    evening_brightness: int = 180,
    night_brightness: int = 80,
    morning_kelvin: int = 6667,
    day_end_kelvin: int = 4000,
    evening_kelvin: int = 3200,
    night_kelvin: int = DEFAULT_NIGHT_KELVIN,
) -> dict:
    """brightness/kelvin/kelvin_rgb/rgb_color for an already-known phase,
    in one call - the single orchestration point for
    brightness_for_phase/kelvin_for_phase/kelvin_to_rgb.

    Takes day_phase rather than computing it via phase_at() itself
    because some callers need to substitute a different phase first
    (coordinator.py's manual override reads phase_at()'s result but then
    may replace it with select.adaptive_lighting_phase's value before
    computing brightness/kelvin from it) - phase_at() stays a separate
    call so that substitution has somewhere to happen. Callers that don't
    need it can just call phase_at() immediately before this.

    Previously this 4-line sequence was hand-copied at every call site
    (the compute_curve service, the coordinator's "now" values, its
    289-point curve loop, and the preview generator) - risking drift if
    the shape of what gets computed here ever changed. One copy now.

    kelvin_rgb duplicates kelvin (kept as a separate key since sensor.py
    and apply_lighting's RGB dispatch path already read it) - there's no
    longer a separate RGB-only Kelvin floor to diverge it from colour-temp
    bulbs' target."""
    brightness = brightness_for_phase(
        day_phase,
        now_ts,
        night_ts,
        day_brightness=day_brightness,
        evening_brightness=evening_brightness,
        night_brightness=night_brightness,
    )
    kelvin = kelvin_for_phase(
        day_phase,
        now_ts,
        evening_ts,
        day_start_ts,
        night_ts,
        morning_kelvin=morning_kelvin,
        day_end_kelvin=day_end_kelvin,
        evening_kelvin=evening_kelvin,
        night_kelvin=night_kelvin,
    )
    return {
        "brightness": brightness,
        "kelvin": kelvin,
        "kelvin_rgb": kelvin,
        "rgb_color": kelvin_to_rgb(kelvin),
    }
