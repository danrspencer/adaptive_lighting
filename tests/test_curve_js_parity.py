"""
Guards the docs site's JS port of curve.py against drift.

docs/assets/js/curve.js is a second implementation of the schedule,
written so the docs site's interactive playground can recompute the
curve live in the browser as a slider moves (the real Lovelace card
never does that - it reads a precomputed `points` attribute produced by
curve.py itself). Two implementations of the same math is exactly the
setup where one quietly stops matching the other, so this runs the JS
under node against a grid of inputs and asserts every single value is
identical to what curve.py returns.

The grid deliberately includes exact .5 ties in the brightness and Kelvin
ramps, because the two languages disagree there by default: Python's
round() is half-to-EVEN, JS's Math.round() is half-UP. curve.js has a
pyRound() for those ramps for that reason, and swapping it for Math.round
is a silent off-by-one on ties only - invisible by eye, and verified
here (mutating it fails this test).

kelvin_to_rgb is the documented exception, and this test does NOT pin its
tie-breaking rule - because nothing can. Its three channels come out of
log/pow, and an exact .5 was searched for and does not occur anywhere in
1000-20000K at integer steps, nor on a 0.01K grid across 1000-10000K. So
half-up and half-to-even are indistinguishable for it in practice, and
curve.py's floor(x + 0.5) is belt-and-braces rather than load-bearing.
What the 1K-step RGB sweep below *does* pin is the algorithm itself: a
changed coefficient or a changed branch that moves any channel by a whole
integer fails it. Changes smaller than that (a 12th-significant-figure
coefficient tweak) or ones the 0-255 clamp absorbs (the temp<=19 blue
cutoff and the temp<=66 red one are both continuous - the alternative
branch clamps to the same value) pass, correctly: they are not observable
differences in what a light is actually told to do.

Skips (rather than fails) when node isn't installed, so the pure-Python
test layer still runs on a machine without it. CI's ubuntu-latest runner
has node preinstalled, so it does run there.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from curve import (
    DEFAULT_CURVE_VALUES,
    brightness_for_phase,
    kelvin_for_phase,
    kelvin_to_rgb,
    phase_at,
    phase_marks,
)

CURVE_JS = Path(__file__).resolve().parent.parent / "docs" / "assets" / "js" / "curve.js"
CARD_JS = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "flare"
    / "www"
    / "flare-curve-card.js"
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

# A day's worth of boundaries, plus deliberately awkward ones: a very
# short Day (evening right after day start, so the ramp's divisor gets
# small), and an Evening whose hold window is squeezed out entirely
# (night less than 2h after evening, so hold_start clamps to fade_start).
BOUNDARY_SETS = [
    {"morningTs": 6 * 3600, "dayStartTs": 8 * 3600, "eveningTs": 19.75 * 3600, "nightTs": 22 * 3600},
    {"morningTs": 5 * 3600, "dayStartTs": 7 * 3600, "eveningTs": 17 * 3600, "nightTs": 23 * 3600},
    {"morningTs": 6 * 3600, "dayStartTs": 8 * 3600, "eveningTs": 8.25 * 3600, "nightTs": 22 * 3600},
    {"morningTs": 6 * 3600, "dayStartTs": 8 * 3600, "eveningTs": 20 * 3600, "nightTs": 21.5 * 3600},
]

# Defaults, plus ranges and durations chosen to land on exact .5 ties in
# the ramps - odd endpoints over an even number of steps put the
# interpolation on halves, which is the only place Python's half-to-even
# and JS's half-up disagree.
CURVE_VALUE_SETS = [
    dict(DEFAULT_CURVE_VALUES),
    {**DEFAULT_CURVE_VALUES, "evening_brightness": 161, "night_brightness": 80},
    {**DEFAULT_CURVE_VALUES, "morning_kelvin": 6501, "day_kelvin": 4001, "evening_kelvin": 3001, "night_kelvin": 2701},
    {**DEFAULT_CURVE_VALUES, "evening_brightness": 3, "night_brightness": 254, "morning_kelvin": 2000, "day_kelvin": 6500},
    # Durations away from the defaults, including zero (a hard cut) and a
    # value far longer than its phase (clamped to the whole phase).
    {**DEFAULT_CURVE_VALUES, "evening_kelvin_transition": 0, "evening_brightness_transition": 0},
    {**DEFAULT_CURVE_VALUES, "night_kelvin_transition": 1440, "morning_brightness_transition": 1440},
    {**DEFAULT_CURVE_VALUES, "day_kelvin_transition": 37, "evening_brightness_transition": 91},
]

PHASES = ["Morning", "Day", "Evening", "Night"]


def _instants(boundaries):
    """Every 5 minutes across the day, plus each boundary exactly and one
    second either side of it - half-open interval edges are where an
    off-by-one in phase_at would hide."""
    instants = [i * 300 for i in range(289)]
    for key in ("morningTs", "dayStartTs", "eveningTs", "nightTs"):
        b = boundaries[key]
        instants.extend([b - 1, b, b + 1])
    # The Evening fade and hold boundaries specifically.
    instants.extend([boundaries["nightTs"] - 3600, boundaries["eveningTs"] + 3600])
    return sorted(set(float(t) for t in instants))


def _build_cases():
    cases = []
    for boundaries in BOUNDARY_SETS:
        for values in CURVE_VALUE_SETS:
            for t in _instants(boundaries):
                for phase in PHASES:
                    cases.append({"boundaries": boundaries, "values": values, "t": t, "phase": phase})
    return cases


def _node_eval(driver_src, payload):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", driver_src],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"node failed:\n{result.stderr}")
    return json.loads(result.stdout)


DRIVER = f"""
import {{ phaseAt, brightnessForPhase, kelvinForPhase, kelvinToRgb }} from {json.dumps(CURVE_JS.as_posix())};

