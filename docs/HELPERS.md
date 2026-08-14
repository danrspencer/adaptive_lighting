# Adaptive Lighting Helpers — service & sensor reference

> Part of [Adaptive Lighting](../README.md) — see there for why this project is shaped the way it is
> (in particular, [why the schedule has four named phases](../README.md#why-four-phases-not-a-continuous-curve)
> rather than a single continuous curve) and how to install it.

Four services, each documented in full in `services.yaml` (visible in Home Assistant's Developer Tools → Actions
once installed) — call them directly from your own automations or scripts, no blueprint required.

## Bring your own sensor

`apply_lighting` and `compute_lighting_groups` don't require this integration's own `sensor.adaptive_lighting` —
they'll read brightness/colour targets off any sensor entity that exposes the right attributes. That's the whole
contract, and nothing else about the entity matters (its `state` is never read):

| Attribute | Type | Required |
|---|---|---|
| `brightness` | 0-255 | yes |
| `color_temp` | Kelvin | yes |
| `rgb_color` | `[r, g, b]` | no — only needed if you're using `prefer_rgb_color` |

A minimal hand-written template sensor satisfying that contract:

```yaml
template:
  - sensor:
      - name: "My Room's Adaptive Lighting"
        state: "{{ 'Evening' if now().hour >= 18 else 'Day' }}" # anything - not read by these services
        attributes:
          brightness: "{{ 180 if now().hour >= 18 else 255 }}"
          color_temp: "{{ 3200 if now().hour >= 18 else 5500 }}"
          # Optional - only needed for prefer_rgb_color
          rgb_color: "{{ [255, 200, 150] if now().hour >= 18 else [255, 255, 255] }}"
```

Point `apply_lighting`'s `sensor_entity_id` (or the blueprint's Adaptive Lighting Sensor input) at that entity
and everything else — reachability, tolerance, override protection, two-step transitions, RGB dispatch —
works exactly the same as with this integration's own sensor.

## `adaptive_lighting_helpers.apply_lighting`

