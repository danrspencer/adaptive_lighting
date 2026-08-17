"""
Pure-logic tests for two_step.py - no Home Assistant dependency, same
as test_curve.py/test_grouping.py/test_scenes.py (see tests/conftest.py
for how the module is imported bare).

The end-to-end half - registry scanning, the repair actually appearing,
and the fix flow applying labels - lives in
tests/integration/test_two_step_repair.py, since none of that can be
exercised without a real entity/device/label registry.
"""

from two_step import (
    DEFAULT_TWO_STEP_MODEL_PATTERNS,
    CandidateLight,
    describe,
    find_unlabelled_two_step_lights,
    model_matches,
    parse_extra_patterns,
)


def _light(entity_id="light.a", manufacturer="IKEA", model="TRADFRI bulb GU10, color/white spectrum, 345 lm"):
    return CandidateLight(
        entity_id=entity_id,
        device_id="dev-" + entity_id,
        name=entity_id.split(".")[-1].replace("_", " ").title(),
        manufacturer=manufacturer,
        model=model,
    )


class TestModelMatching:
    def test_the_shipped_default_matches_a_real_tradfri_gu10(self):
        assert model_matches(
            "IKEA", "TRADFRI bulb GU10, color/white spectrum, 345 lm", DEFAULT_TWO_STEP_MODEL_PATTERNS
        )

    def test_matching_is_case_insensitive(self):
        """Manufacturer strings are wildly inconsistent between
        integrations for the same physical bulb."""
        assert model_matches("ikea", "tradfri BULB gu10", ["*TRADFRI bulb*"])

    def test_a_different_manufacturers_bulb_does_not_match(self):
        assert not model_matches("Signify Netherlands B.V.", "Hue color spot", DEFAULT_TWO_STEP_MODEL_PATTERNS)

    def test_a_pattern_can_pin_the_manufacturer_alone(self):
        assert model_matches("IKEA", "KAJPLATS bulb", ["ikea*"])

    def test_missing_manufacturer_and_model_never_matches(self):
        """A device with no identity at all mustn't match a bare '*'
        pattern into flagging every light in the house."""
        assert not model_matches(None, None, ["*"])

    def test_empty_pattern_list_matches_nothing(self):
        assert not model_matches("IKEA", "TRADFRI bulb GU10", [])


class TestParseExtraPatterns:
    def test_splits_on_newlines_and_strips(self):
        assert parse_extra_patterns(" *foo*\n\n  *bar* \n") == ["*foo*", "*bar*"]

    def test_also_accepts_commas(self):
        assert parse_extra_patterns("*foo*, *bar*") == ["*foo*", "*bar*"]

    def test_blank_and_none_yield_nothing(self):
        """A misconfigured options field should fall back to the shipped
        defaults, never raise during setup."""
        assert parse_extra_patterns("") == []
        assert parse_extra_patterns(None) == []
        assert parse_extra_patterns("\n  \n") == []


class TestFindUnlabelled:
    def test_reports_a_matching_light_with_no_label(self):
        lights = [_light("light.spot_1")]
        found = find_unlabelled_two_step_lights(lights, DEFAULT_TWO_STEP_MODEL_PATTERNS, lambda c: False)
        assert [c.entity_id for c in found] == ["light.spot_1"]

    def test_ignores_a_matching_light_that_is_already_labelled(self):
        lights = [_light("light.spot_1")]
        found = find_unlabelled_two_step_lights(lights, DEFAULT_TWO_STEP_MODEL_PATTERNS, lambda c: True)
        assert found == []

    def test_ignores_a_non_matching_light_even_without_a_label(self):
        lights = [_light("light.hue", manufacturer="Signify", model="Hue color spot")]
        found = find_unlabelled_two_step_lights(lights, DEFAULT_TWO_STEP_MODEL_PATTERNS, lambda c: False)
        assert found == []

    def test_an_extra_pattern_widens_detection(self):
        """The per-install half of the list - a bulb the shipped
        defaults don't know about yet."""
        lights = [_light("light.odd", manufacturer="Acme", model="Weird Bulb 9000")]
        assert find_unlabelled_two_step_lights(lights, DEFAULT_TWO_STEP_MODEL_PATTERNS, lambda c: False) == []

        patterns = [*DEFAULT_TWO_STEP_MODEL_PATTERNS, *parse_extra_patterns("*weird bulb*")]
        found = find_unlabelled_two_step_lights(lights, patterns, lambda c: False)
        assert [c.entity_id for c in found] == ["light.odd"]

    def test_results_are_sorted_so_the_repair_text_is_stable(self):
        lights = [_light("light.c"), _light("light.a"), _light("light.b")]
        found = find_unlabelled_two_step_lights(lights, DEFAULT_TWO_STEP_MODEL_PATTERNS, lambda c: False)
        assert [c.entity_id for c in found] == ["light.a", "light.b", "light.c"]

    def test_per_light_label_check_is_respected_not_all_or_nothing(self):
        lights = [_light("light.a"), _light("light.b")]
        found = find_unlabelled_two_step_lights(
            lights, DEFAULT_TWO_STEP_MODEL_PATTERNS, lambda c: c.entity_id == "light.a"
        )
        assert [c.entity_id for c in found] == ["light.b"]


class TestDescribe:
    def test_lists_names_and_entity_ids(self):
        assert describe([_light("light.a")]) == "A (light.a)"

    def test_truncates_a_long_list(self):
        lights = [_light(f"light.spot_{i}") for i in range(12)]
        text = describe(lights, limit=3)
        assert text.endswith("and 9 more")
        assert text.count("(") == 3

    def test_empty_is_empty(self):
        assert describe([]) == ""
