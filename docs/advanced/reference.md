---
title: Integration reference
parent: Power users
nav_order: 1
permalink: /advanced/reference/
render_with_liquid: false
# Liquid is off for this page: it contains Home Assistant Jinja, which
# shares Liquid's {{ }} delimiters. With Liquid on, those examples render
# as empty strings and nothing errors - see tests/test_docs_site.py.
---

# Integration reference
{: .no_toc }

The services FLARE registers, the override-protection machinery behind them, and the
schedule sensors.

<details open markdown="block">
  <summary>On this page</summary>
  {: .text-delta }
1. TOC
{:toc}
</details>

Seven services, callable from your own automations or scripts with no blueprint involved.
Each field is documented in full in Developer Tools → Actions.

## `flare.apply_lighting`

Takes a brightness and colour temperature as plain values and turns entities on or off,
handling reachability, tolerance, override protection, two-step transitions and
RGB-vs-colour-temp dispatch.

Neither this nor `compute_lighting_groups` reads a sensor entity. Feeding them from a
sensor's attributes is an ordinary template on your side — see
[Bring your own sensor](../../blueprint/#bring-your-own-sensor) for the attribute shape the
blueprint uses.

```yaml
action: flare.apply_lighting
data:
  entities: [light.kitchen_1, light.kitchen_2]
  brightness: 200
  color_temp_kelvin: 3200
  transition: 2
  brightness_multipliers: { light.kitchen_2: 0.5 }
  prefer_rgb_color: true # optional - see "RGB colour" below
  force: false # optional - see "Override protection" below
```

### Override protection

FLARE stops driving a light once something else has taken it — a switch, a scene, another
automation — and picks it up again when released.

**Claims belong to a state device, not to a caller.** A state device is a named tracking scope you configure
(Settings → Devices & Services → **FLARE Tracking** → Add state device), pointed at an area, some
devices, or specific lights. Every light resolves to exactly one:

1. a state device whose target names the **entity**
2. …names its **device**
3. …names its **area**
4. otherwise **not tracked at all**

Most specific wins; ties break on the state device's name, so the result is stable across restarts. A light
matching nothing is never tracked and stays permanently manageable — there is no catch-all bucket, so a light
missing an area shows up as an absent scope rather than being quietly absorbed.

Because scope comes from configuration rather than from whoever wrote last, **two automations driving the same
room share that room's claims and co-operate.** Give them separate state devices to track them apart.

`force: true` writes through regardless of who holds a light. The write is still recorded, so protection works
again on the next non-forced call.

#### The two claims

Each tracked light carries two claims on its state device:

- **`observed`** — a state known to be safe to write over: one an earlier call saw the bulb adopt, the
  pre-write baseline for a first write, or the snapshot taken when a device returns from unavailable.
- **`latest`** — the most recent write sent, not yet re-observed.

Two rather than one because a write is recorded when issued, not when confirmed. With a single record, one
dropped write would lock a light out permanently: the next tick compares the light's unchanged context against
a value the device never adopted, and nothing afterwards can make those equal.

A context mismatch alone isn't proof either. HA's `Entity._context` expires 5 seconds after the service call, so
a bulb whose round-trip takes longer reports back under an unrelated context while echoing exactly what was
asked for. Each claim also records its `target`, and the comparison falls back to values.

Claims are **not persisted**. After a restart nothing is tracked, so every light is manageable.

#### Turning a light off is an override

Switching a light off is a decision worth respecting, the same as dimming it, so FLARE leaves it off rather than
relighting it on the next tick.

A turn-off records a target of its own (`{"state": "off"}`), which is what separates *FLARE turned this off*
from *somebody else did*. Without it there would be nothing to compare once the write's context expires after
five seconds, and a room turned off at bedtime could never be turned on again.

**A scope releases every claim it holds once none of its lights are on.** Nobody is using the room, so handing
it back overrides nobody's choice — and it is what ends a hand turn-off. Two things to know:

- **"The room" is the scope, not the physical room.** A light that no state device tracks is not consulted, so
  it holds nothing open.
- **Anything not reporting `on` counts as dark**, including unavailable. Requiring every tracked light to report
  `off` would let one permanently unavailable entity — an orphaned Zigbee group, say — veto the release forever.

This is also the automatic way out of `overridden`, which previously needed the Clear button or the light going
off and on again.

### The hand-over event

Every time a tracked light passes into someone else's hands, this fires
`flare_light_overridden` carrying a full snapshot of that moment:

```yaml
entity_id: light.kitchen_1
scope: Kitchen                        # the state device that lost it
device_id: ...                        # so the row lands in that device's Activity
previous_status: controlled
live_context_id: 01M11...
live: { state: on, brightness: 12, color_temp_kelvin: 6500, rgb_color: null }
observed: { context_id: ..., target: {...}, recorded_at: ... }
latest:   { context_id: ..., target: {...}, recorded_at: ... }
```

Compare `live` against each claim's `target` to tell a genuine hand-over from a false positive. That comparison
can't be reconstructed later — by the time you look, the curve has moved on.

It is **edge-triggered**: it fires once when a light changes hands, not repeatedly while it stays taken. Lights
already overridden before a restart aren't re-announced.

The event carries an `entity_id`, so it follows that light's existing recorder filtering and appears in its
logbook timeline alongside its state changes:

> **kitchen_lights** released this light to something else (last asked for 255/6667, found 12/6500)

Trigger on it like any event:

```yaml
trigger:
  - platform: event
    event_type: flare_light_overridden
```

### The state device's entities

Each state device carries four entities, all on its own device so they're renamed and deleted together:

| entity | |
|---|---|
| `sensor.<name>_flare_tracking` | **the claims themselves.** State is the number of lights tracked; the `claims` attribute holds the per-light `observed`/`latest` records |
| `sensor.<name>_flare_controlled` | how many of its lights it is currently driving |
| `sensor.<name>_flare_overridden` | how many are currently held by something else |
| `button.<name>_flare_clear` | press to discard this scope's tracked state |

The `claims` attribute is excluded from the recorder, so it has no history; the two counters are plain numbers
that graph and produce long-term statistics.

**The two counts don't sum to the tracked total.** An unavailable light is in neither, and nor is one holding
no claim.

**Overridden is a normal outcome, not a fault** — something else took the light and FLARE stepped back. These
entities report who holds what; they aren't a health check.

**Clear discards every claim in the scope**, not just the overridden ones. The healthy lights lose their claims
too and are unprotected until their next write — one tick, for a live room automation.

### Transitions

Each phase holds its own brightness and colour, then **eases to the next phase's over the
last N minutes of its own span**. The duration is named for the phase it runs in:
`day_kelvin_transition` is how long before Day ends to start easing to Evening's colour.

Transitions finish *at* the boundary, so if Morning starts at 06:00 the lights are at
Morning's values at 06:00, not beginning a ramp toward them. Two consequences:

- **`0` is a hard cut**, and a legitimate choice. Some boundaries should be
  visible: setting `evening_kelvin_transition: 0` makes Night arrive as a step.
- **A duration longer than its phase covers the whole phase.** It clamps rather
  than bleeding backwards, so `day_kelvin_transition: 1440` reads as "always be
  transitioning" — which is exactly how the default Day slides from Morning's
  colour to Evening's across the whole afternoon.

{: .note }
> Night is the only phase whose span crosses midnight, so its transition runs at
> the *end* of the early-morning stretch — the minutes before Morning, not before
> midnight.

Brightness and colour have separate durations: by default Day's colour slides across the
whole afternoon while its brightness eases over the last hour.

### Inspecting tracked state

`sensor.<name>_flare_tracking`'s `claims` attribute holds the raw `observed`/`latest` records per light.
`check_control` turns those into a `status` plus a `matched_via` — `"latest-context"`, `"latest-value"`,
`"observed-context"` or `"observed-value"` — telling you *how* a light was matched, not just that it was.

| status | meaning |
|---|---|
| `controlled` | the live `context.id` matches a claim, or the current value still matches a claim's `target` — a delayed confirmation, or a write that never landed. Not excluded from the next tick |
| `overridden` | matches neither claim's context, and the value matches neither claim's target. Something else has touched it |
| `unavailable` | no live state to compare against |
| `off` | the light is off and holds no claim at all. A light FLARE turned off is `controlled`; one somebody else turned off is `overridden` |

The tracking sensor updates on every write and clear, and also polls, since a light's live state can change
without FLARE doing anything.

Each claim carries `recorded_at` (ISO 8601, or `null` for the first-write baseline). With the claim's
`context_id` that's enough to trace it through HA's logbook (`logbook/get_events`, filtered by `context_id`).

Records are pruned after a full day with no write or observation. This matters mainly for an entity deleted
from Home Assistant outright — a Zigbee2MQTT group removed at source — which nothing else can detect, since
there is no state left to observe. Runs at startup and hourly; nothing to configure.

## `flare.compute_lighting_groups`

The pure-planner version of `apply_lighting`: given a set of light entities, a target brightness/colour-temperature,
and optional per-light brightness multipliers, returns the minimal set of groups actually needing a
`light.turn_on`/`light.turn_off` call — filters out unreachable lights, buckets by multiplier, skips anything
already within tolerance of the target, leaves externally-set lights alone, and separates out lights tagged for
two-step transitions — without touching any light itself. Use this instead of `apply_lighting` if you want to
dispatch the calls yourself (custom transition curves, logging, etc.).

```yaml
action: flare.compute_lighting_groups
data:
  entities: [light.kitchen_1, light.kitchen_2]
  brightness: 200
  color_temp_kelvin: 3200
  brightness_multipliers: { light.kitchen_2: 0.5 }
response_variable: plan
# plan.groups -> [{multiplier, brightness, needing_off, combined, two_step, combined_rgb, two_step_rgb}, ...]
```

### RGB colour

Both services above accept `prefer_rgb_color` (off by default) and an explicit `rgb_color` field. When
`prefer_rgb_color` is on, entities whose `supported_color_modes` indicates RGB support (auto-detected — nothing
to configure per light) are routed to `rgb_color` instead of `color_temp_kelvin`; entities without RGB support
are unaffected. Neither service invents an RGB target on its own, or reads one off a sensor — see `compute_curve`
below (or `sensor.<name>_flare`'s own `rgb_color` attribute) for where that value comes from, or supply your
own. `rgb_color` can be left unset, or passed explicitly as `null` (useful if you're templating it from a source
that doesn't always have one) — either way it's simply ignored unless `prefer_rgb_color` is also on.

## `flare.compute_curve`

Given today's morning/day/evening/night phase-boundary timestamps, returns the target brightness, colour
temperature, RGB colour, and phase name for a given instant (or now). Useful for building your own day-phase
sensor without any of the rest of this project.

```yaml
action: flare.compute_curve
data:
  morning: "{{ today_at('06:00:00') | as_timestamp }}"
  day: "{{ today_at('08:00:00') | as_timestamp }}"
  evening: "{{ today_at('18:00:00') | as_timestamp }}"
  night: "{{ today_at('22:00:00') | as_timestamp }}"
  # All optional - each defaults to the value shown, see services.yaml for the full list
  morning_brightness: 255
  morning_kelvin: 6667
  day_brightness: 255
  day_kelvin: 6667
  evening_brightness: 180
  evening_kelvin: 3200
  night_brightness: 80
  night_kelvin: 2700
  # ...and one transition per phase per channel, in minutes
  day_kelvin_transition: 1440
  evening_brightness_transition: 60
response_variable: now
# now.phase / now.brightness / now.kelvin / now.rgb_color
```

`rgb_color` is just the Kelvin → RGB conversion of `kelvin` - useful as a ready-made `rgb_color` value for
`apply_lighting`/`compute_lighting_groups`'s `prefer_rgb_color` path even when you're not using RGB bulbs any
differently from colour-temperature ones.

## Two-step transition bulbs

Some bulbs can't take brightness and colour temperature in one command — sent together, they snap or drop one of
the two. `apply_lighting` sends those as two sequential half-length calls instead, and picks which bulbs get that
treatment from a Home Assistant **label**:

| | |
|---|---|
| Label id | `no_combined_transition` |
| Goes on | the light **entity** or its **device** — either works, device is more durable |
| Overridable per call | `two_step_label` on `apply_lighting` / `compute_lighting_groups` |

The match is on the label's *id*, not its display name. A label whose id doesn't line up produces no error and
no log line — the bulb silently goes back to combined transitions.

### Keeping the list current

Any light whose device matches a known two-step model but has no label raises a **repair** with a Fix button,
which applies the label and creates it with the correct id if needed. The check re-runs on registry changes, so
a newly paired bulb surfaces without a restart, and the repair clears once the labels are in place.

The model list is in the integration's options (Settings → Devices & Services → FLARE → **Configure**), one
case-insensitive glob per line, matched against `"<manufacturer> <model>"` — both `*TRADFRI bulb*` and `IKEA*`
work. The box is pre-filled with the shipped defaults, so what you see is the complete list in use; a pattern
you delete is genuinely gone. Clearing the box entirely falls back to the defaults rather than disabling
detection — to stop being told about unlabelled bulbs, [ignore the repair](#dismissing-the-repair).

{: .note }
> Once you save your own list it's yours: later releases adding models won't change it. Adding a bulb to
> `DEFAULT_TWO_STEP_MODEL_PATTERNS` in `two_step.py` is a one-line PR and reaches every install that hasn't
> customised the field.

Keep patterns narrow. Too broad is worse than missing — it recommends a label that makes those bulbs transition
*worse*, two calls where one was fine.

### Dismissing the repair

Use the standard **Ignore** action on the repair card. It stays ignored across upgrades. To bring it back, open
**Settings → Repairs** and enable **Show ignored issues** from the overflow menu.

Ignoring only silences the notification — the check keeps running, so labelling the bulbs later clears the issue
as normal.

## Optional: day-phase/curve sensors

To have the curve running continuously rather than calling `compute_curve` yourself, add a sensor from the
Schedules entry (Settings → Devices & Services → FLARE Schedules → Add Sensor). It asks only for a name.

Add as many as you like; each is independent and gets its own device. Renaming the device later updates every
entity's displayed name, but **entity_ids keep the name you first typed**, so it's worth getting right up front.

Each sensor's device contains, computed the same way `compute_curve` computes them and refreshed every 60 seconds:

| Entity | What it is |
|---|---|
| `sensor.<name>_flare` | The "right now" reading — see the attribute table below. Point the blueprint's FLARE Sensor input at this |
| `select.<name>_flare_phase` | Manual phase override — `Auto` (default) or a specific phase. An override holds until the schedule itself next moves on: pin `Day` during Evening and it still becomes `Night` when Evening would have ended. The sticky switch below changes that |
| `time.<name>_morning_time` / `day_time` / `evening_earliest_time` / `evening_latest_time` / `night_time` | The five schedule boundaries — start times for Morning, Day, and Night, and Evening's earliest/latest bound. Each starts at a representative default (06:00/08:00/17:00/20:00/22:00) and is adjustable at any time; the change applies within seconds, not on the next 60s poll |
| `number.<name>_<phase>_brightness` / `_kelvin` | The eight curve values — brightness (0-255) and colour temperature (1000-10000K), one pair per phase. Each starts at the value shown in `compute_curve`'s field list above, and is adjustable at any time |
| `number.<name>_<phase>_brightness_transition` / `_kelvin_transition` | The eight transition durations, in minutes — see below |
| `switch.<name>_sticky_phase_override` | Off by default (an override self-clears at the next phase boundary). Turn on to keep a manual phase override pinned until you clear it back to `Auto` yourself instead |

The `time.*`/`number.*`/`switch.*` entities are tagged `entity_category: config`, so Home Assistant collapses
them under the device's Configuration section. Edit them from the device page, a dashboard, or an automation —
there is no configuration form. Removing a schedule means removing its device from the integration's page.

### `sensor.<name>_flare` attributes

| Attribute | |
|---|---|
| state | the phase — `Morning`/`Day`/`Evening`/`Night` |
| `brightness` | 0-255 |
| `color_temp` | Kelvin |
| `rgb_color` | `[r, g, b]` |
| `morning_start` / `day_start` / `evening_start` / `night_start` | today's phase-boundary timestamps |
| `evening_earliest` / `evening_latest` | the bounds Evening was clamped between |
| `points` | the full day as 289 `{t, brightness, kelvin}` samples, for the chart |

There are no separate boundary sensors: a phase-change automation needs only a
`state` trigger with `attribute: phase` on this entity.

`points` does **not** follow a manual phase override, unlike the other attributes — it's a full-day schedule,
not a right-now value.

For a dashboard,
[dashboard/flare-section.yaml](https://github.com/danrspencer/flare/blob/main/dashboard/flare-section.yaml) is a
copy-paste section with the curve card, the phase override and every schedule and curve entity as tiles. The
sensor's own device page already groups the same entities for free.