const input = JSON.parse(await new Promise((resolve) => {{
  let buf = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (c) => (buf += c));
  process.stdin.on('end', () => resolve(buf));
}}));

const out = input.cases.map((c) => {{
  const b = c.boundaries;
  return [
    phaseAt(c.t, b.morningTs, b.dayStartTs, b.eveningTs, b.nightTs),
    brightnessForPhase(c.phase, c.t, b, c.values),
    kelvinForPhase(c.phase, c.t, b, c.values),
  ];
}});

process.stdout.write(JSON.stringify({{ out, rgb: input.kelvins.map(kelvinToRgb) }}));
"""


def test_curve_js_matches_curve_py():
    cases = _build_cases()
    # Every Kelvin the schedule can plausibly produce, at 1K steps - a
    # dense enough sweep to hit the .5 ties in the RGB approximation.
    kelvins = list(range(1000, 10001))

    js = _node_eval(DRIVER, {"cases": cases, "kelvins": kelvins})

    assert len(js["out"]) == len(cases), "node returned a different number of results"

    mismatches = []
    for case, (js_phase, js_brightness, js_kelvin) in zip(cases, js["out"]):
        b, v, t, phase = case["boundaries"], case["values"], case["t"], case["phase"]

        py_phase = phase_at(t, b["morningTs"], b["dayStartTs"], b["eveningTs"], b["nightTs"])
        args = (b["morningTs"], b["dayStartTs"], b["eveningTs"], b["nightTs"])
        py_brightness = brightness_for_phase(phase, t, *args, **_brightness_kwargs(v))
        py_kelvin = kelvin_for_phase(phase, t, *args, **_kelvin_kwargs(v))

        if py_phase != js_phase:
            mismatches.append(f"phase_at(t={t}, {b}) py={py_phase} js={js_phase}")
        if py_brightness != js_brightness:
            mismatches.append(f"brightness({phase}, t={t}, night={b['nightTs']}, {v}) py={py_brightness} js={js_brightness}")
        if py_kelvin != js_kelvin:
            mismatches.append(f"kelvin({phase}, t={t}, {b}, {v}) py={py_kelvin} js={js_kelvin}")

    assert not mismatches, "curve.js has drifted from curve.py:\n" + "\n".join(mismatches[:20])

    rgb_mismatches = [
        f"kelvin_to_rgb({k}) py={tuple(kelvin_to_rgb(k))} js={tuple(js_rgb)}"
        for k, js_rgb in zip(kelvins, js["rgb"])
        if tuple(kelvin_to_rgb(k)) != tuple(js_rgb)
    ]
    assert not rgb_mismatches, "curve.js's kelvinToRgb has drifted:\n" + "\n".join(rgb_mismatches[:20])


def _brightness_kwargs(values):
    return {k: v for k, v in values.items() if "brightness" in k}


def _kelvin_kwargs(values):
    return {k: v for k, v in values.items() if "kelvin" in k}


# --- the dashboard card's own copies --------------------------------
#
# The card carries its own phaseAt/phaseMarks/kelvinToRgb rather than
# importing them: it has to run standalone inside Home Assistant's
# frontend, with no build step and nothing to import from. That makes it
# a THIRD implementation of logic curve.py owns, and until these tests it
# was the only one nothing checked - the docs port at least had the grid
# above.
#
# It imports cleanly under node given three shims. Home Assistant loads
# it as an ES module (add_extra_js_url defaults to es5: false), so the
# `export` keywords that make this possible are inert in production.
CARD_SHIMS = """
globalThis.HTMLElement = class {};
globalThis.customElements = { define() {}, get() { return undefined; } };
globalThis.window = globalThis;
"""

CARD_DRIVER = CARD_SHIMS + f"""
const {{ phaseAt, phaseMarks, kelvinToRgb }} = await import({json.dumps(CARD_JS.as_posix())});

