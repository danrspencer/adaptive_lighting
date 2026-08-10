# Adaptive Lighting

Two independent pieces, designed to work together but not coupled to each other:

- **Adaptive Lighting Helpers** — a standalone Home Assistant integration exposing brightness/colour-temperature
  curve math and per-light grouping (reachability, tolerance, manual-override protection, two-step transitions) as
  plain HA services. Useful in your own automations even if you never touch the blueprint below.
- **The Adaptive Lighting blueprint** — a per-room automation built on top of those services: brightness and
  colour temperature follow a solar schedule, motion controls on/off, scenes can take over partially or entirely,
  manual changes are respected, and lights that don't reach their target get corrected automatically.

![Adaptive Lighting Curve card, showing brightness and colour temperature through the day](dashboard/curve-preview.svg)

## Adaptive Lighting Helpers (the integration)

Three services, each documented in full in `services.yaml` (visible in Home Assistant's Developer Tools → Actions
once installed) — call them directly from your own automations or scripts, no blueprint required.

### `adaptive_lighting_helpers.compute_lighting_groups`

Given a set of light entities, a target brightness/colour-temperature, and optional per-light brightness
multipliers, returns the minimal set of groups actually needing a `light.turn_on`/`light.turn_off` call: filters
out unreachable lights, buckets by multiplier, skips anything already within tolerance of the target, leaves
manually-set lights alone, and separates out lights tagged for two-step transitions.

```yaml
action: adaptive_lighting_helpers.compute_lighting_groups
data:
  entities: [light.kitchen_1, light.kitchen_2]
  sensor_brightness: 200
  sensor_color_temp_kelvin: 3200
  brightness_multipliers: { light.kitchen_2: 0.5 }
response_variable: plan
# plan.groups -> [{multiplier, brightness, needing_off, combined, two_step}, ...]
```

### `adaptive_lighting_helpers.compute_curve`

Given today's morning/day/evening/night phase-boundary timestamps, returns the target brightness, colour
temperature, and phase name for a given instant (or now). Useful for building your own day-phase sensor without
any of the rest of this project.

```yaml
action: adaptive_lighting_helpers.compute_curve
data:
  morning: "{{ today_at('06:00:00') | as_timestamp }}"
  day: "{{ today_at('08:00:00') | as_timestamp }}"
  evening: "{{ today_at('18:00:00') | as_timestamp }}"
  night: "{{ today_at('22:00:00') | as_timestamp }}"
response_variable: now
# now.phase / now.brightness / now.kelvin
```

### `adaptive_lighting_helpers.compute_scene_coverage`

Given a candidate scene and the entities you want a default behaviour applied to, works out which of those
entities the scene actually covers — hand covered ones to the scene, apply your default (adaptive lighting or
anything else) to whatever's left. A scene only counts if it exists and everything it covers is within
`scope_entities`; a scene reaching outside that scope, or one that doesn't exist, is treated the same as no scene
at all. Nothing here is specific to adaptive lighting, or even to lighting.

```yaml
action: adaptive_lighting_helpers.compute_scene_coverage
data:
  scene_entity_id: scene.kitchen_night
  scope_entities: [light.kitchen_1, light.kitchen_2, light.kitchen_strip_effect]
  target_entities: [light.kitchen_1, light.kitchen_2]
response_variable: coverage
# coverage.scene_active / scene_valid / covered_entities / uncovered_entities
```

### Optional: day-phase/curve sensors

If you'd rather have this running continuously as sensors than call `compute_curve` yourself, fill in the five
`input_datetime` fields when setting up the integration (Settings → Devices & Services → Adaptive Lighting
Helpers → Configure) — morning/day/night start times, and evening's earliest/latest bound (evening itself tracks
sunset, clamped between those two). Leave them blank and you just get the three services above with nothing else.

Filling them in adds, computed the same way `compute_curve` computes them, refreshed every 60 seconds:

