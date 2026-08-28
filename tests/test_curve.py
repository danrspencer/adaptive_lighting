import pytest

from curve import (
    brightness_for_phase,
    kelvin_for_phase,
    kelvin_to_rgb,
    phase_at,
    phase_marks,
    targets_for_phase,
)

# A synthetic day used across every test in this file, expressed as
# seconds-of-day for readability (these functions only ever care about
# differences between timestamps, so an arbitrary epoch is fine):
#   06:00 morning, 08:00 day, 18:00 evening, 22:00 night
MORNING = 6 * 3600
DAY_START = 8 * 3600
EVENING = 18 * 3600
NIGHT = 22 * 3600


def _bri(phase, t, **kw):
    return brightness_for_phase(phase, t, MORNING, DAY_START, EVENING, NIGHT, **kw)


def _kel(phase, t, **kw):
    return kelvin_for_phase(phase, t, MORNING, DAY_START, EVENING, NIGHT, **kw)


def _targets(phase, t, **kw):
    return targets_for_phase(phase, t, EVENING, DAY_START, NIGHT, MORNING, **kw)


def test_phase_at_boundaries():
    assert phase_at(0, MORNING, DAY_START, EVENING, NIGHT) == "Night"
    assert phase_at(MORNING, MORNING, DAY_START, EVENING, NIGHT) == "Morning"
    assert phase_at(DAY_START, MORNING, DAY_START, EVENING, NIGHT) == "Day"
    assert phase_at(EVENING, MORNING, DAY_START, EVENING, NIGHT) == "Evening"
    assert phase_at(NIGHT, MORNING, DAY_START, EVENING, NIGHT) == "Night"
    assert phase_at(NIGHT + 3600, MORNING, DAY_START, EVENING, NIGHT) == "Night"


def test_brightness_morning_and_day_are_full():
    # Independently configurable (morning_brightness/day_brightness),
    # but share the same default - see test_brightness_custom_morning_and_day_values
    # for proof they're actually separate knobs.
    assert _bri("Morning", 0) == 255
    assert _bri("Day", 0) == 255


def test_brightness_night_is_dim():
    assert _bri("Night", 0) == 80


def test_brightness_evening_holds_then_fades():
    fade_start = NIGHT - 3600  # 21:00, evening_brightness_transition minutes before Night
    # Before the transition window: holds at its own value.
    assert _bri("Evening", fade_start - 1) == 180
    assert _bri("Evening", fade_start) == 180
    # Halfway: a plain linear interpolation now. This used to be 160 -
    # the old formula multiplied the span by 1.6, so brightness reached
    # the night value ~22 minutes early and sat there. That ratio existed
    # only because the original Jinja did it.
    assert _bri("Evening", fade_start + 1800) == 130
    # Arrives exactly on the boundary, which is the whole point of
    # transitions running *before* it.
    assert _bri("Evening", NIGHT) == 80


def test_kelvin_morning_is_fixed():
    assert _kel("Morning", 0) == 6667


def test_kelvin_night_is_fixed():
    assert _kel("Night", 0) == 2700


def test_kelvin_day_ramps_down_across_the_day():
    """Day is an ordinary phase now: it holds day_kelvin and eases to
    Evening's over its own transition. The default transition is longer
    than any Day can be, so it clamps to the whole phase - which is how
    the old hardcoded full-phase slide is expressed."""
    assert _kel("Day", DAY_START) == 6667
    # Ends on Evening's own value. There is no day_end_kelvin waypoint
    # any more - Day hands straight over to the next phase.
    assert _kel("Day", EVENING) == 3200
    one_third = DAY_START + (EVENING - DAY_START) / 3
    assert _kel("Day", one_third) == 5511


def test_kelvin_evening_holds_then_fades():
    """Evening has two segments now, not three. Its old opening ramp
    (4000 -> 3200 over the hour *after* the boundary) is gone: the change
    happens in Day's tail instead, so 18:00 IS the evening colour. That
    is the "if Morning is at 6am it IS the morning setting at 6am" rule
    applied at every boundary."""
    fade_start = NIGHT - 3600  # 21:00

    # Segment 1 - flat hold at its own value, from the boundary itself.
    assert _kel("Evening", EVENING) == 3200
    assert _kel("Evening", (EVENING + fade_start) / 2) == 3200
    assert _kel("Evening", fade_start) == 3200

    # Segment 2 - ramp to Night's value, arriving exactly on the boundary.
    assert _kel("Evening", fade_start + 1800) == 2950
    assert _kel("Evening", NIGHT) == 2700


