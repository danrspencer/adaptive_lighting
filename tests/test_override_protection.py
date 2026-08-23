"""
Pure unit tests for override_protection.classify()/target_matches_values() -
the standalone decision logic behind the check_ownership/record_ownership
services and grouping.py's own EntityLookup.externally_set() (now a thin
adapter over classify(), see grouping.py's own docstring).

No EntityLookup, no fakes beyond plain values - classify() takes exactly
the confirmed/pending claim dicts and live values it needs, nothing else.

Requires a real Home Assistant install (override_protection.py imports
homeassistant.util.color directly - see its own module docstring for why
that's a deliberate exception to "no HA dependency", not an oversight).
"""

from datetime import datetime, timedelta, timezone

from override_protection import PENDING_GRACE_SECONDS, _color_temp_matches, classify, is_blocked, target_matches_values

NOW = datetime(2026, 8, 23, 6, 0, 0, tzinfo=timezone.utc)


def test_off_light_is_never_protected_regardless_of_claims():
    # A light that isn't on is free to manage no matter what confirmed/
    # pending say - this is the exact precondition sensor.py's status
    # classification used to fail to mirror (a real, live-found bug: a
    # light turned off by a code path outside apply_lighting fell
    # through to "overridden" despite this precondition).
    status, owner, matched_via = classify(
        is_on=False,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending={"context_id": "ctx-pending", "owner_id": "automation.a"},
        current_context="ctx-something-else-entirely",
    )
    assert (status, owner, matched_via) == ("off", None, None)


def test_no_claim_at_all_is_unclaimed():
    status, owner, matched_via = classify(is_on=True, confirmed=None, pending=None, current_context="ctx-anything")
    assert (status, owner, matched_via) == ("unclaimed", None, None)


def test_a_single_unconfirmed_pending_that_does_not_match_is_unclaimed():
    # The one gap the two-claim design doesn't close: a light's very
    # first tracked write, if dropped, is indistinguishable from a
    # genuinely external change until a confirmed baseline exists.
    status, owner, matched_via = classify(
        is_on=True,
        confirmed=None,
        pending={"context_id": "ctx-first-attempt", "owner_id": "automation.a"},
        current_context="ctx-whatever-this-light-had-before",
    )
    assert (status, owner, matched_via) == ("unclaimed", None, None)


def test_context_matches_pending():
    status, owner, matched_via = classify(
        is_on=True,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending={"context_id": "ctx-pending", "owner_id": "automation.b"},
        current_context="ctx-pending",
    )
    assert (status, owner, matched_via) == ("pending", "automation.b", "context")


def test_context_matches_confirmed():
    status, owner, matched_via = classify(
        is_on=True,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending={"context_id": "ctx-pending", "owner_id": "automation.a"},
        current_context="ctx-confirmed",
    )
    assert (status, owner, matched_via) == ("controlled", "automation.a", "context")


def test_context_matches_neither_but_value_matches_pendings_target():
    # The delayed-echo rescue - see classify()'s own docstring for why
    # a context mismatch alone doesn't prove an external touch.
    status, owner, matched_via = classify(
        is_on=True,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending={"context_id": "ctx-pending", "owner_id": "automation.b", "target": {"brightness": 200, "color_temp_kelvin": 3000}},
        current_context="ctx-device-echo-unrelated",
        current_brightness=200,
        current_color_temp_kelvin=3000,
    )
    # Rescued back to "controlled", attributed to pending's own owner -
    # the bundled correctness fix: this used to skip the owner check
    # entirely for the rescue case. matched_via distinguishes this from
    # the context-matched "controlled" case above.
    assert (status, owner, matched_via) == ("controlled", "automation.b", "value")


def test_rescue_does_not_apply_when_values_dont_match():
    status, owner, matched_via = classify(
        is_on=True,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending={"context_id": "ctx-pending", "owner_id": "automation.a", "target": {"brightness": 200, "color_temp_kelvin": 3000}},
        current_context="ctx-someone-else",
        current_brightness=40,
        current_color_temp_kelvin=6000,
    )
    assert (status, owner, matched_via) == ("overridden", None, None)