| Entity | What it is |
|---|---|
| `sensor.morning_start` / `day_start` / `evening_start` / `night_start` | Today's boundary, state + `attributes.timestamp` |
| `sensor.day_phase` | Morning / Day / Evening / Night |
| `sensor.solar_adaptive_lighting_brightness` | Target brightness right now (0-255) |
| `sensor.solar_adaptive_lighting_color_temperature` | Target colour temperature right now (Kelvin) |
| `sensor.adaptive_lighting_curve` | `attributes.points`: the full day as 289 `{t, brightness, kelvin}` samples — what the [dashboard card](#previewing-the-dashboard-card) reads |

These entity IDs are forced to match what a Jinja `packages/*.yaml` day-phase setup would typically use (rather
than the usual integration-prefixed auto-generated ones), so this is meant as a drop-in replacement for one — if
you're migrating from your own version of that, remove it first or these will get suffixed `_2`.

## The blueprint

Built on the services above, but the two are only loosely coupled — the blueprint just calls
`compute_lighting_groups` the same way it calls `light.turn_on`, and doesn't otherwise assume anything about how
that service is implemented.

### Solar-driven brightness & colour temperature

Tracks a target brightness and Kelvin value that changes through the day — full brightness and cool white during
the day, warming and dimming through the evening, dim and warm at night — following a configurable
morning/day/evening/night schedule (evening tracks sunset, clamped between an earliest and latest bound). Applied
roughly once a minute while the room is occupied, so lights drift with the schedule instead of jumping.

The [dashboard curve card](#previewing-the-dashboard-card) also plots today's actual sunrise/sunset (from
`sun.sun`) against the schedule, so it's easy to see at a glance how far the configured boundaries and earliest/
latest clamps are actually tracking the sun.

### Motion-driven on/off

Turns a room on when motion starts and off `no_motion_wait` seconds after it stops. A motion/occupancy sensor is
optional — without one, the blueprint still keeps already-on lights updated with the adaptive curve, it just
won't turn anything on by itself.

### Manual override detection

A light changed directly — wall switch, app, voice assistant — is left alone rather than being overwritten on
the next adaptive tick. Detected via `context.user_id`: a real person's action through the UI always carries a
user id, while automations and a device regaining power after an outage don't. The latter case is not treated as
an override, so a bulb reconnecting after a power or Zigbee blip is brought back in line automatically rather
than left stuck at its last known state.

### Scene handoff

An optional template returns the entity_id of a scene to activate instead of the adaptive curve — for example,
`scene.kitchen_night` when a day-phase sensor reads `Night`. The template is written directly by whoever sets up
the room, so the mapping is explicit rather than guessed from a naming convention. A scene only qualifies if
every entity it touches is within the blueprint's own scope (the controlled lights, plus sibling entities on the
same device, such as a light strip's effect selector); a scene reaching outside that scope, or one that doesn't
exist (a typo, a renamed scene), is treated the same as the template returning nothing.

### Per-light brightness scaling

An optional template maps `entity_id` to a brightness multiplier:

| Value | Effect |
|---|---|
| a number | scales that light's brightness, floored at 1 |
| `0` | turns the light off during the adaptive step |
| `null` / `false` | skips the light entirely on power-on (for another automation or a fixed scene to own), but still includes it when the room turns off |

### Additional triggers

Both templates above are re-rendered fresh on every run, regardless of what triggered it — so an entity that
one of them depends on (a TV, for a brightness multiplier that dims the room while it's on; whatever a scene
template checks) can be added to Additional Triggers to take effect immediately, rather than waiting for the
next adaptive tick.

### Two-step transitions

Bulbs that can't transition brightness and colour temperature together (some IKEA TRÅDFRI models) can be tagged
with a `no_combined_transition` label and are sent as two sequential half-length transitions instead of one.
Everything else gets a single combined call.

### Reachability and redundancy filtering

Lights reported `unavailable` or `unknown` are skipped. Lights already within tolerance of the target
brightness/colour-temperature (±2 brightness, ±10K, to absorb rounding differences some bulbs report back) are
left alone rather than recommanded on every tick.

### Self-healing

On a configurable interval, if the room is unoccupied but a light is still on, the off command is retried. This
recovers from dropped commands (a missed Zigbee message, for example) without manual intervention.

## Repository layout

```
custom_components/adaptive_lighting_helpers/
    __init__.py    registers the three services against real HA state
    sensor.py      optional day-phase/curve sensors (see "Optional:
                   day-phase/curve sensors" above) - only set up if the
                   config entry has schedule entities configured
    curve.py       brightness/colour-temperature schedule
    grouping.py    reachability, multiplier bucketing, tolerance checks,
                   manual-override protection, two-step/combined routing
    scenes.py      scene-coverage gap filling (apply a scene, then a
                   default for whatever it doesn't cover)
    manifest.json, config_flow.py, services.yaml, strings.json,
    translations/  standard HA integration/HACS scaffolding
    curve.py, grouping.py, and scenes.py are pure Python, no Home
    Assistant dependency - testable directly, and usable from anywhere
    that wants the math without the HA service/sensor wrapper around
    it. __init__.py and sensor.py are the only files that touch `hass`.

hacs.json
    HACS repository metadata for the integration.

blueprints/automation/danspencer/adaptive_lighting.yaml
    The automation blueprint: triggers, conditions, target resolution,
    and the action sequence (which service to call, with what target).
    Deliberately named differently from any prior "Adaptive Lighting
    Unified" blueprint so the two can run side by side while rooms are
    migrated over individually, rather than one replacing the other
    in place.

www/adaptive-lighting-curve-card.js
    Custom Lovelace card rendering the day's curve as a rendered-colour
    chart, with a live "now" marker.

dashboard/
    house-settings-card.yaml   card config to add to a view
    preview.html                renders the real card against synthetic
                                 data, no Home Assistant instance needed
    generate_preview_data.py    generates that synthetic data
    render_preview_svg.py       renders the screenshot above as a
                                 standalone SVG

tests/
    pytest suite for curve.py and grouping.py.
```

Triggers, conditions, and target resolution stay in the blueprint; Home Assistant `condition:` blocks can't call
a service, so anything a condition depends on has to remain template-based. Multiplier bucketing, tolerance
checks, and transition routing are implemented in the integration and unit tested. See `CLAUDE.md` for further
implementation notes, including the (fairly involved) history of getting a custom integration to load correctly
at all.

## Installation

### Adaptive Lighting Helpers

Not yet published to the HACS default store. Add this repository as a HACS custom repository (HACS → the "⋮"
menu → Custom repositories → this repo's URL, category "Integration"), install, restart Home Assistant (a brand
new `custom_components` entry needs a restart to be discovered, not just a reload), then add it once via
Settings → Devices & Services → Add Integration → "Adaptive Lighting Helpers". The setup form is entirely
optional — leave every field blank to just get the three services above, or fill in the five `input_datetime`
fields for the day-phase/curve sensors too (see "Optional: day-phase/curve sensors" above).

For local testing before it's on HACS at all, `scripts/link_into_ha.sh` copies
`custom_components/adaptive_lighting_helpers/` directly onto an HA host over SSH — see the script's own header
comment for details and why it copies rather than symlinks.

### The blueprint

Import directly via Home Assistant's own blueprint importer (Settings → Automations & Scenes → Blueprints →
Import Blueprint, paste this repo's raw URL to
`blueprints/automation/danspencer/adaptive_lighting.yaml`) — this is a plain HA feature, not something HACS is
involved in. Requires Adaptive Lighting Helpers to be installed first, since the blueprint calls its
`compute_lighting_groups` service.

