"""
These scenarios mirror ones validated live against the deployed
blueprint's Jinja before the port (same fake entities, same expected
groupings) - the Python and the Jinja it replaced agree on every case
here.
"""

import pytest

from grouping import MAX_BRIGHTNESS, build_groups
from fakes import make_lookup


def test_reachable_entities_are_excluded_from_every_group():
    lookup = make_lookup(
        {
            "light.a": {"state": "on", "attributes": {"brightness": 100}},
            "light.b": {"state": "unavailable", "attributes": {}},
            "light.c": {"state": "unknown", "attributes": {}},
        }
    )
    groups = build_groups(
        entities=["light.a", "light.b", "light.c"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
    )
    assert len(groups) == 1
    touched = groups[0].combined + groups[0].two_step + groups[0].needing_off
    assert "light.b" not in touched
    assert "light.c" not in touched


def test_update_group_splits_by_two_step_label():
    lookup = make_lookup(
        states={
            "light.kitchen_1": {"state": "on", "attributes": {"brightness": 180, "color_temp_kelvin": 3050}},
            "light.kitchen_2": {"state": "off", "attributes": {}},
            "light.kitchen_6": {"state": "unavailable", "attributes": {}},
        },
        device_of={"light.kitchen_1": "dev1", "light.kitchen_2": "dev2"},
        labels_of={"dev1": ["no_combined_transition"]},
    )
    groups = build_groups(
        entities=["light.kitchen_1", "light.kitchen_2", "light.kitchen_6"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
    )
    assert len(groups) == 1
    group = groups[0]
    assert group.brightness == 200
    assert group.two_step == ["light.kitchen_1"]  # labelled, needs updating (180/3050 != 200/3000)
    assert group.combined == ["light.kitchen_2"]  # unlabelled, currently off so needs turning on
    assert group.needing_off == []


def test_already_within_tolerance_is_skipped():
    lookup = make_lookup(
        states={
            "light.kitchen_1": {"state": "on", "attributes": {"brightness": 180, "color_temp_kelvin": 3050}},
        },
        device_of={"light.kitchen_1": "dev1"},
        labels_of={"dev1": ["no_combined_transition"]},
    )
    # Target exactly matches current state - nothing should need a command
    groups = build_groups(
        entities=["light.kitchen_1"],
        brightness_multipliers={},
        sensor_brightness=180,
        sensor_color_temp_kelvin=3050,
        lookup=lookup,
    )
    assert groups[0].combined == []
    assert groups[0].two_step == []


def test_tolerance_is_not_exact_match():
    lookup = make_lookup(
        states={"light.a": {"state": "on", "attributes": {"brightness": 199, "color_temp_kelvin": 3005}}},
    )
    # Within the default tolerances (brightness +-2, colour-temp +-10)
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
    )
    assert groups[0].combined == []

    # Just outside brightness tolerance
    lookup2 = make_lookup(
        states={"light.a": {"state": "on", "attributes": {"brightness": 197, "color_temp_kelvin": 3005}}},
    )
    groups2 = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup2,
    )
    assert groups2[0].combined == ["light.a"]


def test_a_mired_equivalent_color_temp_is_treated_as_already_set():
    # Real live incident: asked for 4373K, the bulb reports 4385K back -
    # a 12K gap, past the default 10K color_temp_tolerance, but both
    # values floor to the identical mired 228 via HA's own
    # color_temperature_kelvin_to_mired (the bulb's real native unit).
    # Without accounting for this, the light would get needlessly
    # re-commanded on every single tick forever, even though it's
    # already exactly correct.
    lookup = make_lookup(
        states={"light.a": {"state": "on", "attributes": {"brightness": 255, "color_temp_kelvin": 4385}}},
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=255,
        sensor_color_temp_kelvin=4373,
        lookup=lookup,
    )
    assert groups[0].combined == []


def test_multiplier_zero_only_targets_reachable_lights_not_already_off():
    lookup = make_lookup(
        states={
            "light.kitchen_1": {"state": "on", "attributes": {}},
            "light.kitchen_2": {"state": "off", "attributes": {}},
        }
    )
    groups = build_groups(
        entities=["light.kitchen_1", "light.kitchen_2"],
        brightness_multipliers={"light.kitchen_1": 0, "light.kitchen_2": 0},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
    )
    assert len(groups) == 1
    assert groups[0].brightness == 0
    assert groups[0].needing_off == ["light.kitchen_1"]