def test_rescue_does_not_apply_when_pending_has_no_recorded_target():
    status, owner, matched_via = classify(
        is_on=True,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending={"context_id": "ctx-pending", "owner_id": "automation.a", "target": None},
        current_context="ctx-someone-else",
        current_brightness=200,
        current_color_temp_kelvin=3000,
    )
    assert (status, owner, matched_via) == ("overridden", None, None)


def test_pending_within_grace_period_is_pending_not_overridden():
    # Live incident, 2026-08-23: a Night->Day phase transition writes a
    # whole room's lights at once. On the very next tick, a device whose
    # round-trip hasn't landed yet reports neither a matching context nor
    # a matching value - previously misjudged "overridden" a full tick
    # too early, which then locked the light out forever (see
    # grouping.py's externally_set() docstring, point 5). now=NOW here
    # is only 30s after pending was recorded - well within
    # PENDING_GRACE_SECONDS.
    status, owner, matched_via = classify(
        is_on=True,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending={
            "context_id": "ctx-pending",
            "owner_id": "automation.a",
            "target": {"brightness": 200, "color_temp_kelvin": 3000},
            "recorded_at": (NOW - timedelta(seconds=30)).isoformat(),
        },
        current_context="ctx-still-the-old-one",
        current_brightness=50,
        current_color_temp_kelvin=2700,
        now=NOW,
    )
    assert (status, owner, matched_via) == ("pending", "automation.a", "grace")


def test_pending_past_grace_period_is_overridden():
    # Same shape as above, but the pending write is now old enough that a
    # round-trip delay no longer explains the mismatch - back to the
    # ordinary "overridden" conclusion.
    status, owner, matched_via = classify(
        is_on=True,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending={
            "context_id": "ctx-pending",
            "owner_id": "automation.a",
            "target": {"brightness": 200, "color_temp_kelvin": 3000},
            "recorded_at": (NOW - timedelta(seconds=PENDING_GRACE_SECONDS + 1)).isoformat(),
        },
        current_context="ctx-still-the-old-one",
        current_brightness=50,
        current_color_temp_kelvin=2700,
        now=NOW,
    )
    assert (status, owner, matched_via) == ("overridden", None, None)


def test_grace_period_boundary_is_inclusive():
    # Exactly PENDING_GRACE_SECONDS old still counts as within grace
    # (<=, not <) - a deliberate boundary choice, worth locking in
    # explicitly rather than leaving to whichever way the comparison
    # operator happens to point.
    status, _owner, matched_via = classify(
        is_on=True,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending={
            "context_id": "ctx-pending",
            "owner_id": "automation.a",
            "target": {"brightness": 200, "color_temp_kelvin": 3000},
            "recorded_at": (NOW - timedelta(seconds=PENDING_GRACE_SECONDS)).isoformat(),
        },
        current_context="ctx-still-the-old-one",
        current_brightness=50,
        current_color_temp_kelvin=2700,
        now=NOW,
    )
    assert (status, matched_via) == ("pending", "grace")


def test_omitting_now_disables_the_grace_period():
    # A caller that doesn't pass `now` at all (the default) gets the
    # original, stricter behaviour - not a silent, unbounded leniency.
    # This is what keeps every pre-existing caller/test correct without
    # having to opt in explicitly.
    status, owner, matched_via = classify(
        is_on=True,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending={
            "context_id": "ctx-pending",
            "owner_id": "automation.a",
            "target": {"brightness": 200, "color_temp_kelvin": 3000},
            "recorded_at": NOW.isoformat(),
        },
        current_context="ctx-still-the-old-one",
        current_brightness=50,
        current_color_temp_kelvin=2700,
    )
    assert (status, owner, matched_via) == ("overridden", None, None)


def test_pending_with_no_recorded_at_gets_no_grace_period():
    # A claim missing recorded_at (shouldn't occur for a real write - see
    # write_tracking.py's async_record, which always stamps one - but
    # defensive nonetheless) can't justify any leniency, regardless of
    # how recent `now` is.
    status, owner, matched_via = classify(
        is_on=True,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending={
            "context_id": "ctx-pending",
            "owner_id": "automation.a",
            "target": {"brightness": 200, "color_temp_kelvin": 3000},
            "recorded_at": None,
        },
        current_context="ctx-still-the-old-one",
        current_brightness=50,
        current_color_temp_kelvin=2700,
        now=NOW,
    )
    assert (status, owner, matched_via) == ("overridden", None, None)


