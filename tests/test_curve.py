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
    assert brightness_for_phase("Morning", 0, NIGHT) == 255
    assert brightness_for_phase("Day", 0, NIGHT) == 255


def test_brightness_night_is_dim():
    assert brightness_for_phase("Night", 0, NIGHT) == 80


def test_brightness_evening_holds_then_fades():
    fade_start = NIGHT - 3600  # 21:00
    # Before the fade window: holds at 180
    assert brightness_for_phase("Evening", fade_start - 1, NIGHT) == 180
    # Start of the fade: still ~180 (t=1, clamped)
    assert brightness_for_phase("Evening", fade_start, NIGHT) == 180
    # Halfway through the fade: 80 + 160*0.5 = 160
    assert brightness_for_phase("Evening", fade_start + 1800, NIGHT) == 160
    # At night boundary: fully faded to 80
    assert brightness_for_phase("Evening", NIGHT, NIGHT) == 80


def test_kelvin_morning_is_fixed():
    assert kelvin_for_phase("Morning", 0, EVENING, DAY_START, NIGHT) == 6667


def test_kelvin_night_is_fixed():
    assert kelvin_for_phase("Night", 0, EVENING, DAY_START, NIGHT) == 2700


def test_kelvin_day_ramps_down_across_the_day():
    assert kelvin_for_phase("Day", DAY_START, EVENING, DAY_START, NIGHT) == 6667
    assert kelvin_for_phase("Day", EVENING, EVENING, DAY_START, NIGHT) == 4000
    # A third of the way through the day: 6667 - 2667/3 = 5778 exactly
    one_third = DAY_START + (EVENING - DAY_START) / 3
    assert kelvin_for_phase("Day", one_third, EVENING, DAY_START, NIGHT) == 5778


def test_kelvin_evening_has_three_segments():
    fade_start = NIGHT - 3600  # 21:00
    hold_start = min(EVENING + 3600, fade_start)  # 19:00

    # Segment 1 - ramp 4000 -> 3200 from evening start to hold_start
    assert kelvin_for_phase("Evening", EVENING, EVENING, DAY_START, NIGHT) == 4000
    assert kelvin_for_phase("Evening", hold_start, EVENING, DAY_START, NIGHT) == 3200

    # Segment 2 - flat hold at 3200
    midpoint = (hold_start + fade_start) / 2
    assert kelvin_for_phase("Evening", midpoint, EVENING, DAY_START, NIGHT) == 3200

    # Segment 3 - ramp 3200 -> 2700 from fade_start to night
    assert kelvin_for_phase("Evening", fade_start, EVENING, DAY_START, NIGHT) == 3200
    assert kelvin_for_phase("Evening", NIGHT, EVENING, DAY_START, NIGHT) == 2700
    assert kelvin_for_phase("Evening", fade_start + 1800, EVENING, DAY_START, NIGHT) == 2950


def test_kelvin_night_kelvin_defaults_to_2700():
    # Same as test_kelvin_night_is_fixed, but explicit about why: this is
    # the default that keyword-only night_kelvin exists to override.
    assert kelvin_for_phase("Night", 0, EVENING, DAY_START, NIGHT) == 2700
    assert kelvin_for_phase("Night", 0, EVENING, DAY_START, NIGHT, night_kelvin=2700) == 2700


def test_kelvin_night_kelvin_lowers_night_and_evening_tail_only():
    fade_start = NIGHT - 3600  # 21:00

    # Night itself follows night_kelvin directly.
    assert kelvin_for_phase("Night", 0, EVENING, DAY_START, NIGHT, night_kelvin=2000) == 2000

    # Evening's final-hour fade ramps from evening_kelvin down to
    # night_kelvin - continuous with segment 2's hold (still 3200 by
    # default) at fade_start, reaching night_kelvin exactly at the night
    # boundary.
    assert kelvin_for_phase("Evening", NIGHT, EVENING, DAY_START, NIGHT, night_kelvin=2000) == 2000
    assert kelvin_for_phase("Evening", fade_start, EVENING, DAY_START, NIGHT, night_kelvin=2000) == 3200

    # Morning, Day, and Evening's earlier ramp/hold (before the final
    # hour) are all untouched by night_kelvin.
    assert kelvin_for_phase("Morning", 0, EVENING, DAY_START, NIGHT, night_kelvin=2000) == 6667
    assert kelvin_for_phase("Day", DAY_START, EVENING, DAY_START, NIGHT, night_kelvin=2000) == 6667
    assert kelvin_for_phase("Evening", EVENING, EVENING, DAY_START, NIGHT, night_kelvin=2000) == 4000