def test_kelvin_night_kelvin_defaults_to_2700():
    # Same as test_kelvin_night_is_fixed, but explicit about why: this is
    # the default that keyword-only night_kelvin exists to override.
    assert _kel("Night", 0) == 2700
    assert _kel("Night", 0, night_kelvin=2700) == 2700


def test_kelvin_night_kelvin_lowers_night_and_evening_tail_only():
    fade_start = NIGHT - 3600  # 21:00

    # Night itself follows night_kelvin directly.
    assert _kel("Night", 0, night_kelvin=2000) == 2000

    # Evening's final-hour fade ramps from evening_kelvin down to
    # night_kelvin - continuous with segment 2's hold (still 3200 by
    # default) at fade_start, reaching night_kelvin exactly at the night
    # boundary.
    assert _kel("Evening", NIGHT, night_kelvin=2000) == 2000
    assert _kel("Evening", fade_start, night_kelvin=2000) == 3200

    # Morning, Day, and Evening's hold are all untouched by night_kelvin.
    assert _kel("Morning", 0, night_kelvin=2000) == 6667
    assert _kel("Day", DAY_START, night_kelvin=2000) == 6667
    assert _kel("Evening", EVENING, night_kelvin=2000) == 3200


def test_brightness_custom_morning_and_day_values():
    # morning_brightness and day_brightness are independent knobs -
    # setting one leaves the other at its own default.
    assert _bri("Morning", 0, morning_brightness=100) == 100
    assert _bri("Day", 0, morning_brightness=100) == 255
    assert _bri("Day", 0, day_brightness=200) == 200
    assert _bri("Morning", 0, day_brightness=200) == 255


def test_brightness_custom_evening_night_values():
    assert _bri("Night", 0, night_brightness=50) == 50
    fade_start = NIGHT - 3600  # 21:00
    assert _bri("Evening", fade_start - 1, evening_brightness=150) == 150


def test_kelvin_every_phase_has_its_own_independent_value():
    """Day used to have no colour of its own - it started at Morning's
    and ran to a day_end_kelvin. It is now an ordinary phase with its own
    value, which merely *defaults* to the same number Morning uses."""
    assert _kel("Morning", 0, morning_kelvin=5000) == 5000
    # Day no longer inherits Morning's value.
    assert _kel("Day", DAY_START, morning_kelvin=5000) == 6667
    assert _kel("Day", DAY_START, day_kelvin=3500) == 3500
    assert _kel("Evening", EVENING, evening_kelvin=3000) == 3000


def test_targets_for_phase_rgb_color_is_kelvin_converted():
    # rgb_color is always just the Kelvin -> RGB conversion of kelvin -
    # there's no separate RGB-only target.
    for phase, t in (("Morning", 0), ("Day", DAY_START), ("Evening", EVENING), ("Night", NIGHT)):
        targets = _targets(phase, t)
        assert targets["rgb_color"] == kelvin_to_rgb(targets["kelvin"])


def test_targets_for_phase_passes_through_morning_brightness():
    assert _targets("Morning", 0, morning_brightness=120)["brightness"] == 120


def test_targets_for_phase_defaults_match_existing_literals():
    # Zero kwargs reproduces the shipped defaults - a regression guard on
    # DEFAULT_CURVE_VALUES being what actually runs.
    assert _targets("Morning", 0)["brightness"] == 255
    assert _targets("Morning", 0)["kelvin"] == 6667
    assert _targets("Day", DAY_START)["kelvin"] == 6667
    assert _targets("Evening", EVENING)["brightness"] == 180
    assert _targets("Evening", EVENING)["kelvin"] == 3200
    assert _targets("Night", 0)["brightness"] == 80
    assert _targets("Night", 0)["kelvin"] == 2700


def test_kelvin_to_rgb_reference_points():
    # 6600K is the algorithm's own r/g/b crossover point - all three
    # channels clamp to exactly 255 there, a solid regression anchor.
    assert kelvin_to_rgb(6600) == (255, 255, 255)
    # Very warm: heavy red, no blue at all (temp <= 19 branch).
    assert kelvin_to_rgb(1000) == (255, 68, 0)
    # Very cool: blue-dominant, red channel well below max.
    assert kelvin_to_rgb(10000) == (202, 218, 255)