const input = JSON.parse(await new Promise((resolve) => {{
  let buf = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (c) => (buf += c));
  process.stdin.on('end', () => resolve(buf));
}}));

process.stdout.write(JSON.stringify({{
  phases: input.phaseCases.map((c) => phaseAt(c.t, c.b[0], c.b[1], c.b[2], c.b[3])),
  marks: input.markCases.map((b) => phaseMarks(b[0], b[1], b[2], b[3])),
  rgb: input.kelvins.map(kelvinToRgb),
}}));
"""

# Ordinary schedules plus every way the boundaries can be out of order -
# which they can be, since they're freely settable `time` entities.
_H = 3600
MARK_CASES = [
    [6 * _H, 8 * _H, 19 * _H, 22 * _H],
    [10 * _H, 8 * _H, 19 * _H, 22 * _H],
    [6 * _H, 8 * _H, 7 * _H, 22 * _H],
    [6 * _H, 8 * _H, 19 * _H, 18 * _H],
    [6 * _H, 6 * _H, 19 * _H, 22 * _H],
    [6 * _H, 8 * _H, 8 * _H, 22 * _H],
    [22 * _H, 20 * _H, 18 * _H, 16 * _H],
    [0, 8 * _H, 19 * _H, 22 * _H],
    [6 * _H, 6 * _H, 6 * _H, 6 * _H],
    [0, 0, 0, 86399],
]


def test_dashboard_card_matches_curve_py():
    phase_cases = [
        {"t": float(t), "b": b}
        for b in MARK_CASES
        for t in list(range(0, 86400, 900)) + [x for edge in b for x in (edge - 1, edge, edge + 1)]
    ]
    kelvins = list(range(1000, 10001))

    js = _node_eval(CARD_DRIVER, {"phaseCases": phase_cases, "markCases": MARK_CASES, "kelvins": kelvins})

    phase_mismatches = [
        f"phase_at(t={c['t']}, {c['b']}) py={py} js={got}"
        for c, got in zip(phase_cases, js["phases"])
        if (py := phase_at(c["t"], *c["b"])) != got
    ]
    assert not phase_mismatches, "the card's phaseAt has drifted:\n" + "\n".join(phase_mismatches[:10])

    mark_mismatches = []
    for boundaries, got in zip(MARK_CASES, js["marks"]):
        expected = [[name, start] for name, start in phase_marks(*boundaries)]
        if expected != got:
            mark_mismatches.append(f"phase_marks({boundaries}) py={expected} js={got}")
    assert not mark_mismatches, "the card's phaseMarks has drifted:\n" + "\n".join(mark_mismatches[:10])

    rgb_mismatches = [
        f"kelvin_to_rgb({k}) py={tuple(kelvin_to_rgb(k))} js={tuple(got)}"
        for k, got in zip(kelvins, js["rgb"])
        if tuple(kelvin_to_rgb(k)) != tuple(got)
    ]
    assert not rgb_mismatches, "the card's kelvinToRgb has drifted:\n" + "\n".join(rgb_mismatches[:10])
