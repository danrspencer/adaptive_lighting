"""
Pure unit tests for override_protection.classify()/target_matches_values() -
the standalone decision logic behind the check_ownership/record_ownership
services and grouping.py's own EntityLookup.externally_set() (now a thin
adapter over classify(), see grouping.py's own docstring).

No EntityLookup, no fakes beyond plain values - classify() takes exactly
the confirmed/pending claim dicts and live values it needs, nothing else.
"""

from override_protection import classify, is_blocked, target_matches_values


def test_off_light_is_never_protected_regardless_of_claims():
    # A light that isn't on is free to manage no matter what confirmed/
    # pending say - this is the exact precondition sensor.py's status
    # classification used to fail to mirror (a real, live-found bug: a
    # light turned off by a code path outside apply_lighting fell
    # through to "overridden" despite this precondition).
    status, owner = classify(
        is_on=False,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending={"context_id": "ctx-pending", "owner_id": "automation.a"},
        current_context="ctx-something-else-entirely",
    )
    assert (status, owner) == ("off", None)


def test_no_claim_at_all_is_unclaimed():
    status, owner = classify(is_on=True, confirmed=None, pending=None, current_context="ctx-anything")
    assert (status, owner) == ("unclaimed", None)


def test_a_single_unconfirmed_pending_that_does_not_match_is_unclaimed():
    # The one gap the two-claim design doesn't close: a light's very
    # first tracked write, if dropped, is indistinguishable from a
    # genuinely external change until a confirmed baseline exists.
    status, owner = classify(
        is_on=True,
        confirmed=None,
        pending={"context_id": "ctx-first-attempt", "owner_id": "automation.a"},
        current_context="ctx-whatever-this-light-had-before",
    )
    assert (status, owner) == ("unclaimed", None)


def test_context_matches_pending():
    status, owner = classify(
        is_on=True,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending={"context_id": "ctx-pending", "owner_id": "automation.b"},
        current_context="ctx-pending",
    )
    assert (status, owner) == ("pending", "automation.b")


def test_context_matches_confirmed():
    status, owner = classify(
        is_on=True,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending={"context_id": "ctx-pending", "owner_id": "automation.a"},
        current_context="ctx-confirmed",
    )
    assert (status, owner) == ("controlled", "automation.a")


def test_context_matches_neither_but_value_matches_pendings_target():
    # The delayed-echo rescue - see classify()'s own docstring for why
    # a context mismatch alone doesn't prove an external touch.
    status, owner = classify(
        is_on=True,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending={"context_id": "ctx-pending", "owner_id": "automation.b", "target": {"brightness": 200, "color_temp_kelvin": 3000}},
        current_context="ctx-device-echo-unrelated",
        current_brightness=200,
        current_color_temp_kelvin=3000,
    )
    # Rescued back to "controlled", attributed to pending's own owner -
    # the bundled correctness fix: this used to skip the owner check
    # entirely for the rescue case.
    assert (status, owner) == ("controlled", "automation.b")


def test_rescue_does_not_apply_when_values_dont_match():
    status, owner = classify(
        is_on=True,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending={"context_id": "ctx-pending", "owner_id": "automation.a", "target": {"brightness": 200, "color_temp_kelvin": 3000}},
        current_context="ctx-someone-else",
        current_brightness=40,
        current_color_temp_kelvin=6000,
    )
    assert (status, owner) == ("overridden", None)


def test_rescue_does_not_apply_when_pending_has_no_recorded_target():
    status, owner = classify(
        is_on=True,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending={"context_id": "ctx-pending", "owner_id": "automation.a", "target": None},
        current_context="ctx-someone-else",
        current_brightness=200,
        current_color_temp_kelvin=3000,
    )
    assert (status, owner) == ("overridden", None)


def test_genuinely_overridden_when_confirmed_exists_and_nothing_matches():
    status, owner = classify(
        is_on=True,
        confirmed={"context_id": "ctx-confirmed", "owner_id": "automation.a"},
        pending=None,
        current_context="ctx-someone-else",
    )
    assert (status, owner) == ("overridden", None)


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