def test_grace_period_status_still_blocks_a_different_owner():
    # classify() alone doesn't decide blocking - is_blocked() does, on
    # top of the (status, claim_owner) pair. A grace-period "pending"
    # still correctly blocks a caller that isn't pending's own owner,
    # exactly like an ordinary context-matched "pending" already does -
    # the grace period rescues a genuine in-flight write from
    # misclassification, it doesn't loosen the owner check on top.
    status, owner, _matched_via = classify(
        is_on=True,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending={
            "context_id": "ctx-pending",
            "owner_id": "automation.a",
            "target": {"brightness": 200, "color_temp_kelvin": 3000},
            "recorded_at": (NOW - timedelta(seconds=30)).isoformat(),
        },
        current_context="ctx-still-the-old-one",
        current_brightness=50,
        current_color_temp_kelvin=2700,
        now=NOW,
    )
    assert status == "pending"
    assert is_blocked(status, owner, owner_id="automation.b") is True
    assert is_blocked(status, owner, owner_id="automation.a") is False


def test_genuinely_overridden_when_confirmed_exists_and_nothing_matches():
    status, owner, matched_via = classify(
        is_on=True,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending=None,
        current_context="ctx-someone-else",
    )
    assert (status, owner, matched_via) == ("overridden", None, None)


def test_target_matches_values_rgb_variant():
    assert target_matches_values(
        {"brightness": 200, "rgb_color": [255, 120, 10]},
        current_brightness=200,
        current_color_temp_kelvin=None,
        current_rgb_color=[255, 121, 9],
    )
    assert not target_matches_values(
        {"brightness": 200, "rgb_color": [255, 120, 10]},
        current_brightness=200,
        current_color_temp_kelvin=None,
        current_rgb_color=[10, 10, 10],
    )


def test_target_matches_values_no_target_never_matches():
    assert not target_matches_values(None, 200, 3000, None)
    assert not target_matches_values({}, 200, 3000, None)


def test_target_matches_values_recognises_a_mired_equivalent_color_temp():
    # The live incident this fix closes: 4373K asked for, 4385K reported
    # back - both floor to mired 228 via HA's own
    # color_temperature_kelvin_to_mired, so they're the same colour as
    # far as the device is concerned, even though the Kelvin gap (12)
    # exceeds the default color_temp_tolerance (10).
    assert target_matches_values(
        {"brightness": 255, "color_temp_kelvin": 4373},
        current_brightness=255,
        current_color_temp_kelvin=4385,
        current_rgb_color=None,
    )


def test_is_blocked_force_bypasses_regardless_of_status():
    assert is_blocked("overridden", None, "ours", force=True) is False


def test_is_blocked_no_owner_id_bypasses():
    assert is_blocked("overridden", None, owner_id=None) is False


def test_is_blocked_off_and_unclaimed_never_block():
    assert is_blocked("off", None, "ours") is False
    assert is_blocked("unclaimed", None, "ours") is False


def test_is_blocked_overridden_always_blocks():
    assert is_blocked("overridden", None, "ours") is True


def test_is_blocked_matched_claim_blocks_only_for_a_different_owner():
    assert is_blocked("controlled", "ours", "ours") is False
    assert is_blocked("controlled", "theirs", "ours") is True
    assert is_blocked("pending", None, "ours") is False  # unowned claim blocks nobody


def test_color_temp_matches_within_flat_kelvin_tolerance():
    # The pre-existing behaviour, unchanged: close enough in plain
    # Kelvin terms matches regardless of mireds.
    assert _color_temp_matches(3005, 3000, tolerance_kelvin=10)
    assert not _color_temp_matches(3050, 3000, tolerance_kelvin=10)


def test_color_temp_matches_the_real_live_incident_exactly():
    # 4373K -> floor(1000000/4373) = mired 228 -> floor(1000000/228) = 4385K.
    # Confirmed live: HA's own conversion, not a guess.
    assert _color_temp_matches(4385, 4373, tolerance_kelvin=10)


def test_color_temp_matches_rejects_a_genuinely_different_mired_value():
    # A gap large enough that it's not explained by mired rounding at
    # all - e.g. brightness 40/color 6000 vs target 3000 from the
    # override tests above. Neither within tolerance nor mired-equal.
    assert not _color_temp_matches(6000, 3000, tolerance_kelvin=10)
