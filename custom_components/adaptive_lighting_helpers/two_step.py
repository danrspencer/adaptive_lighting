"""
Finds lights that almost certainly need the `no_combined_transition`
label but don't have it yet.

Background: some bulbs can't transition brightness and colour temperature
in one command - sent together, they either snap or drop one of the two.
grouping.py routes any light carrying the `no_combined_transition` label
into two sequential half-length calls instead (see build_groups' two_step
/two_step_rgb buckets). That label is plain Home Assistant label-registry
data, applied by hand to an entity or its device - this integration only
ever *reads* it, and has no say in what's on the list.

Which makes it quietly easy to get wrong. Adding a sixth bulb to a room
that already has five labelled ones silently gets combined transitions,
with no error and no log line anywhere - the only symptom is a fade that
looks slightly wrong, which you'd have to be watching for. Worse, the
lookup is an exact string match, so a label whose id doesn't match
(a typo, or a display name whose generated id differs from what you
assumed) fails exactly the same silent way.

This module is the detection half of the repair that closes that gap: it
matches a light's device manufacturer/model against a list of known
patterns and reports anything matching that isn't labelled. Pure logic,
no Home Assistant imports - the registry access lives in __init__.py and
is injected, same split as curve.py/grouping.py/scenes.py.

The pattern list is deliberately two-layered:
  * DEFAULT_TWO_STEP_MODEL_PATTERNS ships with the integration, so the
    common cases work with no configuration at all, and a newly
    discovered bad bulb can be contributed back as a one-line PR that
    every install picks up on its next update.
  * A per-install list, configured through the integration's own options
    (see config_flow.py), is merged on top - so an unusual bulb can be
    handled immediately without waiting for a release, and without
    editing anything that an update would overwrite.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Callable, Iterable, Optional

TWO_STEP_LABEL_ID = "no_combined_transition"
TWO_STEP_LABEL_NAME = "No Combined Transition"
TWO_STEP_LABEL_ICON = "mdi:transition"
TWO_STEP_LABEL_DESCRIPTION = (
    "Bulb can't transition brightness and colour temperature together "
    "(e.g. IKEA TRADFRI) - Adaptive Lighting Helpers sends these as two "
    "sequential calls instead of one combined call."
)

# Case-insensitive glob patterns matched against "<manufacturer> <model>".
#
# Deliberately narrow: every entry here should be a bulb someone has
# actually observed misbehaving, not a guess. A pattern that's too broad
# is worse than a missing one - it produces a repair suggesting a label
# that would make those bulbs transition *worse*, in two calls when one
# would have been fine.
#
# Adding to this list is the intended way to contribute a newly found
# bulb: one line here, and every install picks it up on its next update.
DEFAULT_TWO_STEP_MODEL_PATTERNS: tuple[str, ...] = (
    # IKEA TRADFRI - the original case this whole code path exists for.
    # Covers the GU10/E27/E14 spectrum bulbs, which all share the prefix.
    "*TRADFRI bulb*",
)


@dataclass(frozen=True)
class CandidateLight:
    """One light entity plus the device identity used to match it.

    manufacturer/model come from the *device*, not the entity - a light
    entity carries no model information of its own.
    """

    entity_id: str
    device_id: Optional[str]
    name: str
    manufacturer: str
    model: str


def parse_extra_patterns(raw: str | Iterable[str] | None) -> list[str]:
    """Turns the options-flow text field into a pattern list.

    Accepts a newline- or comma-separated string (what the multiline text
    selector hands back) or an already-split iterable, and drops blanks
    and surrounding whitespace. Anything falsy yields an empty list
    rather than raising - a misconfigured options field should quietly
    fall back to the shipped defaults, never break setup.
    """
    if not raw:
        return []
    if isinstance(raw, str):
        parts: Iterable[str] = raw.replace(",", "\n").splitlines()
    else:
        parts = raw
    return [p.strip() for p in parts if isinstance(p, str) and p.strip()]


def model_matches(manufacturer: str | None, model: str | None, patterns: Iterable[str]) -> bool:
    """True if this device looks like a known two-step bulb.

    Matched against "<manufacturer> <model>" so a pattern can pin either
    or both ("IKEA*", "*TRADFRI bulb*"). Case-insensitive, because
    manufacturer strings are wildly inconsistent between integrations
    for the same physical device.
    """
    haystack = f"{manufacturer or ''} {model or ''}".strip().lower()
    if not haystack:
        return False
    return any(fnmatch(haystack, pattern.strip().lower()) for pattern in patterns if pattern.strip())


def find_unlabelled_two_step_lights(
    candidates: Iterable[CandidateLight],
    patterns: Iterable[str],
    has_label: Callable[[CandidateLight], bool],
) -> list[CandidateLight]:
    """Every candidate whose model matches but which isn't labelled yet.

    has_label is injected rather than computed here because "labelled"
    means "the label is on the entity *or* on its device" - which is
    registry access, and matches how grouping.py's EntityLookup.tags()
    resolves it. Keeping the same either/or rule in both places is the
    whole point: anything this reports as missing is exactly what
    grouping.py would fail to route into the two-step path.

    Sorted by entity_id so the resulting repair text is stable between
    runs - an issue whose description reshuffles on every check looks
    like new information when it isn't.
    """
    pattern_list = [p for p in patterns if p and p.strip()]
    if not pattern_list:
        return []
    matched = [c for c in candidates if model_matches(c.manufacturer, c.model, pattern_list)]
    return sorted((c for c in matched if not has_label(c)), key=lambda c: c.entity_id)


def describe(lights: Iterable[CandidateLight], limit: int = 8) -> str:
    """Short human-readable list for the repair's own description.

    Truncated because the repair dialog is not a good place to render
    thirty entity ids - the fix applies to all of them regardless of how
    many are named here.
    """
    names = [f"{light.name} ({light.entity_id})" for light in lights]
    if not names:
        return ""
    if len(names) <= limit:
        return ", ".join(names)
    remaining = len(names) - limit
    return ", ".join(names[:limit]) + f", and {remaining} more"