def test_kelvin_to_rgb_gets_warmer_as_kelvin_drops():
    # Monotonic sanity check rather than more hardcoded midpoints: lower
    # Kelvin should never mean *less* red or *more* blue.
    high = kelvin_to_rgb(6500)
    mid = kelvin_to_rgb(4000)
    low = kelvin_to_rgb(2000)
    assert low[0] >= mid[0] >= high[0]  # red non-increasing as Kelvin rises
    assert low[2] <= mid[2] <= high[2]  # blue non-decreasing as Kelvin rises


def test_evening_kelvin_fade_never_extrapolates_past_night_kelvin():
    """day_phase is a parameter, not derived from now_ts - coordinator.py
    passes a manually-overridden phase (select.<slug>_flare_phase)
    alongside the real current time, so "Evening" can legitimately be asked
    for at an instant past NIGHT. Unclamped, the fade's interpolation factor
    goes negative there and extrapolates straight through night_kelvin, the
    floor it used to bottom out at - now it holds Evening's own kelvin
    instead of even that floor, see _value_at's own docstring for why
    (confirmed live as a real bug: bottoming out at the *next* phase's
    value is exactly as wrong as unbounded extrapolation for a manual
    override, just less dramatically so)."""
    # One hour past Night: t would be -1.0 unclamped -> 2700 + 500*-1 = 2200K.
    assert _kel("Evening", NIGHT + 3600) == 3200
    # Just before midnight (two hours past Night): t would be ~-1.98 -> ~1708K.
    assert _kel("Evening", NIGHT + 7199) == 3200
    # And with a custom range, it holds that range's own evening value.
    assert _kel("Evening", NIGHT + 3600, evening_kelvin=4000, night_kelvin=2200) == 4000


def test_evening_kelvin_fade_is_unchanged_inside_its_real_window():
    """The clamp must not alter the fade itself - only its extrapolation."""
    # Exactly at Night: fully faded to night_kelvin.
    assert _kel("Evening", NIGHT) == 2700
    # Exactly at the fade's start (one hour before Night): still evening_kelvin.
    assert _kel("Evening", NIGHT - 3600) == 3200
    # Halfway through the fade: halfway between the two.
    assert _kel("Evening", NIGHT - 1800) == 2950


# --- phase_marks -----------------------------------------------------
#
# phase_marks() exists so anything drawing the boundaries directly (the
# dashboard card's labels and lines) agrees with what phase_at() actually
# does, including when the boundary times are set out of order - which
# they can be, since they're freely settable `time` entities.


def _observed_transitions(boundaries, step=60):
    """The day's real phase changes, read straight off phase_at().

    This is the ground truth phase_marks() has to reproduce: sample the
    whole day, collapse it into runs, and report where each run begins.

    A LEADING NIGHT run is dropped, and only a leading Night one. The day
    always opens on Night for any ordinary schedule, and that opening is
    the wrap-around of the same Night the schedule returns to at its
    night boundary - so reporting it would double-count. Any other
    leading run is a genuine phase start (Morning set to 00:00 really
    does start Morning at 00:00), and a day that is Night end to end
    collapses to a single run that this drops, leaving nothing - which is
    right, since nothing changes.
    """
    runs = []
    previous = None
    for i in range(0, 86400, step):
        current = phase_at(i, *boundaries)
        if current != previous:
            runs.append((current, float(i)))
        previous = current
    if runs and runs[0][0] == "Night":
        runs = runs[1:]
    return runs


# Hour-aligned so a 60s sampling grid lands exactly on every boundary.
# Includes in-order schedules, every kind of out-of-order one, and
# duplicates (two phases starting at the same instant).
_H = 3600
BOUNDARY_CASES = [
    (6 * _H, 8 * _H, 19 * _H, 22 * _H),  # normal
    (10 * _H, 8 * _H, 19 * _H, 22 * _H),  # Morning after Day -> no Morning
    (6 * _H, 8 * _H, 7 * _H, 22 * _H),  # Evening before Day -> no Day
    (6 * _H, 8 * _H, 19 * _H, 18 * _H),  # Night before Evening -> no Evening
    (6 * _H, 6 * _H, 19 * _H, 22 * _H),  # Morning == Day -> no Morning
    (6 * _H, 8 * _H, 8 * _H, 22 * _H),  # Day == Evening -> no Day
    (22 * _H, 20 * _H, 18 * _H, 16 * _H),  # fully reversed -> only Night
    (0, 8 * _H, 19 * _H, 22 * _H),  # Morning at midnight -> no leading Night
    (6 * _H, 6 * _H, 6 * _H, 6 * _H),  # everything collapsed onto one instant
]


