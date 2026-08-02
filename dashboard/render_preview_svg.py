#!/usr/bin/env python3
"""
Renders dashboard/preview_data.json (see generate_preview_data.py) as a
standalone SVG - a static equivalent of the interactive Lovelace card,
for embedding in README.md. No browser or Home Assistant needed.

Colour math (Kelvin -> RGB) is Tanner Helland's approximation, the same
one www/adaptive-lighting-curve-card.js uses - kept in sync by hand
since it's a display concern, not schedule logic (same reasoning the
card's own comment gives for not sharing it with curve.py).

Run from the repo root: python3 dashboard/render_preview_svg.py
"""

import json
import math
from pathlib import Path

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
FOOTNOTE_H = 24
CARD_PAD = 16
CARD_W = VB_W + CARD_PAD * 2
CARD_H = TITLE_H + NOW_LABEL_H + VB_H + FOOTNOTE_H + CARD_PAD


def clamp(v, lo, hi):
    return min(max(v, lo), hi)


def kelvin_to_rgb(kelvin):
    temp = kelvin / 100
    r = 255 if temp <= 66 else clamp(329.698727446 * (temp - 60) ** -0.1332047592, 0, 255)
    if temp <= 66:
        g = clamp(99.4708025861 * math.log(temp) - 161.1195681661, 0, 255)
    else:
        g = clamp(288.1221695283 * (temp - 60) ** -0.0755148492, 0, 255)
    if temp >= 66:
        b = 255
    elif temp <= 19:
        b = 0
    else:
        b = clamp(138.5177312231 * math.log(temp - 10) - 305.0447927307, 0, 255)
    return round(r), round(g), round(b)


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
    .footnote {{ fill: #6f6f6f; font-size: 12px; }}
  </style>
  <rect x="0" y="0" width="{CARD_W}" height="{CARD_H}" rx="12" fill="#ffffff" stroke="#e0e0e0" />
  <text x="{CARD_PAD}" y="28" class="title">Adaptive Lighting Curve</text>
  <text x="{CARD_PAD}" y="{TITLE_H + 18}" class="now-label">{now_label}</text>
  <g transform="translate({CARD_PAD}, {TITLE_H + NOW_LABEL_H})">
    {"".join(bars)}
    {"".join(boundary_lines)}
    {"".join(hour_ticks)}
    {now_marker}
    <line x1="{PAD_L}" y1="{BASELINE_Y}" x2="{VB_W - PAD_R}" y2="{BASELINE_Y}" class="axis-line" />
  </g>
  <text x="{CARD_PAD}" y="{TITLE_H + NOW_LABEL_H + VB_H + 18}" class="footnote">Evening starts at {fmt_time(boundaries["evening"])}, following tonight's sunset.</text>
</svg>
'''

    out_path = Path(__file__).resolve().parent / "curve-preview.svg"
    out_path.write_text(svg)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
