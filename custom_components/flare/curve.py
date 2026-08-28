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
# Day starts at Morning's colour and spends the whole phase easing to
# Evening's - see DEFAULT_DAY_KELVIN_TRANSITION.
DEFAULT_DAY_KELVIN = DEFAULT_MORNING_KELVIN
DEFAULT_EVENING_KELVIN = 3200
DEFAULT_NIGHT_KELVIN = 2700

# How long before each phase ends to begin easing to the next phase's
# value, in minutes - one per phase per channel, named for the phase the
# transition runs *in*. 0 is a hard cut, which is a real choice: some
# boundaries should be visible.
#
# Day's Kelvin default is longer than any Day can be on purpose. Because
# a duration clamps to its own phase, "24 hours" reads as "always be
# transitioning", and keeps meaning that if the boundaries move - it is
# how the old hardcoded full-phase slide is expressed now that Day is an
# ordinary phase.
#
# Morning's do nothing on the shipped values, since Day holds the same
# brightness and colour Morning does and there is nothing to interpolate.
# They matter the moment Day is given its own.
DEFAULT_MORNING_BRIGHTNESS_TRANSITION = 60
DEFAULT_MORNING_KELVIN_TRANSITION = 60
DEFAULT_DAY_BRIGHTNESS_TRANSITION = 65
DEFAULT_DAY_KELVIN_TRANSITION = 1440
DEFAULT_EVENING_BRIGHTNESS_TRANSITION = 60
DEFAULT_EVENING_KELVIN_TRANSITION = 60
DEFAULT_NIGHT_BRIGHTNESS_TRANSITION = 30
DEFAULT_NIGHT_KELVIN_TRANSITION = 30

# Sixteen now: eight values and eight transitions, keyed exactly like
# coordinator.py's CURVE_KEYS - the one
# place config_flow.py (and anything else wanting the full default set)
# reads actual numbers from, rather than hand-copying each constant.
DEFAULT_CURVE_VALUES = {
    "morning_brightness": DEFAULT_MORNING_BRIGHTNESS,
    "morning_kelvin": DEFAULT_MORNING_KELVIN,
    "day_brightness": DEFAULT_DAY_BRIGHTNESS,
    "day_kelvin": DEFAULT_DAY_KELVIN,
    "evening_brightness": DEFAULT_EVENING_BRIGHTNESS,
    "evening_kelvin": DEFAULT_EVENING_KELVIN,
    "night_brightness": DEFAULT_NIGHT_BRIGHTNESS,
    "night_kelvin": DEFAULT_NIGHT_KELVIN,
    "morning_brightness_transition": DEFAULT_MORNING_BRIGHTNESS_TRANSITION,
    "morning_kelvin_transition": DEFAULT_MORNING_KELVIN_TRANSITION,
    "day_brightness_transition": DEFAULT_DAY_BRIGHTNESS_TRANSITION,
    "day_kelvin_transition": DEFAULT_DAY_KELVIN_TRANSITION,
    "evening_brightness_transition": DEFAULT_EVENING_BRIGHTNESS_TRANSITION,
    "evening_kelvin_transition": DEFAULT_EVENING_KELVIN_TRANSITION,
    "night_brightness_transition": DEFAULT_NIGHT_BRIGHTNESS_TRANSITION,
    "night_kelvin_transition": DEFAULT_NIGHT_KELVIN_TRANSITION,
}

# A representative day schedule (hour-of-day), not read by anything
# below - a single shared "sensible starting point" for anything that
# wants to seed or preview a schedule without real user input yet
# (time.py's default value for every new sensor's boundary-time
# entities, and the docs site's curve playground). The one place these
# numbers are literals.
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
    www/flare-curve-card.js's kelvinToRgb(), so the dashboard
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


SECONDS_PER_DAY = 86400

# Which phase each one hands over to. Night wraps back to Morning, which
# is what makes it the only phase whose span crosses midnight.
_NEXT_PHASE = {"Morning": "Day", "Day": "Evening", "Evening": "Night", "Night": "Morning"}


