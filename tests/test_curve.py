from adaptive_lighting import brightness_for_phase, kelvin_for_phase, phase_at

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
