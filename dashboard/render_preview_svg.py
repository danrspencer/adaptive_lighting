#!/usr/bin/env python3
"""
Renders dashboard/preview_data.json (see generate_preview_data.py) as a
standalone SVG - a static equivalent of the interactive Lovelace card,
for embedding in README.md. No browser or Home Assistant needed.

Colour math (Kelvin -> RGB) is curve.kelvin_to_rgb - the same function
the compute_curve/apply_lighting services and www/adaptive-lighting-
curve-card.js's kelvinToRgb() all agree with (previously this file kept
its own hand-maintained third copy of the algorithm; consolidated once
kelvin_to_rgb became real integration logic, not just a display concern).

Run from the repo root: python3 dashboard/render_preview_svg.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components" / "adaptive_lighting_helpers"))
from curve import kelvin_to_rgb  # noqa: E402

VB_W = 960
VB_H = 220
PAD_L = 34
PAD_R = 12
PAD_TOP = 44
PAD_BOTTOM = 26
CHART_W = VB_W - PAD_L - PAD_R
CHART_H = VB_H - PAD_TOP - PAD_BOTTOM
BASELINE_Y = VB_H - PAD_BOTTOM

TITLE_H = 40
NOW_LABEL_H = 26
SUN_LABEL_H = 18
FOOTNOTE_H = 24
CARD_PAD = 16
CARD_W = VB_W + CARD_PAD * 2
CARD_H = TITLE_H + NOW_LABEL_H + SUN_LABEL_H + VB_H + FOOTNOTE_H + CARD_PAD


def rgb_to_hex(rgb):
    return "#" + "".join(f"{v:02x}" for v in rgb)


def fmt_time(t):
    import datetime

    return datetime.datetime.fromtimestamp(t).strftime("%H:%M")


def main():
    data = json.loads((Path(__file__).resolve().parent / "preview_data.json").read_text())
    points = data["points"]
    boundaries = data["boundaries"]

    day_start = points[0]["t"]
    day_end = points[-1]["t"]
    span = day_end - day_start

    def x_of(t):
        return PAD_L + ((t - day_start) / span) * CHART_W

    def h_of(brightness):
        return (brightness / 255) * CHART_H

    bars = []
    bar_w = CHART_W / (len(points) - 1) + 0.6
    for p in points:
        x = x_of(p["t"])
        h = h_of(p["brightness"])
        color = rgb_to_hex(kelvin_to_rgb(p["kelvin"]))
        bars.append(
            f'<rect x="{x - bar_w / 2:.2f}" y="{BASELINE_Y - h:.2f}" '
            f'width="{bar_w:.2f}" height="{h:.2f}" fill="{color}" />'
        )

    hour_ticks = []
    for h in range(0, 25, 3):
        t = day_start + h * 3600
        hour_ticks.append(
            f'<text x="{x_of(t):.1f}" y="{VB_H - 6}" class="axis-label" text-anchor="middle">{h:02d}:00</text>'
        )

    boundary_defs = [
        ("Morning", boundaries["morning"]),
        ("Day", boundaries["day"]),
        ("Evening", boundaries["evening"]),
        ("Night", boundaries["night"]),
    ]
    boundary_lines = []
    for i, (label, t) in enumerate(boundary_defs):
        x = x_of(t)
        anchor = "start" if i == 0 else "end" if i == len(boundary_defs) - 1 else "middle"
        label_y = PAD_TOP - 26 if i % 2 == 0 else PAD_TOP - 10
        boundary_lines.append(
            f'<line x1="{x:.1f}" y1="{PAD_TOP}" x2="{x:.1f}" y2="{BASELINE_Y}" class="boundary-line" />'
            f'<text x="{x:.1f}" y="{label_y}" class="boundary-label" text-anchor="{anchor}">{label} {fmt_time(t)}</text>'
        )

    sunrise_x = x_of(data["sun"]["sunrise"])
    sunset_x = x_of(data["sun"]["sunset"])
    sun_markers = (
        f'<line x1="{sunrise_x:.1f}" y1="{PAD_TOP}" x2="{sunrise_x:.1f}" y2="{BASELINE_Y}" class="sun-line" />'
        f'<circle cx="{sunrise_x:.1f}" cy="{PAD_TOP}" r="3" class="sun-dot" />'
        f'<line x1="{sunset_x:.1f}" y1="{PAD_TOP}" x2="{sunset_x:.1f}" y2="{BASELINE_Y}" class="sun-line" />'
        f'<circle cx="{sunset_x:.1f}" cy="{PAD_TOP}" r="3" class="sun-dot" />'
    )
    sun_label = f"Sunrise {fmt_time(data['sun']['sunrise'])} · Sunset {fmt_time(data['sun']['sunset'])}"

    now_x = x_of(data["_now_ts"])
    now_hex = rgb_to_hex(kelvin_to_rgb(data["now_kelvin"]))
    now_marker = (
        f'<line x1="{now_x:.1f}" y1="{PAD_TOP - 4}" x2="{now_x:.1f}" y2="{BASELINE_Y}" class="now-line" />'
        f'<circle cx="{now_x:.1f}" cy="{BASELINE_Y - h_of(data["now_brightness"]):.1f}" r="4" '
        f'fill="{now_hex}" class="now-dot" />'
    )

    now_label = (
        f"Now {fmt_time(data['_now_ts'])} · {data['now_brightness']} bri · "
        f"{data['now_kelvin']}K · {data['now_phase']}"
    )

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CARD_W} {CARD_H}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif">
  <style>
    .title {{ fill: #212121; font-size: 20px; font-weight: 600; }}
    .now-label {{ fill: #6f6f6f; font-size: 13px; }}
    .axis-line {{ stroke: #888; stroke-width: 1; }}
    .axis-label {{ fill: #6f6f6f; font-size: 11px; }}
    .boundary-line {{ stroke: #6f6f6f; stroke-width: 1; stroke-dasharray: 3 3; opacity: 0.6; }}
    .boundary-label {{ fill: #212121; font-size: 11px; }}
    .now-line {{ stroke: #212121; stroke-width: 1.5; }}
    .now-dot {{ stroke: #ffffff; stroke-width: 1.5; }}
    .sun-line {{ stroke: #f5a623; stroke-width: 1.5; opacity: 0.85; }}
    .sun-dot {{ fill: #f5a623; }}
    .sun-label {{ fill: #6f6f6f; font-size: 12px; }}
    .footnote {{ fill: #6f6f6f; font-size: 12px; }}
  </style>
  <rect x="0" y="0" width="{CARD_W}" height="{CARD_H}" rx="12" fill="#ffffff" stroke="#e0e0e0" />
  <text x="{CARD_PAD}" y="28" class="title">Adaptive Lighting Curve</text>
  <text x="{CARD_PAD}" y="{TITLE_H + 18}" class="now-label">{now_label}</text>
  <text x="{CARD_PAD}" y="{TITLE_H + NOW_LABEL_H + 14}" class="sun-label">{sun_label}</text>
  <g transform="translate({CARD_PAD}, {TITLE_H + NOW_LABEL_H + SUN_LABEL_H})">
    {"".join(bars)}
    {"".join(boundary_lines)}
    {"".join(hour_ticks)}
    {sun_markers}
    {now_marker}
    <line x1="{PAD_L}" y1="{BASELINE_Y}" x2="{VB_W - PAD_R}" y2="{BASELINE_Y}" class="axis-line" />
  </g>
  <text x="{CARD_PAD}" y="{TITLE_H + NOW_LABEL_H + SUN_LABEL_H + VB_H + 18}" class="footnote">Evening starts at {fmt_time(boundaries["evening"])}, following tonight's sunset.</text>
</svg>
'''

    out_path = Path(__file__).resolve().parent / "curve-preview.svg"
    out_path.write_text(svg)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
