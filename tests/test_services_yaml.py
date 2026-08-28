"""
services.yaml is user-facing: Home Assistant renders these field
descriptions inline in the service-call UI, so both compute_lighting_groups
and apply_lighting genuinely need their own full copy - deduplicating them
with a YAML anchor would save ~150 lines but leaves a phantom top-level key
(HA reads every top-level key as a service name) and makes a reference file
you read top-to-bottom into one you have to jump around.

So the duplication stays, and this guards it instead: the fields that are
meant to be identical must actually stay identical, and the two that are
meant to differ are named explicitly rather than silently tolerated.
CLAUDE.md already records these copies drifting apart once.
"""

from pathlib import Path

import yaml

SERVICES = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "custom_components/flare/services.yaml").read_text()
)

# compute_lighting_groups only *reports* what it would do; apply_lighting
# actually dispatches. These two describe that difference, so their wording
# is deliberately not shared. scope_device_id joins them for the same
# reason: compute_lighting_groups never writes, so it only ever *reads*
# the named scope's claims to decide what's already externally-set,
# where apply_lighting's copy describes recording a write into it.
INTENTIONALLY_DIFFERENT = {"prefer_rgb_color", "two_step_label", "scope_device_id"}


def _shared_fields():
    a = SERVICES["compute_lighting_groups"]["fields"]
    b = SERVICES["apply_lighting"]["fields"]
    return a, b, sorted(set(a) & set(b))


def test_the_planner_and_the_dispatcher_share_the_same_field_set():
    """apply_lighting is compute_lighting_groups plus `transition` - if a
    field is added to one and forgotten on the other, they stop being the
    read-only/side-effecting pair they're documented as."""
    a, b, _ = _shared_fields()
    assert set(b) - set(a) == {"transition"}
    assert set(a) - set(b) == set()


def test_fields_meant_to_be_identical_have_not_drifted():
    a, b, shared = _shared_fields()
    drifted = [k for k in shared if k not in INTENTIONALLY_DIFFERENT and a[k] != b[k]]
    assert drifted == [], (
        f"These services.yaml fields are duplicated between compute_lighting_groups and "
        f"apply_lighting and have drifted apart: {drifted}. Update both copies, or add the "
        f"field to INTENTIONALLY_DIFFERENT if the wording genuinely should differ."
    )


def test_the_intentionally_different_fields_really_do_still_differ():
    """The other direction: if these two are ever reconciled, the allowlist
    is stale and should shrink rather than quietly excusing a real match."""
    a, b, shared = _shared_fields()
    for key in INTENTIONALLY_DIFFERENT:
        assert key in shared
        assert a[key] != b[key], f"{key} no longer differs - remove it from INTENTIONALLY_DIFFERENT"


def test_every_documented_service_is_one_this_integration_registers():
    """A stray top-level key in services.yaml is silently ignored at
    runtime (HA looks descriptions up per registered service), so nothing
    would surface a typo or a leftover block."""
    expected = {
        "compute_lighting_groups",
        "apply_lighting",
        "compute_curve",
        "compute_scene_coverage",
        "check_control",
        "record_write",
        "clear_claims",
    }
    assert set(SERVICES) == expected


def test_compute_curve_documents_the_defaults_it_actually_uses():
    """services.yaml is what Developer Tools -> Actions shows, so a stale
    `default:` there is a number a user reads, types nowhere, and gets
    something else from. It's the third copy of these values after
    curve.py and curve.js - the JS one is pinned in
    test_curve_js_parity.py."""
    from curve import DEFAULT_CURVE_VALUES

    fields = SERVICES["compute_curve"]["fields"]
    documented = {k: v.get("default") for k, v in fields.items() if k in DEFAULT_CURVE_VALUES}

    assert documented == DEFAULT_CURVE_VALUES, (
        "services.yaml's documented defaults have drifted from curve.py's:\n"
        + "\n".join(
            f"  {k}: curve.py={DEFAULT_CURVE_VALUES[k]} services.yaml={documented.get(k, '<missing>')}"
            for k in sorted(DEFAULT_CURVE_VALUES)
            if DEFAULT_CURVE_VALUES[k] != documented.get(k)
        )
    )
