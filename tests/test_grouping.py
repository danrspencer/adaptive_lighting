"""
These scenarios mirror ones validated live against the deployed
blueprint's Jinja before the port (same fake entities, same expected
groupings) - the Python and the Jinja it replaced agree on every case
here.
"""

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


def test_multiplier_caps_at_max_brightness():
    """A multiplier above 1 means "as bright as it goes", so a template
    can just say 1.5 without knowing what the curve is currently at."""
    lookup = make_lookup(states={"light.bright": {"state": "off", "attributes": {}}})
    groups = build_groups(
        entities=["light.bright"],
        brightness_multipliers={"light.bright": 1.5},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
    )
    assert groups[0].brightness == MAX_BRIGHTNESS


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


def test_externally_set_light_is_not_recommanded_even_when_mismatched():
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 40, "color_temp_kelvin": 6000},
                "context_id": "ctx-someone-else",
            }
        },
        confirmed_context_ids={"light.a": "ctx-ours"},
        confirmed_owner_ids={"light.a": "ours"},
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
    assert groups[0].two_step == []


def test_externally_set_light_is_protected_from_being_turned_off_too():
    # brightness_multiplier says this light should be off, but something
    # else set it on since our last write - that choice wins, it isn't
    # forced off
    lookup = make_lookup(
        states={"light.a": {"state": "on", "attributes": {}, "context_id": "ctx-someone-else"}},
        confirmed_context_ids={"light.a": "ctx-ours"},
        confirmed_owner_ids={"light.a": "ours"},
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


def test_no_write_record_is_not_treated_as_externally_set():
    # Same mismatched state as the protected case above, but we've never
    # recorded a write for this entity at all (a brand new entity, or
    # the very first tick before anything's been recorded - also what a
    # HA restart looks like before write_tracking.py's persisted store
    # reloads) - this light gets corrected normally rather than getting
    # stuck unmanaged forever, even though a real owner_id was given.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 40, "color_temp_kelvin": 6000},
                "context_id": "ctx-whatever",
            }
        }
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        owner_id="ours",
    )
    assert groups[0].combined == ["light.a"]


def test_our_own_last_write_is_not_treated_as_externally_set():
    # The light's current context.id matches what we last wrote it with,
    # under the same owner_id we're calling with now - nothing has
    # touched it since our own last apply_lighting call, so it's still
    # ours to manage normally.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 40, "color_temp_kelvin": 6000},
                "context_id": "ctx-ours",
            }
        },
        confirmed_context_ids={"light.a": "ctx-ours"},
        confirmed_owner_ids={"light.a": "ours"},
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        owner_id="ours",
    )
    assert groups[0].combined == ["light.a"]


def test_different_owner_id_is_treated_as_externally_set_even_with_matching_context():
    # Nothing has touched the light since the recorded write (context.id
    # still matches), but that write was made under a *different*
    # owner_id (e.g. a different room's automation) - from this caller's
    # perspective that's still someone else's write, not mine.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 40, "color_temp_kelvin": 6000},
                "context_id": "ctx-shared",
            }
        },
        confirmed_context_ids={"light.a": "ctx-shared"},
        confirmed_owner_ids={"light.a": "other_automation"},
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


def test_no_owner_id_forces_past_the_check():
    # Omitting owner_id entirely is the explicit force/override path -
    # the light is written regardless of what last touched it, even
    # though there's a recorded write from a mismatched context under a
    # different owner_id right here.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 40, "color_temp_kelvin": 6000},
                "context_id": "ctx-someone-else",
            }
        },
        confirmed_context_ids={"light.a": "ctx-ours"},
        confirmed_owner_ids={"light.a": "ours"},
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
    )
    assert groups[0].combined == ["light.a"]


def test_force_true_bypasses_the_check_even_with_a_matching_owner_id_given():
    # force=True bypasses the check outright, regardless of owner_id and
    # regardless of how mismatched the recorded context/owner are.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 40, "color_temp_kelvin": 6000},
                "context_id": "ctx-someone-else",
            }
        },
        confirmed_context_ids={"light.a": "ctx-ours"},
        confirmed_owner_ids={"light.a": "someone_else"},
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        owner_id="ours",
        force=True,
    )
    assert groups[0].combined == ["light.a"]


