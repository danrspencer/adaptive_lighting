"""
These scenarios mirror ones validated live against the deployed
blueprint's Jinja before the port (same fake entities, same expected
groupings) - the Python and the Jinja it replaced agree on every case
here.
"""

from adaptive_lighting import build_groups
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


def test_multiplier_floors_at_one_never_accidentally_off():
    lookup = make_lookup(states={"light.dim": {"state": "off", "attributes": {}}})
    groups = build_groups(
        entities=["light.dim"],
        brightness_multipliers={"light.dim": 0.001},
        sensor_brightness=10,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
    )
    assert groups[0].brightness == 1
