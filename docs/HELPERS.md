# Adaptive Lighting Helpers — service & sensor reference

> Part of [Adaptive Lighting](../README.md) — see there for why this project is shaped the way it is
> (in particular, [why the schedule has four named phases](../README.md#why-four-phases-not-a-continuous-curve)
> rather than a single continuous curve) and how to install it.

Three services, each documented in full in `services.yaml` (visible in Home Assistant's Developer Tools → Actions
once installed) — call them directly from your own automations or scripts, no blueprint required.

## `adaptive_lighting_helpers.compute_lighting_groups`

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

## `adaptive_lighting_helpers.compute_curve`

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

## `adaptive_lighting_helpers.compute_scene_coverage`

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

## Optional: day-phase/curve sensors

If you'd rather have this running continuously as sensors than call `compute_curve` yourself, fill in the five
schedule times when setting up the integration (Settings → Devices & Services → Adaptive Lighting Helpers →
Configure) — start times for Morning, Day, and Night, and Evening's earliest/latest bound. No separate helper
entities to create first — these are plain times stored directly on the integration's own config entry, editable
later from the same Configure screen. Leave them blank and you just get the three services above with nothing
else.

Filling them in adds, computed the same way `compute_curve` computes them, refreshed every 60 seconds:

| Entity | What it is |
|---|---|
| `sensor.morning_start` / `day_start` / `night_start` | Today's boundary, `attributes.timestamp` |
| `sensor.evening_start` | Today's evening boundary, `attributes.timestamp`, plus `attributes.earliest`/`latest` (the two configured bounds, for reference) |
| `sensor.adaptive_lighting` | Combined "right now" reading — state is the phase (Morning/Day/Evening/Night), `attributes.brightness` (0-255) and `attributes.color_temp` (Kelvin) are exactly the attribute names the blueprint's `adaptive_sensor` input already reads, so this can be pointed at directly |
| `sensor.adaptive_lighting_curve` | `attributes.points`: the full day as 289 `{t, brightness, kelvin}` samples — what the [dashboard card](../README.md#previewing-the-dashboard-card) reads. Deliberately does **not** follow a manual phase override (see below) — it's a full-day schedule, not a "right now" value |
| `select.adaptive_lighting_phase` | Manual override — `Auto` (default) or a specific phase. Pinning a phase holds it until the *schedule itself* next moves on (e.g. override to `Day` during `Evening` and it still becomes `Night` once Evening would naturally have ended, rather than staying on `Day` forever) — tick "Keep a manual phase override until cleared by hand" in Configure to disable that and keep an override until you clear it yourself instead |

Entity IDs are forced to match what a Jinja `packages/*.yaml` day-phase setup would typically use (rather than
the usual integration-prefixed auto-generated ones), so this is meant as a drop-in replacement for one — if
you're migrating from your own version of that, remove it first or these will get suffixed `_2`.