The "just make it happen" service: reads brightness/colour-temperature (and optionally RGB colour) off any
sensor entity you point it at — see ["Bring your own sensor"](#bring-your-own-sensor) above for
the exact contract — and actually turns entities on/off via `light.turn_on`/`light.turn_off`, handling
reachability, tolerance, override protection, two-step transitions, and RGB-vs-colour-temp dispatch
internally. This is what the blueprint calls.

```yaml
action: adaptive_lighting_helpers.apply_lighting
data:
  entities: [light.kitchen_1, light.kitchen_2]
  sensor_entity_id: sensor.adaptive_lighting
  transition: 2
  brightness_multipliers: { light.kitchen_2: 0.5 }
  prefer_rgb_color: true # optional - see "RGB colour" below
```

## `adaptive_lighting_helpers.compute_lighting_groups`

The pure-planner version of `apply_lighting`: given a set of light entities, a target brightness/colour-temperature,
and optional per-light brightness multipliers, returns the minimal set of groups actually needing a
`light.turn_on`/`light.turn_off` call — filters out unreachable lights, buckets by multiplier, skips anything
already within tolerance of the target, leaves externally-set lights alone, and separates out lights tagged for
two-step transitions — without touching any light itself. Use this instead of `apply_lighting` if you want to
dispatch the calls yourself (custom transition curves, logging, etc.).

```yaml
action: adaptive_lighting_helpers.compute_lighting_groups
data:
  entities: [light.kitchen_1, light.kitchen_2]
  sensor_brightness: 200
  sensor_color_temp_kelvin: 3200
  brightness_multipliers: { light.kitchen_2: 0.5 }
response_variable: plan
# plan.groups -> [{multiplier, brightness, needing_off, combined, two_step, combined_rgb, two_step_rgb}, ...]
```

### RGB colour

Both services above accept `prefer_rgb_color` (off by default). When on, entities whose `supported_color_modes`
indicates RGB support (auto-detected — nothing to configure per light) are routed to `rgb_color` instead of
`color_temp_kelvin`; entities without RGB support are unaffected. `apply_lighting` reads the RGB target straight
off `sensor_entity_id`'s `rgb_color` attribute; `compute_lighting_groups` takes it as an explicit `rgb_color`
field since it isn't reading a sensor at all. Neither service invents an RGB target on its own — see
`compute_curve` below (or `sensor.adaptive_lighting`'s own `rgb_color` attribute) for where that value comes
from, or supply your own.

## `adaptive_lighting_helpers.compute_curve`

Given today's morning/day/evening/night phase-boundary timestamps, returns the target brightness, colour
temperature, RGB colour, and phase name for a given instant (or now). Useful for building your own day-phase
sensor without any of the rest of this project.

```yaml
action: adaptive_lighting_helpers.compute_curve
data:
  morning: "{{ today_at('06:00:00') | as_timestamp }}"
  day: "{{ today_at('08:00:00') | as_timestamp }}"
  evening: "{{ today_at('18:00:00') | as_timestamp }}"
  night: "{{ today_at('22:00:00') | as_timestamp }}"
  # All optional - each defaults to the value shown, see services.yaml for the full list
  morning_brightness: 255
  morning_kelvin: 6667
  day_brightness: 255
  day_end_kelvin: 4000
  evening_brightness: 180
  evening_kelvin: 3200
  night_brightness: 80
  night_kelvin: 2700
response_variable: now
# now.phase / now.brightness / now.kelvin / now.rgb_color
```

`rgb_color` is just the Kelvin → RGB conversion of `kelvin` - useful as a ready-made `rgb_color` value for
`apply_lighting`/`compute_lighting_groups`'s `prefer_rgb_color` path even when you're not using RGB bulbs any
differently from colour-temperature ones.

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

If you'd rather have this running continuously as sensors than call `compute_curve` yourself, add a sensor from
the integration's own page (Settings → Devices & Services → Adaptive Lighting Helpers → Add Sensor) — just a
name. Adding the integration itself needs no configuration and sets up nothing beyond the services above; a
schedule only exists once you add a sensor.

You can add any number of sensors this way, each independent, each grouped under its own device — naming one
"Living Room" gets you a device called "Living Room". Rename the device later (Settings → Devices → the sensor's
device → rename) and every entity under it updates its displayed name at once — that's the only place the
sensor's *displayed* name lives; its entity_ids stay as originally created from whatever you typed here, so it's
worth getting the name right the first time rather than relying on a later rename to fix it.

Each sensor's device contains, computed the same way `compute_curve` computes them and refreshed every 60 seconds:

| Entity | What it is |
|---|---|
| `sensor.<name_>adaptive_lighting` | Combined "right now" reading — state is the phase (Morning/Day/Evening/Night), `attributes.brightness` (0-255), `attributes.color_temp` (Kelvin), and `attributes.rgb_color` (`[r, g, b]`) are exactly the attribute names `apply_lighting`'s `sensor_entity_id` and the blueprint's `adaptive_sensor` input already read, so this can be pointed at directly. Also carries today's four phase-boundary timestamps as `attributes.morning_start`/`day_start`/`evening_start`/`night_start`, plus `attributes.evening_earliest`/`evening_latest` (the two configured bounds Evening was actually clamped between) — no separate boundary sensors, since a phase-change automation only needs a `platform: state, attribute: phase` trigger on this same entity, and anything that specifically wants a boundary time (the dashboard card, in particular) can read it straight off these attributes. `attributes.points` carries the full day as 289 `{t, brightness, kelvin}` samples — what the [dashboard card](../README.md#previewing-the-dashboard-card) reads for its chart, deliberately **not** following a manual phase override (see below) the way the rest of this entity's attributes do, since it's a full-day schedule, not a "right now" value |
| `select.<name_>adaptive_lighting_phase` | Manual override — `Auto` (default) or a specific phase. Pinning a phase holds it until the *schedule itself* next moves on (e.g. override to `Day` during `Evening` and it still becomes `Night` once Evening would naturally have ended, rather than staying on `Day` forever) — see the sticky-override switch below to disable that and keep an override until you clear it yourself instead |
| `time.<name_>morning_time` / `day_time` / `evening_earliest_time` / `evening_latest_time` / `night_time` | The five schedule boundaries — start times for Morning, Day, and Night, and Evening's earliest/latest bound. Each starts at a representative default (06:00/08:00/17:00/20:00/22:00) and is adjustable at any time; the change applies within seconds, not on the next 60s poll |
| `number.<name_>morning_brightness` / `morning_kelvin` / `day_brightness` / `day_end_kelvin` / `evening_brightness` / `evening_kelvin` / `night_brightness` / `night_kelvin` | The eight brightness (0-255)/colour-temperature (1000-10000K) curve values, one pair per phase (`day_end_kelvin` is what Day ramps down to by the time Evening starts). Each starts at the value shown in `compute_curve`'s own field list above, and is adjustable at any time |
| `switch.<name_>sticky_phase_override` | Off by default (an override self-clears at the next phase boundary). Turn on to keep a manual phase override pinned until you clear it back to `Auto` yourself instead |

`time.*`/`number.*`/`switch.*` are all tagged as configuration entities, so Home Assistant groups them under the
device's collapsed "Configuration" section rather than mixing them into the main entity list — present, and
usable from dashboards/automations, without being sixteen always-visible entities cluttering the device page.
This replaces what used to be a config-flow form only reachable via Configure - the schedule/curve values are now
just entities like anything else, immediately visible and editable from the device page, no separate step needed.

Point `apply_lighting`'s `sensor_entity_id` (or the blueprint's Adaptive Lighting Sensor input) at whichever
sensor's `sensor.<name_>adaptive_lighting` you want. A sensor's whole device is removable later from the
integration's page; there's no reconfigure form since there's nothing left to reconfigure that way - edit the
`time.*`/`number.*`/`switch.*` entities directly, or rename the device, instead.

For a dashboard, [dashboard/adaptive-lighting-section.yaml](../dashboard/adaptive-lighting-section.yaml) is a
copy-paste section with the curve graph, the phase override and sticky-override switch, and all thirteen
schedule/curve entities laid out as tiles - or skip the dashboard entirely and use the sensor's own device page
(Settings → Devices → the sensor's device), which already shows the same entities grouped for free, since
they're tagged `entity_category: config`.
