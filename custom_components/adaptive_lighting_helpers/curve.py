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

# The brightness/Kelvin literals every phase would use if nothing
# overrides them - ported faithfully from the original Jinja package
# (see CLAUDE.md). The only place these numbers are literals; every
# other file imports the named constants (or DEFAULT_CURVE_VALUES)
# instead of repeating them - config_flow.py's form defaults in
# particular, so what a user sees pre-filled always matches what
# actually happens when a field is left unset.
DEFAULT_MORNING_BRIGHTNESS = 255
DEFAULT_DAY_BRIGHTNESS = 255
DEFAULT_EVENING_BRIGHTNESS = 180
DEFAULT_NIGHT_BRIGHTNESS = 80
DEFAULT_MORNING_KELVIN = 6667
DEFAULT_DAY_END_KELVIN = 4000
DEFAULT_EVENING_KELVIN = 3200
DEFAULT_NIGHT_KELVIN = 2700

# All eight, keyed exactly like coordinator.py's CURVE_KEYS - the one
# place config_flow.py (and anything else wanting the full default set)
# reads actual numbers from, rather than hand-copying each constant.
DEFAULT_CURVE_VALUES = {
    "morning_brightness": DEFAULT_MORNING_BRIGHTNESS,
    "morning_kelvin": DEFAULT_MORNING_KELVIN,
    "day_brightness": DEFAULT_DAY_BRIGHTNESS,
    "day_end_kelvin": DEFAULT_DAY_END_KELVIN,
    "evening_brightness": DEFAULT_EVENING_BRIGHTNESS,
    "evening_kelvin": DEFAULT_EVENING_KELVIN,
    "night_brightness": DEFAULT_NIGHT_BRIGHTNESS,
    "night_kelvin": DEFAULT_NIGHT_KELVIN,
}

# A representative day schedule (hour-of-day), not read by anything
# below - a single shared "sensible starting point" for anything that
# wants to seed or preview a schedule without real user input yet
# (time.py's default value for every new sensor's boundary-time
# entities, dashboard/generate_preview_data.py's synthetic preview
# data). The one place these numbers are literals.
DEFAULT_SCHEDULE_HOURS = {
    "morning": 6,
    "day": 8,
    "evening_earliest": 17,
    "evening_latest": 20,
    "night": 22,
}


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


PHASE_ORDER = ("Morning", "Day", "Evening", "Night")


def phase_marks(morning_ts: float, day_start_ts: float, evening_ts: float, night_ts: float) -> list:
    """Which phases actually occur today, and the instant each really starts.

    phase_at() above is a cascade of `t < boundary` tests in a fixed
    order, which quietly tolerates boundaries set out of order - a
    Morning time later than the Day time, say. Two things follow from
    that cascade, and neither is obvious from the four raw boundaries:

    1. A phase can be UNREACHABLE. With Morning at 10:00 and Day at
       08:00, nothing is ever Morning: by the time `t` clears 10:00 it
       has already passed the 08:00 test, so the day runs
       Night -> Day -> Evening -> Night.
    2. The phase that follows an unreachable one starts at the LATER
       boundary, not its own. In that same example Day starts at 10:00
       (Morning's boundary), not at its own 08:00.

    Both fall out of one rule: each phase effectively begins at the
    running maximum of the boundaries up to and including its own, and
    occupies the span up to the next phase's effective start - so it is
    real exactly when that span is non-empty.

    Returned as [(name, start_ts), ...] in order, omitting any phase
    that never happens. Night is a special case in both directions: the
    day always *opens* on Night (`t < morning_ts`), so its own boundary
    marks the RETURN to Night rather than its only appearance - and if
    nothing ever leaves Night (every other phase unreachable, as with a
    fully reversed schedule) there is no return to mark, so nothing is
    returned at all. A mark means "the phase changes here"; a day that is
    Night throughout changes nowhere.

    This exists because the boundaries are freely settable `time`
    entities, so an out-of-order schedule is a state a user can reach.
    The curve itself already handles it correctly, having gone through
    phase_at(); it's anything drawing the boundaries *directly* from the
    four timestamps - the dashboard card's phase labels and lines - that
    would otherwise show a phase that isn't happening, at a time it
    isn't happening at.
    """
    raw = (morning_ts, day_start_ts, evening_ts, night_ts)

    effective_starts = []
    running = None
    for ts in raw:
        running = ts if running is None else max(running, ts)
        effective_starts.append(running)

    marks = []
    for i, name in enumerate(PHASE_ORDER[:-1]):
        # Squeezed out by the next phase starting no later than it does.
        if effective_starts[i] < effective_starts[i + 1]:
            marks.append((name, effective_starts[i]))

    # Night last, and only if some other phase actually happens - see the
    # docstring. Without that check a fully reversed schedule (a day that
    # is Night from end to end) would draw a Night boundary partway
    # through, implying a change that never occurs.
    if marks:
        marks.append(("Night", effective_starts[-1]))
    return marks