@pytest.mark.parametrize("boundaries", BOUNDARY_CASES)
def test_phase_marks_matches_what_phase_at_actually_does(boundaries):
    assert phase_marks(*boundaries) == _observed_transitions(boundaries)


@pytest.mark.parametrize("boundaries", BOUNDARY_CASES)
def test_phase_marks_never_reports_a_phase_that_never_occurs(boundaries):
    """The bug this was written for: a Morning label on a day that never
    has a Morning."""
    occurring = {phase_at(i, *boundaries) for i in range(0, 86400, 60)}
    for name, _ in phase_marks(*boundaries):
        assert name in occurring, f"{name} is marked but never actually happens"


@pytest.mark.parametrize("boundaries", BOUNDARY_CASES)
def test_phase_marks_are_ordered_and_end_on_night(boundaries):
    marks = phase_marks(*boundaries)
    starts = [t for _, t in marks]
    assert starts == sorted(starts)
    assert len(starts) == len(set(starts)), "two phases cannot start at the same instant"
    # Either the day changes phase at some point and must therefore come
    # back to Night at the end, or it never changes at all and there is
    # nothing to mark. There is no in-between.
    assert marks == [] or marks[-1][0] == "Night"


def test_phase_marks_normal_schedule_is_all_four_in_order():
    assert phase_marks(6 * _H, 8 * _H, 19 * _H, 22 * _H) == [
        ("Morning", 6.0 * _H),
        ("Day", 8.0 * _H),
        ("Evening", 19.0 * _H),
        ("Night", 22.0 * _H),
    ]


def test_phase_after_an_unreachable_one_starts_at_the_later_boundary():
    """Not just "hide Morning" - Day genuinely begins at 10:00 here,
    Morning's boundary, not at its own 08:00."""
    marks = dict(phase_marks(10 * _H, 8 * _H, 19 * _H, 22 * _H))
    assert "Morning" not in marks
    assert marks["Day"] == 10.0 * _H


def test_a_day_that_never_leaves_night_has_no_marks():
    """A fully reversed schedule is Night from end to end. Marking its
    night boundary would draw a line implying a change that never
    happens - there is nothing to return to Night *from*."""
    assert phase_marks(22 * _H, 20 * _H, 18 * _H, 16 * _H) == []
    assert phase_marks(6 * _H, 6 * _H, 6 * _H, 6 * _H) == []


def test_a_phase_starting_at_midnight_is_still_marked():
    """Morning at 00:00 genuinely starts Morning at 00:00 - the day opens
    in it rather than in Night, so it is a real phase start, not the
    wrap-around Night that gets folded away."""
    assert phase_marks(0, 8 * _H, 19 * _H, 22 * _H) == [
        ("Morning", 0),
        ("Day", 8.0 * _H),
        ("Evening", 19.0 * _H),
        ("Night", 22.0 * _H),
    ]


# --- transitions -----------------------------------------------------
#
# One mechanism now covers every ramp: each phase holds its value and
# eases to the next phase's over the last N minutes of its own span. The
# transition sits *before* the boundary deliberately - see _value_at.


def test_a_zero_duration_is_a_hard_cut():
    """Not merely a degenerate case - "sometimes you want the visible
    *it's night now*" is why 0 has to mean an instant change rather than
    a minimum blend."""
    assert _kel("Evening", NIGHT - 1, evening_kelvin_transition=0) == 3200
    assert _kel("Night", NIGHT, evening_kelvin_transition=0) == 2700
    assert _bri("Evening", NIGHT - 1, evening_brightness_transition=0) == 180
    assert _bri("Night", NIGHT, night_brightness_transition=0) == 80


@pytest.mark.parametrize("phase,start", [("Morning", MORNING), ("Day", DAY_START), ("Evening", EVENING), ("Night", NIGHT)])
def test_every_phase_holds_its_own_value_at_its_own_start(phase, start):
    """"If Morning is at 6am I want it to BE the morning setting at 6am."
    That is the whole reason transitions run before a boundary rather
    than after it, and it has to hold for every phase and both channels
    regardless of how long the incoming transition was."""
    values = {"Morning": (255, 6667), "Day": (255, 6667), "Evening": (180, 3200), "Night": (80, 2700)}
    assert (_bri(phase, start), _kel(phase, start)) == values[phase]