def test_a_write_recorded_with_no_owner_id_does_not_block_a_later_owner_id_check():
    # A write recorded with no owner_id at all (the caller omitted it, or
    # used force without passing one) doesn't count against anyone later
    # - there was no claim to conflict with. This is what makes force
    # actually useful: force a write through *with* an owner_id, and a
    # later non-forced call under that same owner_id still needs this
    # fallback for the case where an *earlier* write (before force
    # existed, or from an anonymous caller) left no owner on record.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 40, "color_temp_kelvin": 6000},
                "context_id": "ctx-forced",
            }
        },
        confirmed_context_ids={"light.a": "ctx-forced"},
        # No confirmed_owner_ids entry for light.a - the earlier write
        # that produced ctx-forced was made with no owner_id at all.
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        owner_id="ours",
    )
    assert groups[0].combined == ["light.a"]


def test_force_write_is_recognised_by_a_later_non_forced_call_with_the_same_owner_id():
    # The actual bug this design fixes: force a write through *with* an
    # owner_id, and a later, non-forced call under that same owner_id
    # (context unchanged since) must still recognise it as its own -
    # not find an orphaned record and treat it as external.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 200, "color_temp_kelvin": 3000},
                "context_id": "ctx-forced-write",
            }
        },
        confirmed_context_ids={"light.a": "ctx-forced-write"},
        confirmed_owner_ids={"light.a": "ours"},
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        owner_id="ours",
    )
    # Already at target and recognised as our own write - correctly
    # left alone by the *tolerance* check, not the override check.
    assert groups[0].combined == []
    lookup_mismatched = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 40, "color_temp_kelvin": 6000},
                "context_id": "ctx-forced-write",
            }
        },
        confirmed_context_ids={"light.a": "ctx-forced-write"},
        confirmed_owner_ids={"light.a": "ours"},
    )
    groups_mismatched = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup_mismatched,
        owner_id="ours",
    )
    assert groups_mismatched[0].combined == ["light.a"]


def test_turning_the_light_off_ends_the_protection():
    # Same mismatched context.id as the protected case, but the light is
    # now off - there's nothing to protect, and a later "on" is a fresh
    # decision
    lookup = make_lookup(
        states={"light.a": {"state": "off", "attributes": {}, "context_id": "ctx-someone-else"}},
        confirmed_context_ids={"light.a": "ctx-ours"},
        confirmed_owner_ids={"light.a": "ours"},
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        owner_id="ours",
    )
    assert groups[0].combined == ["light.a"]


# Confirmed vs pending: which of the two claims the live context.id
# matches, and what that implies, given a specific {confirmed, pending}
# snapshot. The *dynamic* promotion behaviour (how a snapshot like this
# actually comes to exist over a sequence of real writes) is exercised
# separately in tests/integration/test_services.py against a real Store.


def test_matching_pending_is_recognised_as_ours_even_with_a_different_confirmed():
    # The most recent write landed (context matches pending) - ours,
    # regardless of what confirmed still says from before it.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 40, "color_temp_kelvin": 6000},
                "context_id": "ctx-pending",
            }
        },
        confirmed_context_ids={"light.a": "ctx-confirmed"},
        confirmed_owner_ids={"light.a": "ours"},
        pending_context_ids={"light.a": "ctx-pending"},
        pending_owner_ids={"light.a": "ours"},
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        owner_id="ours",
    )
    assert groups[0].combined == ["light.a"]


def test_matching_confirmed_survives_any_number_of_stale_pending_attempts():
    # Live context is still the OLD confirmed value - pending never
    # landed (could be one dropped attempt or a dozen; the check itself
    # can't tell the difference, and doesn't need to). Still ours.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 40, "color_temp_kelvin": 6000},
                "context_id": "ctx-confirmed",
            }
        },
        confirmed_context_ids={"light.a": "ctx-confirmed"},
        confirmed_owner_ids={"light.a": "ours"},
        pending_context_ids={"light.a": "ctx-pending-attempt-47"},
        pending_owner_ids={"light.a": "ours"},
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        owner_id="ours",
    )
    assert groups[0].combined == ["light.a"]