def test_null_or_false_multiplier_excludes_entity_entirely():
    lookup = make_lookup(states={"light.owned_elsewhere": {"state": "off", "attributes": {}}})
    groups = build_groups(
        entities=["light.owned_elsewhere"],
        brightness_multipliers={"light.owned_elsewhere": None},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
    )
    assert groups == []


def test_distinct_multipliers_form_separate_groups():
    lookup = make_lookup(
        states={
            "light.a": {"state": "off", "attributes": {}},
            "light.b": {"state": "off", "attributes": {}},
        }
    )
    groups = build_groups(
        entities=["light.a", "light.b"],
        brightness_multipliers={"light.a": 1, "light.b": 0.1},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
    )
    by_multiplier = {g.multiplier: g for g in groups}
    assert by_multiplier[1].brightness == 200
    assert by_multiplier[0.1].brightness == 20
    assert by_multiplier[1].combined == ["light.a"]
    assert by_multiplier[0.1].combined == ["light.b"]


@pytest.mark.parametrize(
    ("multiplier", "sensor_brightness", "expected"),
    [(0.001, 10, 1), (1.5, 200, MAX_BRIGHTNESS), (0.5, 200, 100)],
    ids=[
        # Floored at 1 so a tiny multiplier dims rather than silently
        # switching the light off - 0 is how you say "off", explicitly.
        "floors at 1, never accidentally off",
        # Capped so a template can just say 1.5 without knowing what the
        # curve is currently at.
        "caps at MAX_BRIGHTNESS",
        "scales in between",
    ],
)
def test_multiplier_arithmetic(multiplier, sensor_brightness, expected):
    lookup = make_lookup(states={"light.a": {"state": "off", "attributes": {}}})
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={"light.a": multiplier},
        sensor_brightness=sensor_brightness,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
    )
    assert groups[0].brightness == expected


def test_a_light_already_at_max_is_not_recommanded_when_the_multiplier_overshoots():
    """The reason the cap matters beyond ergonomics. light.turn_on
    validates brightness with vol.Clamp(0, 255), so an un-clamped target
    of 300 is silently written as 255 - and then the light reporting 255
    would never match a 300 target, so it'd be re-commanded on every
    tick forever. Capping keeps "at target" reachable."""
    lookup = make_lookup(
        states={"light.bright": {"state": "on", "attributes": {"brightness": 255, "color_temp_kelvin": 3000}}},
    )
    groups = build_groups(
        entities=["light.bright"],
        brightness_multipliers={"light.bright": 1.5},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
    )
    assert groups[0].combined == []
    assert groups[0].two_step == []


# Overrides: a light currently on whose live context.id doesn't match
# either of the two claims this integration itself last wrote it with
# under the *same owner_id* (per write_tracking.LastWriteTracker) is
# left exactly alone, even if it doesn't match the current adaptive
# target - regardless of *what* changed it (a person, another
# automation with no context.user_id of its own, a device regaining
# power, or a different owner_id entirely) - checked fresh against live
# state on every call, so nothing needs to be remembered here or
# explicitly expired. build_groups' own owner_id defaults to None,
# which skips this check altogether (every light is free to manage) -
# the explicit force path, tested separately below.
#
# The tests below through test_turning_the_light_off_ends_the_protection
# all use only `confirmed_*` - the steady-state case where there's no
# outstanding, unverified write in flight, which is the vast majority of
# ticks. The *promotion* behaviour itself - how a `pending` claim earns
# its way into `confirmed`, and how `confirmed` survives any number of
# consecutive dropped writes along the way - is a property of
# write_tracking.LastWriteTracker.async_record, not of this pure check,
# so it's exercised end-to-end against a real Store in
# tests/integration/test_services.py instead. What belongs here is
# narrower: given a specific {confirmed, pending} snapshot, does the
# check land on the right answer - see the "Confirmed vs pending" tests
# right after this section for that.


# --- EntityLookup.externally_set(): the adapter, not the decision table ---
#
# The decision table itself lives in override_protection.classify()/
# is_blocked() and is exercised directly in tests/test_override_protection.py,
# which is where cases about *what a claim means* belong. Re-walking that
# table through build_groups() costs ~25 lines per case to assert one
# boolean and proves nothing extra.
#
# What can only be tested here is the wiring: that externally_set() builds
# both claim dicts with every field classify() reads, and that build_groups()
# then drops an excluded entity from every bucket. That wiring has its own
# failure mode - it shipped once with the `observed` dict missing `target`
# and `secondary_context_id`, which left two correct fixes silently inert in
# production while every pure test still passed.