def test_a_duration_longer_than_its_phase_covers_the_whole_phase():
    """Clamping is what lets Day's default of a full day mean "always be
    transitioning" without knowing how long Day actually is - and what
    stops any transition bleeding backwards into the phase before it."""
    # Evening is four hours here; a six-hour transition can only use four.
    assert _kel("Evening", EVENING, evening_kelvin_transition=360) == 3200
    quarter = EVENING + (NIGHT - EVENING) / 4
    assert _kel("Evening", quarter, evening_kelvin_transition=360) == _kel(
        "Evening", quarter, evening_kelvin_transition=240
    )


def test_brightness_and_colour_transition_independently():
    """They genuinely differ in the shipped defaults - Evening dims over
    an hour while Day's colour slides all afternoon - so one channel's
    duration must not move the other's."""
    t = NIGHT - 1800  # halfway through Evening's default hour
    both = _targets("Evening", t)
    assert (both["brightness"], both["kelvin"]) == (130, 2950)

    # Killing the colour transition leaves brightness mid-fade...
    colour_off = _targets("Evening", t, evening_kelvin_transition=0)
    assert (colour_off["brightness"], colour_off["kelvin"]) == (130, 3200)
    # ...and vice versa.
    brightness_off = _targets("Evening", t, evening_brightness_transition=0)
    assert (brightness_off["brightness"], brightness_off["kelvin"]) == (180, 2950)


def test_nights_transition_runs_before_morning_not_before_midnight():
    """Night is the only phase whose span crosses midnight - phase_at
    returns it both before MORNING and after NIGHT. Its handover is to
    Morning, so its transition belongs at the end of the early-morning
    segment, not at the end of the calendar day."""
    # Half an hour before Morning, with a 30 minute colour transition:
    # just starting to lift toward Morning's value.
    assert _kel("Night", MORNING - 1800, night_kelvin_transition=30) == 2700
    assert _kel("Night", MORNING - 900, night_kelvin_transition=30) == 4684
    assert _kel("Night", MORNING, night_kelvin_transition=30) == 6667
    # Late evening, on the far side of midnight, is untouched by it.
    assert _kel("Night", NIGHT + 3600, night_kelvin_transition=30) == 2700


def test_the_defaults_reproduce_evenings_hour_long_fade():
    """The one piece of today's shape that was explicitly worth keeping:
    "I like the current slow dimming into night"."""
    assert _bri("Evening", NIGHT - 3601) == 180
    assert _bri("Evening", NIGHT - 3600) == 180
    assert _bri("Evening", NIGHT) == 80
    assert _kel("Evening", NIGHT - 3600) == 3200
    assert _kel("Evening", NIGHT) == 2700


def test_a_phase_asked_for_outside_its_own_span_holds_its_own_value():
    """day_phase is a parameter, not derived from now_ts - the
    coordinator passes a manually-overridden phase alongside the real
    clock, so a phase can legitimately be asked for at an instant past
    its own end (the phase-override select forced to "Evening" while
    it's actually the middle of the night, say). It must show Evening's
    own configured value, not the value the old clamp-to-1 behaviour
    settled on instead (Night's, i.e. values[_NEXT_PHASE["Evening"]]) -
    confirmed live as a real bug via the phase-override select: forcing
    "Night" during actual evening real time showed Morning's brightness/
    colour instead of Night's own. See _value_at's own docstring."""
    assert _kel("Evening", NIGHT + 3600) == 3200
    assert _kel("Evening", NIGHT + 7199) == 3200
    assert _bri("Evening", NIGHT + 7199) == 180
    assert _kel("Evening", NIGHT + 3600, evening_kelvin=4000, night_kelvin=2200) == 4000


def test_every_overridden_phase_holds_its_own_value_at_a_real_time_outside_its_span():
    """The exact live incident _value_at's fix addresses, for every
    phase, not just Evening: with real time genuinely within Evening's
    own span, overriding to any OTHER phase must show that phase's own
    configured look, not whatever the old clamp-to-1 ramp settled toward
    (values[_NEXT_PHASE[<forced phase>]]) - confirmed live via the
    phase-override select. A single-phase test can't catch this: it
    needs a real now_ts genuinely outside the *forced* phase's own span,
    for more than one forced phase, to prove the fix generalises rather
    than happening to work for whichever phase the other test covers."""
    now_ts = EVENING + 3600  # comfortably inside Evening's own real span
    assert _bri("Morning", now_ts) == 255 and _kel("Morning", now_ts) == 6667
    assert _bri("Day", now_ts) == 255 and _kel("Day", now_ts) == 6667
    assert _bri("Night", now_ts) == 80 and _kel("Night", now_ts) == 2700
