#!/usr/bin/env python3
"""
Generates dashboard/preview_data.json - a day's worth of sample points
plus a representative "now" reading, computed with the real
custom_components/adaptive_lighting_helpers/curve.py (the same module
the compute_curve service uses). preview.html loads this to render the
actual Lovelace card with synthetic data, so you can see it without a
running Home Assistant instance.

Boundaries here (06:00 morning / 08:00 day / 22:00 night, evening
clamped between a 17:00 earliest and 20:00 latest bound around a
19:45 representative sunset) are just representative defaults for the
preview - the real integration computes these from the five schedule
times configured on its config entry, plus the actual sun.sun.
Evening itself is DERIVED below via the same
max(earliest, min(sunset, latest)) clamp coordinator.py uses, not a
fixed hour - it needs to actually respond to the earliest/latest/sunset
values above it or the "why evening starts when it does" footnote ends
up describing numbers that don't add up (this bit the preview once
already: earliest/latest/sunset were added without wiring them into
evening_ts, so the footnote confidently explained a boundary that had
nothing to do with the reasoning shown).

Run from the repo root: python3 dashboard/generate_preview_data.py
"""

import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components" / "adaptive_lighting_helpers"))
from curve import phase_at, targets_for_phase  # noqa: E402

MORNING_HOUR = 6
DAY_HOUR = 8
NIGHT_HOUR = 22
EVENING_EARLIEST_HOUR = 17
EVENING_LATEST_HOUR = 20

# Representative sunrise/sunset for the preview, deliberately offset from
# the schedule boundaries above - sunrise doesn't track Morning at all in
# the real automation, and Evening tracks sunset only approximately (it's
# clamped between an earliest/latest bound), so the two shouldn't usually
# line up exactly.
SUNRISE_HOUR = 6.4
SUNSET_HOUR = 19.75


def main():
    midnight = datetime.datetime.combine(datetime.date.today(), datetime.time(0, 0, 0)).timestamp()
    morning_ts = midnight + MORNING_HOUR * 3600
    day_start_ts = midnight + DAY_HOUR * 3600
    night_ts = midnight + NIGHT_HOUR * 3600
    evening_earliest_ts = midnight + EVENING_EARLIEST_HOUR * 3600
    evening_latest_ts = midnight + EVENING_LATEST_HOUR * 3600
    sunset_ts = midnight + SUNSET_HOUR * 3600
    # Same clamp coordinator.py's _compute_boundaries uses for the real
    # sensor - see this file's module docstring for why this can't be a
    # fixed hour.
    evening_ts = max(evening_earliest_ts, min(sunset_ts, evening_latest_ts))

    points = []
    for i in range(289):  # every 5 minutes across 24h, matching the real curve sensor
        t = midnight + i * 300
        phase = phase_at(t, morning_ts, day_start_ts, evening_ts, night_ts)
        targets = targets_for_phase(phase, t, evening_ts, day_start_ts, night_ts)
        points.append(
            {
                "t": int(t),
                "brightness": targets["brightness"],
                "kelvin": targets["kelvin"],
                "kelvin_rgb": targets["kelvin_rgb"],
            }
        )

    now = datetime.datetime.now().timestamp()
    now_phase = phase_at(now, morning_ts, day_start_ts, evening_ts, night_ts)
    now_targets = targets_for_phase(now_phase, now, evening_ts, day_start_ts, night_ts)

    data = {
        "boundaries": {
            "morning": morning_ts,
            "day": day_start_ts,
            "evening": evening_ts,
            "night": night_ts,
            "evening_earliest": evening_earliest_ts,
            "evening_latest": evening_latest_ts,
        },
        "sun": {
            "sunrise": midnight + SUNRISE_HOUR * 3600,
            "sunset": sunset_ts,
        },
        "points": points,
        "now_phase": now_phase,
        "now_brightness": now_targets["brightness"],
        "now_kelvin": now_targets["kelvin"],
        "now_rgb_color": list(now_targets["rgb_color"]),
        "_now_ts": int(now),
    }

    out_path = Path(__file__).resolve().parent / "preview_data.json"
    out_path.write_text(json.dumps(data))
    print(f"wrote {len(points)} points to {out_path}")


if __name__ == "__main__":
    main()