def test_an_externally_set_light_is_excluded_from_the_update_group():
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 40, "color_temp_kelvin": 6000},
                "context_id": "ctx-someone-else",
            }
        },
        observed_context_ids={"light.a": "ctx-ours"},
        observed_owner_ids={"light.a": "ours"},
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        owner_id="ours",
    )
    assert groups[0].combined == []


def test_an_externally_set_light_is_excluded_from_the_off_group_too():
    # The multiplier says this light should be off, but something else set
    # it on since our last write - that choice wins, it isn't forced off.
    lookup = make_lookup(
        states={"light.a": {"state": "on", "attributes": {}, "context_id": "ctx-someone-else"}},
        observed_context_ids={"light.a": "ctx-ours"},
        observed_owner_ids={"light.a": "ours"},
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={"light.a": 0},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        owner_id="ours",
    )
    assert groups[0].needing_off == []


# Every field classify() reads off a claim, checked once per claim, via a
# case whose outcome flips if that field never makes it out of the adapter.
# The light sits at 200/3000 while the target is 100/5000, so it always
# needs a write unless override protection excludes it.
_CLAIM_FIELD_CASES = [
    (
        "observed.target",
        dict(
            observed_context_ids={"light.a": "ctx-c"},
            observed_owner_ids={"light.a": "ours"},
            observed_targets={"light.a": {"brightness": 200, "color_temp_kelvin": 3000}},
        ),
        "ctx-unrelated",
        ["light.a"],
    ),
    (
        "latest.target",
        dict(
            observed_context_ids={"light.a": "ctx-c"},
            observed_owner_ids={"light.a": "ours"},
            latest_context_ids={"light.a": "ctx-p"},
            latest_owner_ids={"light.a": "ours"},
            latest_targets={"light.a": {"brightness": 200, "color_temp_kelvin": 3000}},
        ),
        "ctx-unrelated",
        ["light.a"],
    ),
    (
        "observed.secondary_context_id",
        dict(
            observed_context_ids={"light.a": "ctx-c"},
            observed_owner_ids={"light.a": "ours"},
            observed_secondary_context_ids={"light.a": "ctx-c-brightness-step"},
        ),
        "ctx-c-brightness-step",
        ["light.a"],
    ),
    (
        "latest.secondary_context_id",
        dict(
            observed_context_ids={"light.a": "ctx-c"},
            observed_owner_ids={"light.a": "ours"},
            latest_context_ids={"light.a": "ctx-p"},
            latest_owner_ids={"light.a": "ours"},
            latest_secondary_context_ids={"light.a": "ctx-p-brightness-step"},
        ),
        "ctx-p-brightness-step",
        ["light.a"],
    ),
    (
        "observed.owner_id",
        dict(
            observed_context_ids={"light.a": "ctx-c"},
            observed_owner_ids={"light.a": "someone-else"},
        ),
        "ctx-c",
        [],
    ),
    (
        "latest.owner_id",
        dict(
            observed_context_ids={"light.a": "ctx-c"},
            observed_owner_ids={"light.a": "ours"},
            latest_context_ids={"light.a": "ctx-p"},
            latest_owner_ids={"light.a": "someone-else"},
        ),
        "ctx-p",
        [],
    ),
]


@pytest.mark.parametrize(
    ("claim_kwargs", "live_context", "expected"),
    [case[1:] for case in _CLAIM_FIELD_CASES],
    ids=[case[0] for case in _CLAIM_FIELD_CASES],
)
def test_the_adapter_passes_every_claim_field_classify_reads(claim_kwargs, live_context, expected):
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 200, "color_temp_kelvin": 3000},
                "context_id": live_context,
            }
        },
        **claim_kwargs,
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=100,
        sensor_color_temp_kelvin=5000,
        lookup=lookup,
        owner_id="ours",
    )
    assert groups[0].combined == expected


@pytest.mark.parametrize(("owner_id", "force"), [(None, False), ("ours", True)], ids=["no owner_id", "force"])
def test_the_adapter_passes_the_bypasses_through(owner_id, force):
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 40, "color_temp_kelvin": 6000},
                "context_id": "ctx-someone-else",
            }
        },
        observed_context_ids={"light.a": "ctx-ours"},
        observed_owner_ids={"light.a": "someone-else"},
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        owner_id=owner_id,
        force=force,
    )
    assert groups[0].combined == ["light.a"]