def brightness_for_phase(
    day_phase: str,
    now_ts: float,
    night_ts: float,
    *,
    morning_brightness: int = DEFAULT_MORNING_BRIGHTNESS,
    day_brightness: int = DEFAULT_DAY_BRIGHTNESS,
    evening_brightness: int = DEFAULT_EVENING_BRIGHTNESS,
    night_brightness: int = DEFAULT_NIGHT_BRIGHTNESS,
) -> int:
    """Target brightness (0-255) for the given phase/instant."""
    if day_phase == "Morning":
        return morning_brightness
    if day_phase == "Day":
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
    morning_kelvin: int = DEFAULT_MORNING_KELVIN,
    day_end_kelvin: int = DEFAULT_DAY_END_KELVIN,
    evening_kelvin: int = DEFAULT_EVENING_KELVIN,
    night_kelvin: int = DEFAULT_NIGHT_KELVIN,
) -> int:
    """Target colour temperature (Kelvin) for the given phase/instant.

    morning_kelvin: Morning's steady value, and where Day's ramp starts
    from. day_end_kelvin: what Day ramps down to by the time Evening
    starts (also where Evening's own ramp starts). evening_kelvin:
    Evening's steady hold, after its opening ramp from day_end_kelvin.
    night_kelvin: what Evening's final hour fades toward, and what Night
    sits at."""
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
            # Clamped like every other ramp in this file (Day's above,
            # Evening's own opening ramp below, and brightness_for_phase's
            # equivalent fade, which clamps its output instead). Needed
            # because day_phase is a *parameter*, not derived from now_ts:
            # coordinator.py passes a manually-overridden phase alongside
            # the real current time, so "Evening" can legitimately be
            # asked for at an instant past night_ts. Unclamped, t goes
            # negative there and the interpolation extrapolates straight
            # through night_kelvin - the floor this fade is meant to
            # bottom out at - returning 2200K at 23:00 and 1708K by 23:59
            # on the defaults, below many bulbs' min_color_temp_kelvin.
            t = min(max((night_ts - now_ts) / 3600, 0), 1)
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
    morning_brightness: int = DEFAULT_MORNING_BRIGHTNESS,
    day_brightness: int = DEFAULT_DAY_BRIGHTNESS,
    evening_brightness: int = DEFAULT_EVENING_BRIGHTNESS,
    night_brightness: int = DEFAULT_NIGHT_BRIGHTNESS,
    morning_kelvin: int = DEFAULT_MORNING_KELVIN,
    day_end_kelvin: int = DEFAULT_DAY_END_KELVIN,
    evening_kelvin: int = DEFAULT_EVENING_KELVIN,
    night_kelvin: int = DEFAULT_NIGHT_KELVIN,
) -> dict:
    """brightness/kelvin/rgb_color for an already-known phase, in one
    call - the single orchestration point for
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
    the shape of what gets computed here ever changed. One copy now."""
    brightness = brightness_for_phase(
        day_phase,
        now_ts,
        night_ts,
        morning_brightness=morning_brightness,
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
        "rgb_color": kelvin_to_rgb(kelvin),
    }
