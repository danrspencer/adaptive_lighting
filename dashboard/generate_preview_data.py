#!/usr/bin/env python3
"""
Generates dashboard/preview_data.json - a day's worth of sample points
plus a representative "now" reading, computed with the real
pyscript/modules/adaptive_lighting/curve.py (the same module the
production sensors use). preview.html loads this to render the actual
Lovelace card with synthetic data, so you can see it without a running
Home Assistant instance.

Boundaries here (06:00 morning / 08:00 day / 18:00 evening / 22:00
night) are just representative defaults for the preview - the real
automation computes these from your own input_datetime helpers plus
sunset.

Run from the repo root: python3 dashboard/generate_preview_data.py
"""

import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pyscript" / "modules"))
from adaptive_lighting import brightness_for_phase, kelvin_for_phase, phase_at  # noqa: E402

MORNING_HOUR = 6
DAY_HOUR = 8
EVENING_HOUR = 18
NIGHT_HOUR = 22

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
    evening_ts = midnight + EVENING_HOUR * 3600
    night_ts = midnight + NIGHT_HOUR * 3600

    points = []
    for i in range(289):  # every 5 minutes across 24h, matching the real curve sensor
        t = midnight + i * 300
        phase = phase_at(t, morning_ts, day_start_ts, evening_ts, night_ts)
        points.append(
            {
                "t": int(t),
                "brightness": brightness_for_phase(phase, t, night_ts),
                "kelvin": kelvin_for_phase(phase, t, evening_ts, day_start_ts, night_ts),
            }
        )

    now = datetime.datetime.now().timestamp()
    now_phase = phase_at(now, morning_ts, day_start_ts, evening_ts, night_ts)

    data = {
        "boundaries": {
            "morning": morning_ts,
            "day": day_start_ts,
            "evening": evening_ts,
            "night": night_ts,
        },
        "sun": {
            "sunrise": midnight + SUNRISE_HOUR * 3600,
            "sunset": midnight + SUNSET_HOUR * 3600,
        },
        "points": points,
        "now_phase": now_phase,
        "now_brightness": brightness_for_phase(now_phase, now, night_ts),
        "now_kelvin": kelvin_for_phase(now_phase, now, evening_ts, day_start_ts, night_ts),
        "_now_ts": int(now),
    }

    out_path = Path(__file__).resolve().parent / "preview_data.json"
    out_path.write_text(json.dumps(data))
    print(f"wrote {len(points)} points to {out_path}")


if __name__ == "__main__":
    main()