def test_matching_neither_confirmed_nor_pending_is_externally_set():
    # A confirmed baseline exists, but live context matches neither it
    # nor the outstanding pending attempt - something genuinely
    # different has touched this light.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 40, "color_temp_kelvin": 6000},
                "context_id": "ctx-someone-else",
            }
        },
        confirmed_context_ids={"light.a": "ctx-confirmed"},
        confirmed_owner_ids={"light.a": "ours"},
        pending_context_ids={"light.a": "ctx-pending"},
        pending_owner_ids={"light.a": "ours"},
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


def test_context_mismatch_is_forgiven_when_live_values_match_pendings_target():
    # Neither confirmed nor pending's context.id matches live - but the
    # light's current brightness/color_temp are exactly what pending's
    # own recorded target asked for. This is what a device's real
    # confirmation looks like when it lands more than 5 seconds after
    # the service call (HA's own Entity._context expiry) rather than a
    # genuine external touch - see externally_set()'s docstring.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 200, "color_temp_kelvin": 3000},
                "context_id": "ctx-device-echo-unrelated",
            }
        },
        confirmed_context_ids={"light.a": "ctx-confirmed-stale"},
        confirmed_owner_ids={"light.a": "ours"},
        pending_context_ids={"light.a": "ctx-pending-stale"},
        pending_owner_ids={"light.a": "ours"},
        pending_targets={"light.a": {"brightness": 200, "color_temp_kelvin": 3000}},
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
    assert groups[0].needing_off == []
    # Not externally set, but already at target - no write needed either
    # way. Confirmed via a *different* target: if the curve had moved on
    # since, the light would come back into combined normally (see the
    # next test).


def test_context_mismatch_rescue_still_checks_owner_id():
    # Bundled correctness fix in override_protection.classify(): the
    # value-rescue used to skip the owner check entirely, so a light
    # whose live value happened to match a *different* owner's pending
    # target would incorrectly read as free-to-manage for anyone.
    # Deliberately asks for a target the light is NOT already at
    # (unlike the same-target rescue test above) - if the bug were
    # present, the owner-check-skipping rescue would incorrectly clear
    # this light for writing; with the fix, it correctly stays
    # protected (excluded from combined) despite genuinely needing an
    # update, because that pending claim belongs to a different owner.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 200, "color_temp_kelvin": 3000},
                "context_id": "ctx-device-echo-unrelated",
            }
        },
        confirmed_context_ids={"light.a": "ctx-confirmed-stale"},
        confirmed_owner_ids={"light.a": "ours"},
        pending_context_ids={"light.a": "ctx-pending-stale"},
        pending_owner_ids={"light.a": "theirs"},
        pending_targets={"light.a": {"brightness": 200, "color_temp_kelvin": 3000}},
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=100,
        sensor_color_temp_kelvin=4500,
        lookup=lookup,
        owner_id="ours",
    )
    assert groups[0].combined == []


def test_context_mismatch_with_stale_target_still_updates_once_the_curve_moves():
    # Same stale-context situation as above, but the current tick wants
    # a genuinely different value than what pending's target recorded -
    # proving the self-heal only rescues the light from being marked
    # external, it doesn't also freeze it at a stale value forever.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 200, "color_temp_kelvin": 3000},
                "context_id": "ctx-device-echo-unrelated",
            }
        },
        confirmed_context_ids={"light.a": "ctx-confirmed-stale"},
        confirmed_owner_ids={"light.a": "ours"},
        pending_context_ids={"light.a": "ctx-pending-stale"},
        pending_owner_ids={"light.a": "ours"},
        pending_targets={"light.a": {"brightness": 200, "color_temp_kelvin": 3000}},
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=100,  # the curve has moved on since that echo
        sensor_color_temp_kelvin=4500,
        lookup=lookup,
        owner_id="ours",
    )
    assert groups[0].combined == ["light.a"]


def test_context_mismatch_is_still_external_when_live_values_dont_match_pendings_target():
    # Same stale-context shape, but the light's current values don't
    # match what pending asked for either - a real external change, not
    # an echo of our own write. Must stay excluded.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 40, "color_temp_kelvin": 6000},
                "context_id": "ctx-someone-else",
            }
        },
        confirmed_context_ids={"light.a": "ctx-confirmed-stale"},
        confirmed_owner_ids={"light.a": "ours"},
        pending_context_ids={"light.a": "ctx-pending-stale"},
        pending_owner_ids={"light.a": "ours"},
        pending_targets={"light.a": {"brightness": 200, "color_temp_kelvin": 3000}},
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
    assert groups[0].needing_off == []


