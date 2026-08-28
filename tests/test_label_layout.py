"""
Guards the dashboard card's phase-label collision avoidance.

Each phase label wants to sit centred on its own boundary line. On a wide
card that's fine; on a phone two phases an hour apart are only a few
pixels apart while the words need ~60px between them. Measured on a
375px-wide screen, "Morning"/"Day" and "Evening"/"Night" each overlapped
by 7px.

layoutBoundaryLabels() in the card nudges them apart. It's pure - centres
and widths in, centres out - so it's exercised here directly under node,
with no DOM. The card's own _layoutLabels() supplies the real measured
widths, which is the part that genuinely needs a browser.

The properties below are what "laid out correctly" means, and each is a
way the naive version was wrong: labels must not overlap, must stay
inside the chart, must keep their left-to-right order (a label that
overtook its neighbour would point at the wrong line), and must not move
when they already fit.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CARD_JS = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "flare"
    / "www"
    / "flare-curve-card.js"
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

GAP = 6

DRIVER = f"""
globalThis.HTMLElement = class {{}};
globalThis.customElements = {{ define() {{}}, get() {{ return undefined; }} }};
globalThis.window = globalThis;
const {{ layoutBoundaryLabels }} = await import({json.dumps(CARD_JS.as_posix())});

const input = JSON.parse(await new Promise((resolve) => {{
  let buf = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (c) => (buf += c));
  process.stdin.on('end', () => resolve(buf));
}}));

process.stdout.write(JSON.stringify(
  input.cases.map((c) => layoutBoundaryLabels(c.desired, c.widths, c.containerWidth))
));
"""


def layout(cases):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", DRIVER],
        input=json.dumps({"cases": cases}),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"node failed:\n{result.stderr}")
    return json.loads(result.stdout)


def spans(placed, widths):
    """Left/right edges of everything still visible."""
    return [
        (p["centre"] - w / 2, p["centre"] + w / 2)
        for p, w in zip(placed, widths)
        if not p["hidden"]
    ]


# The real measurement from a 375px phone, plus the wide case that must
# be left alone, plus deliberately hostile ones.
CASES = {
    "phone 375px (the reported collision)": {
        "desired": [116.5, 141.0, 288.0, 316.0],
        "widths": [43, 20, 41, 28],
        "containerWidth": 315,
    },
    "desktop, already fine": {
        "desired": [120.0, 200.0, 500.0, 600.0],
        "widths": [43, 20, 41, 28],
        "containerWidth": 700,
    },
    "all four boundaries within an hour": {
        "desired": [150.0, 154.0, 158.0, 162.0],
        "widths": [43, 20, 41, 28],
        "containerWidth": 315,
    },
    "everything crushed against the left edge": {
        "desired": [0.0, 1.0, 2.0, 3.0],
        "widths": [43, 20, 41, 28],
        "containerWidth": 315,
    },
    "everything crushed against the right edge": {
        "desired": [315.0, 315.0, 315.0, 315.0],
        "widths": [43, 20, 41, 28],
        "containerWidth": 315,
    },
    "container far too narrow for all four": {
        "desired": [10.0, 30.0, 50.0, 70.0],
        "widths": [43, 20, 41, 28],
        "containerWidth": 90,
    },
    "single label": {"desired": [10.0], "widths": [43], "containerWidth": 315},
    "no labels at all": {"desired": [], "widths": [], "containerWidth": 315},
}

CASE_IDS = list(CASES)
RESULTS = layout([CASES[name] for name in CASE_IDS])


@pytest.mark.parametrize("name", CASE_IDS)
def test_visible_labels_never_overlap(name):
    case = CASES[name]
    placed = RESULTS[CASE_IDS.index(name)]
    edges = spans(placed, case["widths"])
    for (left_a, right_a), (left_b, _) in zip(edges, edges[1:]):
        assert left_b >= right_a + GAP - 0.01, (
            f"{name}: labels overlap or crowd - one ends at {right_a:.1f}, the next starts at {left_b:.1f}"
        )


@pytest.mark.parametrize("name", CASE_IDS)
def test_visible_labels_stay_inside_the_chart(name):
    case = CASES[name]
    placed = RESULTS[CASE_IDS.index(name)]
    for left, right in spans(placed, case["widths"]):
        assert left >= -0.01, f"{name}: a label runs off the left edge ({left:.1f})"
        assert right <= case["containerWidth"] + 0.01, (
            f"{name}: a label runs off the right edge ({right:.1f} > {case['containerWidth']})"
        )


@pytest.mark.parametrize("name", CASE_IDS)
def test_labels_keep_their_order(name):
    """A label that overtook its neighbour would sit nearer the wrong
    boundary line - worse than overlapping, because it reads as correct."""
    placed = RESULTS[CASE_IDS.index(name)]
    centres = [p["centre"] for p in placed if not p["hidden"]]
    assert centres == sorted(centres), f"{name}: labels changed order"


def test_labels_that_already_fit_are_not_moved():
    """The wide case must be untouched - nudging labels that don't need it
    would pull them off their own boundary lines for no reason."""
    case = CASES["desktop, already fine"]
    placed = RESULTS[CASE_IDS.index("desktop, already fine")]
    assert [p["centre"] for p in placed] == case["desired"]
    assert not any(p["hidden"] for p in placed)


def test_the_reported_phone_collision_is_resolved():
    """The exact numbers measured in the browser at 375px wide."""
    case = CASES["phone 375px (the reported collision)"]
    placed = RESULTS[CASE_IDS.index("phone 375px (the reported collision)")]
    assert not any(p["hidden"] for p in placed), "all four fit at phone width; none should be dropped"
    edges = spans(placed, case["widths"])
    gaps = [round(b[0] - a[1], 1) for a, b in zip(edges, edges[1:])]
    assert all(g >= GAP - 0.01 for g in gaps), f"gaps were {gaps}"


def test_labels_are_dropped_only_when_they_cannot_fit():
    """Dropping is the last resort - but two words printed on top of each
    other are worse than one honest label, so it does happen."""
    narrow = RESULTS[CASE_IDS.index("container far too narrow for all four")]
    assert any(p["hidden"] for p in narrow), "nothing was dropped in a container too small for all four"
    # Later labels go first, so the earliest phases of the day survive.
    hidden = [i for i, p in enumerate(narrow) if p["hidden"]]
    visible = [i for i, p in enumerate(narrow) if not p["hidden"]]
    assert not visible or max(visible) < min(hidden), "labels were dropped out of order"

    for name in CASES:
        if name == "container far too narrow for all four":
            continue
        placed = RESULTS[CASE_IDS.index(name)]
        assert not any(p["hidden"] for p in placed), f"{name}: dropped a label that had room to fit"