def test_brightness_custom_morning_and_day_values():
    # morning_brightness and day_brightness are independent knobs -
    # setting one leaves the other at its own default.
    assert brightness_for_phase("Morning", 0, NIGHT, morning_brightness=100) == 100
    assert brightness_for_phase("Day", 0, NIGHT, morning_brightness=100) == 255
    assert brightness_for_phase("Day", 0, NIGHT, day_brightness=200) == 200
    assert brightness_for_phase("Morning", 0, NIGHT, day_brightness=200) == 255


def test_brightness_custom_evening_night_values():
    assert brightness_for_phase("Night", 0, NIGHT, night_brightness=50) == 50
    fade_start = NIGHT - 3600  # 21:00
    assert brightness_for_phase("Evening", fade_start - 1, NIGHT, evening_brightness=150) == 150


def test_kelvin_custom_morning_day_end_evening_values():
    assert kelvin_for_phase("Morning", 0, EVENING, DAY_START, NIGHT, morning_kelvin=5000) == 5000
    assert kelvin_for_phase("Day", DAY_START, EVENING, DAY_START, NIGHT, morning_kelvin=5000) == 5000
    assert kelvin_for_phase("Day", EVENING, EVENING, DAY_START, NIGHT, day_end_kelvin=3500) == 3500
    assert kelvin_for_phase("Evening", EVENING, EVENING, DAY_START, NIGHT, day_end_kelvin=3500) == 3500
    hold_start = min(EVENING + 3600, NIGHT - 3600)
    assert kelvin_for_phase("Evening", hold_start, EVENING, DAY_START, NIGHT, evening_kelvin=3000) == 3000


def test_targets_for_phase_rgb_color_is_kelvin_converted():
    # rgb_color is always just the Kelvin -> RGB conversion of kelvin -
    # there's no separate RGB-only target.
    for phase, t in (("Morning", 0), ("Day", DAY_START), ("Evening", EVENING), ("Night", NIGHT)):
        targets = targets_for_phase(phase, t, EVENING, DAY_START, NIGHT)
        assert targets["rgb_color"] == kelvin_to_rgb(targets["kelvin"])


def test_targets_for_phase_passes_through_morning_brightness():
    assert targets_for_phase("Morning", 0, EVENING, DAY_START, NIGHT, morning_brightness=120)["brightness"] == 120


def test_targets_for_phase_defaults_match_existing_literals():
    # Zero new kwargs reproduces every hardcoded literal from before the
    # curve.py generalization exactly - a regression guard for that change.
    assert targets_for_phase("Morning", 0, EVENING, DAY_START, NIGHT)["brightness"] == 255
    assert targets_for_phase("Morning", 0, EVENING, DAY_START, NIGHT)["kelvin"] == 6667
    assert targets_for_phase("Day", DAY_START, EVENING, DAY_START, NIGHT)["kelvin"] == 6667
    assert targets_for_phase("Evening", EVENING, EVENING, DAY_START, NIGHT)["brightness"] == 180
    assert targets_for_phase("Evening", EVENING, EVENING, DAY_START, NIGHT)["kelvin"] == 4000
    assert targets_for_phase("Night", 0, EVENING, DAY_START, NIGHT)["brightness"] == 80
    assert targets_for_phase("Night", 0, EVENING, DAY_START, NIGHT)["kelvin"] == 2700


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
    floor it's supposed to bottom out at."""
    # One hour past Night: t would be -1.0 unclamped -> 2700 + 500*-1 = 2200K.
    assert kelvin_for_phase("Evening", NIGHT + 3600, EVENING, DAY_START, NIGHT) == 2700
    # Just before midnight (two hours past Night): t would be ~-1.98 -> ~1708K.
    assert kelvin_for_phase("Evening", NIGHT + 7199, EVENING, DAY_START, NIGHT) == 2700
    # And with a custom range, it still bottoms out at that range's own floor.
    assert kelvin_for_phase("Evening", NIGHT + 3600, EVENING, DAY_START, NIGHT, evening_kelvin=4000, night_kelvin=2200) == 2200


def test_evening_kelvin_fade_is_unchanged_inside_its_real_window():
    """The clamp must not alter the fade itself - only its extrapolation."""
    # Exactly at Night: fully faded to night_kelvin.
    assert kelvin_for_phase("Evening", NIGHT, EVENING, DAY_START, NIGHT) == 2700
    # Exactly at the fade's start (one hour before Night): still evening_kelvin.
    assert kelvin_for_phase("Evening", NIGHT - 3600, EVENING, DAY_START, NIGHT) == 3200
    # Halfway through the fade: halfway between the two.
    assert kelvin_for_phase("Evening", NIGHT - 1800, EVENING, DAY_START, NIGHT) == 2950


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