def test_context_mismatch_with_no_recorded_target_is_still_external():
    # A pending claim exists but carries no target at all (None) - e.g.
    # data from before this field existed, or a claim written_tracking
    # only ever observed rather than issued (a restart/recovery
    # snapshot). Nothing to compare against, so this must fall through
    # to genuinely external rather than being silently forgiven.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 200, "color_temp_kelvin": 3000},
                "context_id": "ctx-someone-else",
            }
        },
        confirmed_context_ids={"light.a": "ctx-confirmed-stale"},
        confirmed_owner_ids={"light.a": "ours"},
        pending_context_ids={"light.a": "ctx-pending-stale"},
        pending_owner_ids={"light.a": "ours"},
        # No pending_targets entry for light.a.
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


def test_context_mismatch_is_forgiven_when_live_rgb_matches_pendings_target():
    # RGB equivalent of test_context_mismatch_is_forgiven_when_live_values_match_pendings_target.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {
                    "brightness": 200,
                    "rgb_color": [255, 120, 10],
                    "supported_color_modes": ["rgb"],
                },
                "context_id": "ctx-device-echo-unrelated",
            }
        },
        confirmed_context_ids={"light.a": "ctx-confirmed-stale"},
        confirmed_owner_ids={"light.a": "ours"},
        pending_context_ids={"light.a": "ctx-pending-stale"},
        pending_owner_ids={"light.a": "ours"},
        pending_targets={"light.a": {"brightness": 200, "rgb_color": [255, 120, 10]}},
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        owner_id="ours",
        prefer_rgb_color=True,
        rgb_color=(255, 120, 10),
    )
    assert groups[0].combined_rgb == []


def test_an_unconfirmed_first_attempt_that_does_not_match_is_not_yet_external():
    # No confirmed claim has ever been established for this light - only
    # a single pending attempt, which doesn't match live state either
    # (it may have landed after we last checked, or may never land at
    # all; there's no way to tell yet). Deliberately lenient rather than
    # locking this light out over one write of unknown fate - the one
    # gap the two-claim design doesn't close, documented on
    # externally_set() and write_tracking.py's module docstring.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 40, "color_temp_kelvin": 6000},
                "context_id": "ctx-whatever-this-light-had-before",
            }
        },
        pending_context_ids={"light.a": "ctx-first-attempt"},
        pending_owner_ids={"light.a": "ours"},
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        owner_id="ours",
    )
    assert groups[0].combined == ["light.a"]


def test_a_different_owners_pending_write_is_still_externally_set():
    # The owner check applies on a pending match exactly as it does on a
    # confirmed one - a different caller's in-flight write is still not
    # mine, even though nothing about the light's context is stale.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 40, "color_temp_kelvin": 6000},
                "context_id": "ctx-pending",
            }
        },
        pending_context_ids={"light.a": "ctx-pending"},
        pending_owner_ids={"light.a": "other_automation"},
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


def test_a_pending_write_with_no_owner_id_does_not_block_a_later_owner_id_check():
    # Mirrors test_a_write_recorded_with_no_owner_id_does_not_block_a_later_owner_id_check
    # above, but for a match on pending rather than confirmed.
    lookup = make_lookup(
        states={
            "light.a": {
                "state": "on",
                "attributes": {"brightness": 40, "color_temp_kelvin": 6000},
                "context_id": "ctx-pending",
            }
        },
        pending_context_ids={"light.a": "ctx-pending"},
        # No pending_owner_ids entry - that attempt was made with no owner_id at all.
    )
    groups = build_groups(
        entities=["light.a"],
        brightness_multipliers={},
        sensor_brightness=200,
        sensor_color_temp_kelvin=3000,
        lookup=lookup,
        owner_id="ours",
    )
    assert groups[0].combined == ["light.a"]


# RGB colour mode: prefer_rgb_color + rgb_color route RGB-capable
# entities into combined_rgb/two_step_rgb instead of combined/two_step,
# targeting rgb_color instead of sensor_color_temp_kelvin. Entities
# without RGB support are unaffected either way.


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
        confirmed_context_ids={"light.overridden": "ctx-ours"},
        confirmed_owner_ids={"light.overridden": "ours"},
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