def test_stale_target_still_updates_once_the_curve_moves():
    # The value-rescue must not become a permanent free pass: once the
    # curve has moved past what the claim recorded, the light needs a
    # write again. Reverting the rescue to an unconditional "external"
    # is invisible without this - the light is excluded either way when
    # it is still sitting on the recorded target.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 200, "color_temp_kelvin": 3000},
                "context_id": "ctx-device-echo-unrelated",
            }
        },
        observed_context_ids={"light.a": "ctx-confirmed-stale"},
        observed_owner_ids={"light.a": "ours"},
        latest_context_ids={"light.a": "ctx-pending-stale"},
        latest_owner_ids={"light.a": "ours"},
        latest_targets={"light.a": {"brightness": 200, "color_temp_kelvin": 3000}},
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=120,
        sensor_color_temp_kelvin=4200,
        lookup=lookup,
        owner_id="ours",
    )
    assert groups[0].combined == ["light.a"]



def test_supports_rgb_checks_supported_color_modes():
    lookup = make_lookup(
        states={
            "light.rgb": {"state": "on", "attributes": {"supported_color_modes": ["rgb", "color_temp"]}},
            "light.xy": {"state": "on", "attributes": {"supported_color_modes": ["xy"]}},
            "light.temp_only": {"state": "on", "attributes": {"supported_color_modes": ["color_temp"]}},
            "light.brightness_only": {"state": "on", "attributes": {"supported_color_modes": ["brightness"]}},
            "light.no_attr": {"state": "on", "attributes": {}},
        }
    )
    assert lookup.supports_rgb("light.rgb") is True
    assert lookup.supports_rgb("light.xy") is True
    assert lookup.supports_rgb("light.temp_only") is False
    assert lookup.supports_rgb("light.brightness_only") is False
    assert lookup.supports_rgb("light.no_attr") is False