Note: if migrating from an older, pre-rewrite version of this blueprint, the inputs have changed
(`scene_sensor`/`scene_name_prefix` → `scene_template`/`extra_triggers`) — every room automation using the old
inputs will show as misconfigured until updated. Worth doing deliberately, room by room, rather than all at once.

### The dashboard card

Register `www/adaptive-lighting-curve-card.js` as a Lovelace resource (Settings → Dashboards → Resources → Add
Resource, URL `/local/adaptive-lighting-curve-card.js`, type JavaScript Module) and add the card config from
`dashboard/house-settings-card.yaml` to a view. Not currently HACS-distributed either (see CLAUDE.md's "Open
question" section for the plan to make it a proper HACS frontend plugin).

## Configuration

Add an automation using the "Adaptive Lighting" blueprint per room, and set:

| Input | Required | Description |
|---|---|---|
| Light | yes | Entities, a device, or an area to control |
| Adaptive Lighting Sensor | yes | Sensor providing brightness/colour temperature |
| Motion Sensor | no | Enables motion-driven on/off |
| Additional Triggers | no | Entities that trigger immediate re-evaluation (see [Additional triggers](#additional-triggers)) |
| Scene Template | no | Template returning a scene entity_id to hand the room over to |
| Brightness Multiplier Template | no | Per-light brightness scaling |
| Wait time | no | Seconds to keep lights on after motion stops (default 120) |
| Reconcile Interval | no | Self-healing check interval (default every 5 minutes) |
| Motion On / Motion Off / Adaptive Transition | no | Transition durations for each trigger type |

## Previewing the dashboard card

```bash
python3 dashboard/generate_preview_data.py
python3 -m http.server 8934
# open http://localhost:8934/dashboard/preview.html
```

Renders the actual card component against generated data, without a Home Assistant instance.

## Testing

```bash
pip install pytest
pytest
```

No Home Assistant dependency for `curve.py`/`grouping.py` themselves; `tests/fakes.py` provides a fake
state/registry lookup, and `tests/conftest.py` imports them directly (bypassing the integration's `__init__.py`,
which does need `homeassistant` — see its own comment for why). CI (`.github/workflows/tests.yml`) runs the
suite on push and PR across Python 3.9 and 3.13.

## Status

The pure-Python core (`curve.py`, `grouping.py`, `scenes.py`) and the integration wrapping it as HA services
are both written, unit tested, and **installed via HACS and confirmed working against a live Home Assistant
instance** — all three services verified registered and functionally correct, and the blueprint's full
compute-groups-then-turn-on-lights path exercised end to end against real hardware. The optional day-phase/
curve sensors (`sensor.py`) haven't been configured or tested live yet. See CLAUDE.md's "Current status"
section for the full rundown.
