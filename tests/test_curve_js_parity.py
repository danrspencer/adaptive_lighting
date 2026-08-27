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
)

CURVE_JS = Path(__file__).resolve().parent.parent / "docs" / "assets" / "js" / "curve.js"

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

# Defaults, plus ranges chosen to land on exact .5 ties in the ramps -
# an evening/night brightness span of 81 puts the 1.6x fade on halves,
# and odd Kelvin endpoints do the same for the colour ramps.
CURVE_VALUE_SETS = [
    dict(DEFAULT_CURVE_VALUES),
    {**DEFAULT_CURVE_VALUES, "evening_brightness": 161, "night_brightness": 80},
    {**DEFAULT_CURVE_VALUES, "morning_kelvin": 6501, "day_end_kelvin": 4001, "evening_kelvin": 3001, "night_kelvin": 2701},
    {**DEFAULT_CURVE_VALUES, "evening_brightness": 3, "night_brightness": 254, "morning_kelvin": 2000, "day_end_kelvin": 6500},
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
    brightnessForPhase(c.phase, c.t, b.nightTs, c.values),
    kelvinForPhase(c.phase, c.t, b.eveningTs, b.dayStartTs, b.nightTs, c.values),
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
        py_brightness = brightness_for_phase(phase, t, b["nightTs"], **_brightness_kwargs(v))
        py_kelvin = kelvin_for_phase(phase, t, b["eveningTs"], b["dayStartTs"], b["nightTs"], **_kelvin_kwargs(v))

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
    return {k: values[k] for k in ("morning_brightness", "day_brightness", "evening_brightness", "night_brightness")}


def _kelvin_kwargs(values):
    return {k: values[k] for k in ("morning_kelvin", "day_end_kelvin", "evening_kelvin", "night_kelvin")}