def _phase_span(day_phase: str, now_ts: float, boundaries: dict) -> tuple:
    """(start, end) of the phase's own stretch of the timeline.

    Every phase but Night is a plain [start, end) between two of today's
    boundaries. Night is two segments of one period - phase_at() returns
    "Night" both before morning_ts and after night_ts - so which one we
    are in decides whether its handover to Morning is today's or
    tomorrow's. Treating it as a single span that crosses midnight keeps
    the transition maths identical to every other phase's."""
    if day_phase == "Morning":
        return boundaries["morning"], boundaries["day"]
    if day_phase == "Day":
        return boundaries["day"], boundaries["evening"]
    if day_phase == "Evening":
        return boundaries["evening"], boundaries["night"]
    # Night, in whichever of its two segments now_ts falls.
    if now_ts >= boundaries["night"]:
        return boundaries["night"], boundaries["morning"] + SECONDS_PER_DAY
    return boundaries["night"] - SECONDS_PER_DAY, boundaries["morning"]


def _value_at(
    day_phase: str,
    now_ts: float,
    boundaries: dict,
    values: dict,
    duration_minutes: float,
) -> float:
    """This phase's value now, easing toward the next phase's over the
    last `duration_minutes` of the phase.

    The transition sits *before* the boundary, so the value arrives at
    the next phase's exactly as that phase begins - "if Morning is at
    6am, it IS the morning setting at 6am". A duration of 0 is therefore
    a hard cut, which is the point: some boundaries should be visible.

    The duration is clamped to the phase it runs in, so a value too long
    to fit simply means "the whole phase" rather than bleeding backwards
    into the phase before. Clamping happens here rather than by rewriting
    the config: phase lengths move daily (Evening tracks sunset), so a
    duration that fits in summer and not in winter has to keep working.

    The interpolation factor is clamped as well, and that is load-bearing
    for a different reason - day_phase is a *parameter*, not derived from
    now_ts. coordinator.py passes a manually-overridden phase alongside
    the real clock, so a phase can legitimately be asked for at an
    instant outside its own span. Unclamped, the ramp would extrapolate
    straight past the target and keep going.

    Past the phase's own span entirely - strictly past `span_end`, not
    merely reaching it - this returns `own` outright, rather than the
    fully-ramped next-phase value the clamp above would otherwise settle
    on. `now_ts == span_end` exactly still ramps all the way to t=1
    (the next phase's value): that instant is the boundary itself, where
    "the value arrives at the next phase's exactly as that phase begins"
    (see above) is meant to hold regardless of which phase asked for it.
    Confirmed live as a real bug: overriding the phase-override select to
    "Night" during actual evening real time - hours past Night's own
    span, not merely at its edge - showed Morning's brightness/colour
    (255/7000K) instead of Night's own (80/2700K), because the
    interpolation factor clamped to 1 - which is
    `values[_NEXT_PHASE["Night"]]`, Morning's value, not Night's own.
    This can only change behaviour for an overridden phase: phase_at()'s
    own natural output never lets now_ts get past a phase's own span_end
    for that same phase - the instant it does, phase_at() has already
    returned the *next* phase instead - so the full-day curve-preview
    computation (coordinator.py's _compute_curve_points, which always
    derives day_phase fresh via phase_at(t, ...) for that same t) is
    unaffected."""
    own = values[day_phase]
    span_start, span_end = _phase_span(day_phase, now_ts, boundaries)
    duration = min(max(duration_minutes, 0) * 60, max(span_end - span_start, 0))
    if duration <= 0:
        return own
    ramp_start = span_end - duration
    if now_ts <= ramp_start or now_ts > span_end:
        return own
    t = _clamp((now_ts - ramp_start) / duration, 0, 1)
    return own + (values[_NEXT_PHASE[day_phase]] - own) * t


