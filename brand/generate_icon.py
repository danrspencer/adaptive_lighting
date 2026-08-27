#!/usr/bin/env python3
"""
Generates brand/icon.svg - the integration's icon - from the actual
curve module rather than being drawn by hand: the icon IS the day's
brightness/colour curve, not an
artist's impression of it. Bar heights come from brightness_for_phase,
bar colours from kelvin_for_phase run through kelvin_to_rgb, and the
schedule is curve.py's own DEFAULT_SCHEDULE_HOURS - change the
defaults and a regenerated icon follows.

This file (and icon.svg, its output) is design/authoring tooling only -
it lives here, not inside the integration package, and is NOT what HA
actually reads. Since HA 2026.3.0, a custom integration ships its own
brand icon directly inside its own folder - see
`custom_components/adaptive_lighting_helpers/brand/` (icon.png,
icon@2x.png), served automatically via HA's local brands API with no
manifest.json changes and no external submission needed.
`home-assistant/brands` (the previous mechanism, a central repo custom
integrations used to submit icons to) has since stopped accepting PRs
for custom integrations entirely, so that path is no longer viable even
as a fallback - confirmed live, 2026-08-13: no
`custom_integrations/adaptive_lighting_helpers/` entry and no open PR
for one exist there.

This script only regenerates icon.svg; the served PNGs need re-
rendering separately after a design change (e.g. `qlmanage -t -s 256`
and `-s 512` for the two sizes, transparency preserved) into
`custom_components/adaptive_lighting_helpers/brand/{icon.png,icon@2x.png}`
- there's no automated step for that yet.

Run from the repo root: python3 brand/generate_icon.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components" / "adaptive_lighting_helpers"))
from curve import (  # noqa: E402
    DEFAULT_SCHEDULE_HOURS,
    brightness_for_phase,
    kelvin_for_phase,
    kelvin_to_rgb,
    phase_at,
)

SIZE = 256
CORNER_R = 58  # squircle-ish, matches typical brand-icon rounding
BG = "#171a26"  # deep night-blue, so both the warm and cool ends pop

# Chart area inside the tile.
PAD_X = 34
PAD_BOTTOM = 46
PAD_TOP = 56
CHART_W = SIZE - 2 * PAD_X
CHART_H = SIZE - PAD_TOP - PAD_BOTTOM

N_BARS = 7  # same bar language as the dashboard card, icon-sized

# A representative day (hours -> synthetic
# timestamps; the curve functions only care about differences).
MORNING = DEFAULT_SCHEDULE_HOURS["morning"] * 3600
DAY = DEFAULT_SCHEDULE_HOURS["day"] * 3600
EVENING = 18 * 3600  # a mid-window sunset, between earliest and latest
NIGHT = DEFAULT_SCHEDULE_HOURS["night"] * 3600

# Zoom the icon onto the interesting stretch of the day - from just
# before Morning to just after Night - rather than the full 24h, half
# of which is a flat night shelf that wastes icon real estate.
T_START = MORNING - 3 * 3600
T_END = NIGHT + 3 * 3600


def y_of(brightness):
    return SIZE - PAD_BOTTOM - (brightness / 255) * CHART_H


def main():
    # One bar per sample across the zoomed window - the dashboard card's
    # bar language, at icon scale. Each bar's height is the real
    # brightness and its colour the real Kelvin at that instant; the
    # hard jump at Morning and the evening fade are what make this
    # schedule recognisably itself.
    slot_w = CHART_W / N_BARS
    bar_w = slot_w * 0.68
    baseline = SIZE - PAD_BOTTOM

    bars = []
    for i in range(N_BARS):
        t = T_START + (T_END - T_START) * (i + 0.5) / N_BARS
        phase = phase_at(t, MORNING, DAY, EVENING, NIGHT)
        b = brightness_for_phase(phase, t, NIGHT)
        r, g, bl = kelvin_to_rgb(kelvin_for_phase(phase, t, EVENING, DAY, NIGHT))
        x = PAD_X + i * slot_w + (slot_w - bar_w) / 2
        h = baseline - y_of(b)
        bars.append(
            f'<rect x="{x:.1f}" y="{baseline - h:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
            f'rx="{bar_w / 2:.1f}" fill="rgb({r},{g},{bl})" />'
        )

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}">
  <rect x="0" y="0" width="{SIZE}" height="{SIZE}" rx="{CORNER_R}" fill="{BG}" />
  {chr(10).join('  ' + b for b in bars).strip()}
</svg>
"""

    out_path = Path(__file__).resolve().parent / "icon.svg"
    out_path.write_text(svg)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
