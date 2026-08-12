#!/usr/bin/env python3
"""
Generates brand/icon.svg - the integration's icon - from the actual
curve module, the same way dashboard/generate_preview_data.py builds
the card preview: the icon IS the day's brightness/colour curve, not an
artist's impression of it. Heights come from brightness_for_phase, the
horizontal colour ramp from kelvin_for_phase run through kelvin_to_rgb,
and the schedule is curve.py's own DEFAULT_SCHEDULE_HOURS - change the
defaults and a regenerated icon follows.

The white-ringed dot on the evening slope echoes the dashboard card's
"now" marker - the one element of visual identity this project already
has.

Home Assistant doesn't read icons from the integration directory -
they're served from the home-assistant/brands repo (custom_integrations/
adaptive_lighting_helpers/{icon.png,icon@2x.png}), which wants square
PNGs at 256 and 512 with transparency allowed. icon.svg here is the
source of truth for that submission; render it at 256/512 (e.g.
qlmanage -t -s <size>, or any SVG rasteriser) to produce the PNGs.

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

# Same representative day the preview uses (hours -> synthetic
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
STEP = 300


def sample():
    points = []
    t = T_START
    while t <= T_END:
        phase = phase_at(t, MORNING, DAY, EVENING, NIGHT)
        b = brightness_for_phase(phase, t, NIGHT)
        k = kelvin_for_phase(phase, t, EVENING, DAY, NIGHT)
        points.append((t, b, k))
        t += STEP
    return points


def x_of(t):
    return PAD_X + (t - T_START) / (T_END - T_START) * CHART_W


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

    # A sun disc setting behind the evening bar - Evening tracking
    # sunset is the schedule's signature feature, and it's what turns
    # "some bars" into a lighting icon. Centred over the first Evening
    # bar, in the open sky to the right of the full-height Day bars,
    # dipping just behind the bar's top so it reads as setting into the
    # skyline.
    evening_i = next(
        i
        for i in range(N_BARS)
        if phase_at(T_START + (T_END - T_START) * (i + 0.5) / N_BARS, MORNING, DAY, EVENING, NIGHT) == "Evening"
    )
    evening_t = T_START + (T_END - T_START) * (evening_i + 0.5) / N_BARS
    evening_top = y_of(brightness_for_phase("Evening", evening_t, NIGHT))
    SUN_R = 20
    # Floating in the open sky the evening step-down leaves - centred
    # between the Evening and Night bars, halfway between the Day bars'
    # tops and the Evening bar's top, clear of every bar so nothing
    # slices through the disc.
    evening_center = PAD_X + evening_i * slot_w + slot_w / 2
    night_center = PAD_X + (N_BARS - 1) * slot_w + slot_w / 2
    sun_x = (evening_center + night_center) / 2
    sun_y = (PAD_TOP + evening_top) / 2
    sr, sg, sb = kelvin_to_rgb(2200)  # low-sun colour, warmer than any bar

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}">
  <rect x="0" y="0" width="{SIZE}" height="{SIZE}" rx="{CORNER_R}" fill="{BG}" />
  <circle cx="{sun_x:.1f}" cy="{sun_y:.1f}" r="{SUN_R}" fill="rgb({sr},{sg},{sb})" />
  {chr(10).join('  ' + b for b in bars).strip()}
</svg>
"""

    out_path = Path(__file__).resolve().parent / "icon.svg"
    out_path.write_text(svg)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