def test_prefer_rgb_color_routes_rgb_capable_entities_only():
    lookup = make_lookup(
        states={
            "light.rgb_bulb": {"state": "off", "attributes": {"supported_color_modes": ["rgb"]}},
            "light.temp_bulb": {"state": "off", "attributes": {"supported_color_modes": ["color_temp"]}},
        }
    )
    groups = build_groups(
        entities=["light.rgb_bulb", "light.temp_bulb"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        prefer_rgb_color=True,
        rgb_color=(255, 180, 107),
    )
    assert len(groups) == 1
    group = groups[0]
    assert group.combined_rgb == ["light.rgb_bulb"]
    assert group.combined == ["light.temp_bulb"]


def test_prefer_rgb_color_off_behaves_identically_to_before_the_feature():
    # Same entities/state as the routing test above, but the toggle is
    # off - both lights (RGB-capable or not) must land in the plain
    # combined bucket, exactly as if prefer_rgb_color/rgb_color didn't
    # exist as parameters at all.
    lookup = make_lookup(
        states={
            "light.rgb_bulb": {"state": "off", "attributes": {"supported_color_modes": ["rgb"]}},
            "light.temp_bulb": {"state": "off", "attributes": {"supported_color_modes": ["color_temp"]}},
        }
    )
    groups = build_groups(
        entities=["light.rgb_bulb", "light.temp_bulb"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        prefer_rgb_color=False,
        rgb_color=(255, 180, 107),
    )
    assert len(groups) == 1
    group = groups[0]
    assert sorted(group.combined) == ["light.rgb_bulb", "light.temp_bulb"]
    assert group.combined_rgb == []
    assert group.two_step_rgb == []


def test_prefer_rgb_color_on_with_no_rgb_color_behaves_like_off():
    # Toggle on, but nothing to target (e.g. the sensor has no rgb_color
    # attribute) - same fallback as the toggle being off.
    lookup = make_lookup(
        states={"light.rgb_bulb": {"state": "off", "attributes": {"supported_color_modes": ["rgb"]}}}
    )
    groups = build_groups(
        entities=["light.rgb_bulb"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        prefer_rgb_color=True,
        rgb_color=None,
    )
    assert groups[0].combined == ["light.rgb_bulb"]
    assert groups[0].combined_rgb == []


def test_rgb_two_step_label_splits_within_the_rgb_bucket():
    lookup = make_lookup(
        states={
            "light.rgb_two_step": {"state": "off", "attributes": {"supported_color_modes": ["rgb"]}},
            "light.rgb_combined": {"state": "off", "attributes": {"supported_color_modes": ["rgb"]}},
        },
        device_of={"light.rgb_two_step": "dev1"},
        labels_of={"dev1": ["no_combined_transition"]},
    )
    groups = build_groups(
        entities=["light.rgb_two_step", "light.rgb_combined"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        prefer_rgb_color=True,
        rgb_color=(255, 180, 107),
    )
    assert groups[0].two_step_rgb == ["light.rgb_two_step"]
    assert groups[0].combined_rgb == ["light.rgb_combined"]


def test_rgb_tolerance_skips_already_close_lights():
    lookup = make_lookup(
        states={
            "light.rgb_bulb": {
                "state": "on",
                "attributes": {
                    "brightness": 200,
                    "supported_color_modes": ["rgb"],
                    "rgb_color": [253, 181, 108],  # within default tolerance (10) of the target below
                },
            }
        }
    )
    groups = build_groups(
        entities=["light.rgb_bulb"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        prefer_rgb_color=True,
        rgb_color=(255, 180, 107),
    )
    assert groups[0].combined_rgb == []

    # Just outside tolerance on one channel
    lookup2 = make_lookup(
        states={
            "light.rgb_bulb": {
                "state": "on",
                "attributes": {
                    "brightness": 200,
                    "supported_color_modes": ["rgb"],
                    "rgb_color": [255, 180, 90],  # 17 off target's blue channel
                },
            }
        }
    )
    groups2 = build_groups(
        entities=["light.rgb_bulb"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup2,
        prefer_rgb_color=True,
        rgb_color=(255, 180, 107),
    )
    assert groups2[0].combined_rgb == ["light.rgb_bulb"]


def test_rgb_external_override_and_reachability_still_apply():
    # supports_rgb doesn't bypass the existing externally-set/reachability
    # checks - they're evaluated the same way regardless of which bucket
    # an entity ends up in.
    lookup = make_lookup(
        states={
            "light.overridden": {
                "state": "on",
                "attributes": {"brightness": 10, "supported_color_modes": ["rgb"]},
                "context_id": "ctx-someone-else",
            },
            "light.unreachable": {"state": "unavailable", "attributes": {"supported_color_modes": ["rgb"]}},
        },
        observed_context_ids={"light.overridden": "ctx-ours"},
        observed_owner_ids={"light.overridden": "ours"},
    )
    groups = build_groups(
        entities=["light.overridden", "light.unreachable"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        prefer_rgb_color=True,
        rgb_color=(255, 180, 107),
        owner_id="ours",
    )
    assert groups[0].combined_rgb == []
    assert groups[0].two_step_rgb == []


# Colour-temperature targets are compared against the raw target OR the
# target narrowed to the bulb's own advertised range - never only the
# narrowed one. Both halves matter and each has a live incident behind it:
#
#  - Un-narrowed only: light.dining_room_1/kitchen_1 advertise max 6535 K
#    against a flat 6667 K Morning target. 132 K apart and not
#    mired-equivalent (floor(1e6/6535)=153 vs floor(1e6/6667)=149), so the
#    bulb sat at its ceiling and was re-commanded every tick, all phase.
#    HA does not clamp color_temp_kelvin for a natively COLOR_TEMP light -
#    the device does (confirmed against light/__init__.py).
#  - Narrowed only: light.utility_spot_1 advertises max 4000 K while
#    happily reporting 5813 K. Its advertised range simply isn't honest,
#    and comparing against 4000 would re-command it every tick instead.
_COLOUR_RANGE_CASES = [
    ("at its ceiling, target above it", 255, 6535, 2000, 6535, 255, 6667, []),
    ("at its floor, target below it", 80, 2202, 2202, 4000, 80, 1708, []),
    ("reachable target it hasn't met yet", 255, 6535, 2000, 6535, 255, 4000, ["light.a"]),
    ("publishes no range at all", 255, 6535, None, None, 255, 6667, ["light.a"]),
    ("reports outside its advertised range", 255, 5813, 2202, 4000, 255, 5813, []),
]


@pytest.mark.parametrize(
    ("brightness", "current_kelvin", "min_kelvin", "max_kelvin", "target_brightness", "target_kelvin", "expected"),
    [case[1:] for case in _COLOUR_RANGE_CASES],
    ids=[case[0] for case in _COLOUR_RANGE_CASES],
)
def test_colour_target_is_compared_against_both_the_raw_and_range_narrowed_value(
    brightness, current_kelvin, min_kelvin, max_kelvin, target_brightness, target_kelvin, expected
):
    attributes = {"brightness": brightness, "color_temp_kelvin": current_kelvin}
    if min_kelvin is not None:
        attributes["min_color_temp_kelvin"] = min_kelvin
        attributes["max_color_temp_kelvin"] = max_kelvin
    lookup = make_lookup(states={"light.a": {"state": "on", "attributes": attributes}})
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=target_brightness,
        sensor_color_temp_kelvin=target_kelvin,
        lookup=lookup,
    )
    assert groups[0].combined == expected