def brightness_for_phase(
    day_phase: str,
    now_ts: float,
    morning_ts: float,
    day_start_ts: float,
    evening_ts: float,
    night_ts: float,
    *,
    morning_brightness: int = DEFAULT_MORNING_BRIGHTNESS,
    day_brightness: int = DEFAULT_DAY_BRIGHTNESS,
    evening_brightness: int = DEFAULT_EVENING_BRIGHTNESS,
    night_brightness: int = DEFAULT_NIGHT_BRIGHTNESS,
    morning_brightness_transition: float = DEFAULT_MORNING_BRIGHTNESS_TRANSITION,
    day_brightness_transition: float = DEFAULT_DAY_BRIGHTNESS_TRANSITION,
    evening_brightness_transition: float = DEFAULT_EVENING_BRIGHTNESS_TRANSITION,
    night_brightness_transition: float = DEFAULT_NIGHT_BRIGHTNESS_TRANSITION,
) -> int:
    """Target brightness (0-255) for the given phase/instant."""
    return round(
        _value_at(
            day_phase,
            now_ts,
            {"morning": morning_ts, "day": day_start_ts, "evening": evening_ts, "night": night_ts},
            {
                "Morning": morning_brightness,
                "Day": day_brightness,
                "Evening": evening_brightness,
                "Night": night_brightness,
            },
            {
                "Morning": morning_brightness_transition,
                "Day": day_brightness_transition,
                "Evening": evening_brightness_transition,
                "Night": night_brightness_transition,
            }[day_phase],
        )
    )


def kelvin_for_phase(
    day_phase: str,
    now_ts: float,
    morning_ts: float,
    day_start_ts: float,
    evening_ts: float,
    night_ts: float,
    *,
    morning_kelvin: int = DEFAULT_MORNING_KELVIN,
    day_kelvin: int = DEFAULT_DAY_KELVIN,
    evening_kelvin: int = DEFAULT_EVENING_KELVIN,
    night_kelvin: int = DEFAULT_NIGHT_KELVIN,
    morning_kelvin_transition: float = DEFAULT_MORNING_KELVIN_TRANSITION,
    day_kelvin_transition: float = DEFAULT_DAY_KELVIN_TRANSITION,
    evening_kelvin_transition: float = DEFAULT_EVENING_KELVIN_TRANSITION,
    night_kelvin_transition: float = DEFAULT_NIGHT_KELVIN_TRANSITION,
) -> int:
    """Target colour temperature (Kelvin) for the given phase/instant.

    Every phase holds its own value and then eases to the next phase's
    over its own transition - there is no special case for Day any more.
    Day's default transition is longer than any Day can be, which is how
    "slide from Morning's colour to Evening's across the whole day" is
    expressed."""
    return round(
        _value_at(
            day_phase,
            now_ts,
            {"morning": morning_ts, "day": day_start_ts, "evening": evening_ts, "night": night_ts},
            {
                "Morning": morning_kelvin,
                "Day": day_kelvin,
                "Evening": evening_kelvin,
                "Night": night_kelvin,
            },
            {
                "Morning": morning_kelvin_transition,
                "Day": day_kelvin_transition,
                "Evening": evening_kelvin_transition,
                "Night": night_kelvin_transition,
            }[day_phase],
        )
    )


def targets_for_phase(
    day_phase: str,
    now_ts: float,
    evening_ts: float,
    day_start_ts: float,
    night_ts: float,
    morning_ts: float,
    **curve_values,
) -> dict:
    """brightness/kelvin/rgb_color for an already-known phase, in one
    call - the single orchestration point for
    brightness_for_phase/kelvin_for_phase/kelvin_to_rgb.

    Takes day_phase rather than computing it via phase_at() itself
    because some callers need to substitute a different phase first
    (coordinator.py's manual override reads phase_at()'s result but then
    may replace it with the phase-override select's value before
    computing brightness/kelvin from it) - phase_at() stays a separate
    call so that substitution has somewhere to happen. Callers that don't
    need it can just call phase_at() immediately before this.

    Curve values arrive as **kwargs and are split by name rather than
    listed twice: there are sixteen of them now (four phases x value and
    transition x brightness and Kelvin), and re-declaring every one here
    only to pass it straight through was the largest source of
    copy-paste in this file. Unknown keys raise from the callee, so a
    typo still fails loudly rather than being silently dropped."""
    boundaries = (now_ts, morning_ts, day_start_ts, evening_ts, night_ts)
    brightness = brightness_for_phase(
        day_phase, *boundaries, **{k: v for k, v in curve_values.items() if "brightness" in k}
    )
    kelvin = kelvin_for_phase(
        day_phase, *boundaries, **{k: v for k, v in curve_values.items() if "kelvin" in k}
    )
    return {
        "brightness": brightness,
        "kelvin": kelvin,
        "rgb_color": kelvin_to_rgb(kelvin),
    }
