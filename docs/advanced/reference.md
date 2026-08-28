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

The services FLARE registers, and the override-protection machinery behind them.

<details open markdown="block">
  <summary>On this page</summary>
  {: .text-delta }
1. TOC
{:toc}
</details>

# FLARE — service & sensor reference
{: .no_toc }

<details open markdown="block">
  <summary>On this page</summary>
  {: .text-delta }
- TOC
{:toc}
</details>


> Part of [FLARE](../) — see there for why this project is shaped the way it is
> (in particular, [why the schedule has four named phases](../#why-four-phases-not-a-continuous-curve)
> rather than a single continuous curve) and how to install it.

Seven services, each documented in full in `services.yaml` (visible in Home Assistant's Developer Tools → Actions
once installed) — call them directly from your own automations or scripts, no blueprint required.

## `flare.apply_lighting`

The "just make it happen" service: given a target brightness/colour-temperature (and optionally RGB colour) as
plain values, actually turns entities on/off via `light.turn_on`/`light.turn_off`, handling reachability,
tolerance, override protection, two-step transitions, and RGB-vs-colour-temp dispatch internally. Neither this
nor `compute_lighting_groups` reads any sensor entity - if you're feeding these values from a sensor's own
attributes (the adaptive_lighting blueprint in this repo does exactly that, reading its own FLARE
Sensor input - see [the blueprint reference](../blueprint/#bring-your-own-sensor) for the attribute contract that
relies on), that's an ordinary template on the caller's side, not something this service does for you.

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

Adaptive lighting should stop driving a light once somebody else has taken it — a person at a switch, a scene,
another automation — and pick it up again when they let go. That needs an answer to "was the last change to
this light *ours*", which is what the claims below record.

**Claims belong to a state device, not to a caller.** A state device is a named tracking scope you configure
(Settings → Devices & Services → **FLARE Tracking** → Add state device), pointed at an area, some
devices, or specific lights. Every light resolves to exactly one:

1. a state device whose target names the **entity**
2. …names its **device**
3. …names its **area**
4. otherwise **not tracked at all**

Most specific wins; ties break on the state device's name, so the answer is stable across restarts. A light
matching nothing is simply never tracked — it stays permanently manageable. That's deliberate: a catch-all
bucket would silently absorb a light that's missing an area, where an absent scope is a visible signal.

Because the scope is decided by configuration rather than by whoever wrote last, **two automations driving the
same room share that room's claims and co-operate.** Neither reads the other's write as an intruder. If you
want them tracked separately, give them separate state devices.

`apply_lighting` names no owner at all. `force: true` still writes through regardless of who
holds a light, and the write is still recorded, so protection works again on the next non-forced call.

#### What's recorded, and why there are two claims

Each tracked light carries two claims on its state device:

- **`observed`** — a state we've seen and know is safe to write over. Populated four ways, only one of which
  we authored: a write an earlier call saw the bulb adopt, the pre-write baseline for a first-ever write, and
  the snapshot taken when a device returns from unavailable. What they share is *confidence*, not authorship.
- **`latest`** — the most recent write we sent, not yet re-observed.

Two rather than one because `apply_lighting` records the context it *issued*; nothing waits to confirm the
bulb adopted it. With a single record, one dropped write locks a light out permanently — the next tick
compares the light's real, unchanged context against a value the device never adopted, and nothing afterwards
can make those equal.

A context mismatch alone still isn't proof: HA's `Entity._context` expires 5 seconds after the service call,
so a bulb whose round-trip takes longer reports back under an unrelated context while echoing exactly what
was asked for. Each claim therefore also records its `target`, and the comparison falls back to values.

#### Nothing survives a restart, on purpose

Claims are not persisted. These are lighting overrides — losing them means a bulb someone wanted purple goes
back to being managed. After a restart nothing is tracked, so every light is manageable, which is exactly the
state you'd want anyway.

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

The point is the pairing of `live` against each claim's `target`. That comparison is what tells you whether a
hand-over was genuine or a false positive, and it's exactly what can't be reconstructed afterwards - by the
time anyone looks, the curve has moved on and a stale target says nothing about why the light was excluded.

**Edge-triggered**: it marks the light *changing hands*, not the fact that it currently is, so it fires once
per hand-over rather than repeating while the light stays taken. A restart seeds quietly - lights already
overridden before it aren't re-announced.

Home Assistant's recorder keeps it like any other event, and because it carries an `entity_id` it follows
whatever recorder filtering that light already has. It also appears in the light's own logbook timeline,
interleaved with its state changes, which is where you'd be looking anyway:

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

The tracking sensor is the storage, not a view of it — what you see in Developer Tools is the same object
override protection acts on. Its `claims` attribute is excluded from the recorder (it changes on every tick
and runs to kilobytes), so it has no history; the two counters are plain numbers that graph and produce
long-term statistics.

**The two counts deliberately don't sum to the tracked total**: a light that's off or unavailable is in
neither, because override protection doesn't apply to it at all.

**A light being overridden is a supported outcome, not a fault** — something else deliberately took it and
adaptive lighting correctly stepped back. These report who holds what; they aren't a health check.

**The Clear button** discards *every* claim the scope holds, not just the overridden ones — a guaranteed
reset rather than one that depends on agreeing about which lights are stuck. The cost: the scope's healthy
lights lose their claims too, so each is unprotected until its next write. For a live room automation that's
one tick.

### Transitions

Each phase holds its own brightness and colour and then **eases to the next
phase's over the last N minutes of its own span**. The duration is named for the
phase it runs in, because it is that phase's exit: `day_kelvin_transition` is how
long before Day ends to start easing to Evening's colour.

The transition finishing *at* the boundary is the point — if Morning starts at
06:00, the lights are at Morning's values at 06:00, not beginning a ramp toward
them. Two consequences worth knowing:

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

Brightness and colour have separate durations because they genuinely differ: by
default Day's colour slides across the whole afternoon while its brightness only eases
over the last hour or so.

### Inspecting tracked state

Each state device's `sensor.<name>_flare_tracking` makes the mechanism above inspectable directly, rather
than only indirectly through `compute_lighting_groups`'s `combined`/`needing_off` output (which tells you
*whether* a light is currently excluded, never *why*). Its `claims` attribute holds, per light, the raw
`observed`/`latest` records. `check_control` turns those into the computed `status` and `matched_via` -
`"latest-context"`, `"latest-value"`, `"observed-context"` or `"observed-value"` - saying *how* a match was
determined, so you needn't guess whether a light is `controlled` because its reported `context.id` matched
directly, or because it was rescued via the delayed-echo/mired-equivalence value comparison described above.

Records are discarded automatically once they've gone a full day without being written or observed - not just
for lights that are still around but quiet, which is harmless (no record at all reads the same as
`untracked`, never blocked, so a pruned-too-early record simply re-establishes itself on its next write), but
specifically for an entity *deleted from Home Assistant outright* (a Zigbee2MQTT group removed at the source,
say) - the one case none of the recovery handling above can detect, since there's no state left to observe
going away. Runs once at startup and hourly while running; nothing to configure.


- `controlled` — we are in control: the live `context.id` matches a claim, or it matches neither
  claim's `context.id` but its current value still matches what `latest`'s or `observed`'s own `target`
  asked for - almost certainly a delayed confirmation landing under an unrelated context (HA's
  `Entity._context` expires 5s after the service call that set it), or a write that never landed leaving the
  light on the previous one, not a real external change. Either way, not excluded from the next tick.
- `overridden` — matches neither claim's `context.id`, and the current value doesn't match either claim's own
  target either. Something has genuinely touched this light since either recorded write - whether that means
  "externally set" also depends on `force`, which this sensor can't know about.
- `unavailable` — the entity currently has no live state to compare against.
- `off` — the light's live state is `off` (not `unavailable`/`unknown`). Override protection is moot for a
  light that isn't on (see the `is_state(entity_id, "on")` precondition at the very top of the numbered check
  above) - it will be freely managed the next time it's turned on, regardless of any `observed`/`latest`
  claim recorded while it was last on.

Each state device's tracking sensor updates immediately on every write or clear-on-unavailable event, and
also polls - a light's live state can change independently of anything this integration does (a restart, an
entity reconnecting, a light dimmed by hand), and only polling keeps its view correct in that case too.

Each claim also carries `recorded_at` (ISO 8601, or `null` for the synthetic first-write baseline), i.e. when
the claim was stamped. Combined with the claim's `context_id`, that is enough to trace a claim back to what
actually happened via HA's own logbook (`logbook/get_events`, filtered by `context_id`) over a narrow window
around `recorded_at`.

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
below (or `sensor.adaptive_lighting`'s own `rgb_color` attribute) for where that value comes from, or supply your
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

The match is on the label's *id*, not its display name. That makes it easy to get silently wrong: a label whose
id doesn't line up produces no error and no log line — the bulb just goes back to combined transitions, and the
only symptom is a fade that looks slightly off.

### Keeping the list current

Because that failure is invisible, the integration checks for it. Any light whose device matches a known
two-step model but has no label raises a **repair** with a Fix button; pressing it applies the label to those
devices, creating the label itself (with the correct id) if it doesn't already exist. The check re-runs whenever
the entity or device registry changes, so pairing a new bulb surfaces it without a restart, and the repair
clears itself once the labels are in place.

The model list lives in the integration's options (Settings → Devices & Services → FLARE →
**Configure**), one glob per line. The box comes **pre-filled with the shipped defaults**, so what you see there
is the complete list the check uses — you can add to it or delete from it, and a pattern you remove is genuinely
gone rather than being re-added from a hidden layer underneath.

Patterns are case-insensitive globs matched against `"<manufacturer> <model>"`, so both `*TRADFRI bulb*` and
`IKEA*` work. Clearing the box entirely falls back to the shipped defaults rather than disabling detection — to
stop being told about unlabelled bulbs, [ignore the repair](#dismissing-the-repair) instead.

The shipped defaults live in `custom_components/flare/two_step.py` as
`DEFAULT_TWO_STEP_MODEL_PATTERNS` (currently just `*TRADFRI bulb*`). Adding a newly discovered bulb there is a
one-line PR — that's the intended way to contribute one, and it reaches every install that hasn't customised
the field. **Once you save your own list, it's yours**: later releases adding models won't change it, which is
the trade-off for the box showing exactly what runs.

Keep patterns narrow. One that's too broad is worse than a missing one — it produces a repair recommending a
label that would make those bulbs transition *worse*, two calls where one was fine.

### Dismissing the repair

The repair uses Home Assistant's own issue mechanism, so it gets the standard **Ignore** action from the
three-dot menu on the repair card — nothing specific to this integration. Ignoring is remembered permanently
(it records the HA version at the time and stays ignored across upgrades).

To bring it back, open **Settings → Repairs**, use the overflow menu at the top right and enable **Show ignored
issues** — the repair reappears in the list and can be un-ignored from there. Ignoring only silences the
notification; it doesn't change any lighting behaviour, and the check keeps running, so if you later label the
bulbs the issue clears itself as normal.

## Optional: day-phase/curve sensors

If you'd rather have this running continuously as sensors than call `compute_curve` yourself, add a sensor from
the integration's own page (Settings → Devices & Services → FLARE → Add Sensor) — just a
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
| `sensor.<name_>adaptive_lighting` | Combined "right now" reading — state is the phase (Morning/Day/Evening/Night), `attributes.brightness` (0-255), `attributes.color_temp` (Kelvin), and `attributes.rgb_color` (`[r, g, b]`) are exactly the attribute names the blueprint's `adaptive_sensor` input already reads to feed `apply_lighting`'s own `brightness`/`color_temp_kelvin`/`rgb_color` fields (see [the blueprint reference](../blueprint/#bring-your-own-sensor)), so this can be pointed at directly. Also carries today's four phase-boundary timestamps as `attributes.morning_start`/`day_start`/`evening_start`/`night_start`, plus `attributes.evening_earliest`/`evening_latest` (the two configured bounds Evening was actually clamped between) — no separate boundary sensors, since a phase-change automation only needs a `platform: state, attribute: phase` trigger on this same entity, and anything that specifically wants a boundary time (the dashboard card, in particular) can read it straight off these attributes. `attributes.points` carries the full day as 289 `{t, brightness, kelvin}` samples — what the [dashboard card](../contributing/#previewing-the-dashboard-card) reads for its chart, deliberately **not** following a manual phase override (see below) the way the rest of this entity's attributes do, since it's a full-day schedule, not a "right now" value |
| `select.<name_>adaptive_lighting_phase` | Manual override — `Auto` (default) or a specific phase. Pinning a phase holds it until the *schedule itself* next moves on (e.g. override to `Day` during `Evening` and it still becomes `Night` once Evening would naturally have ended, rather than staying on `Day` forever) — see the sticky-override switch below to disable that and keep an override until you clear it yourself instead |
| `time.<name_>morning_time` / `day_time` / `evening_earliest_time` / `evening_latest_time` / `night_time` | The five schedule boundaries — start times for Morning, Day, and Night, and Evening's earliest/latest bound. Each starts at a representative default (06:00/08:00/17:00/20:00/22:00) and is adjustable at any time; the change applies within seconds, not on the next 60s poll |
| `number.<name>_<phase>_brightness` / `_kelvin` | The eight curve values — brightness (0-255) and colour temperature (1000-10000K), one pair per phase. Each starts at the value shown in `compute_curve`'s field list above, and is adjustable at any time |
| `number.<name>_<phase>_brightness_transition` / `_kelvin_transition` | The eight transition durations, in minutes — see below |
| `switch.<name_>sticky_phase_override` | Off by default (an override self-clears at the next phase boundary). Turn on to keep a manual phase override pinned until you clear it back to `Auto` yourself instead |

`time.*`/`number.*`/`switch.*` are all tagged as configuration entities, so Home Assistant groups them under the
device's collapsed "Configuration" section rather than mixing them into the main entity list — present, and
usable from dashboards/automations, without being sixteen always-visible entities cluttering the device page.
This replaces what used to be a config-flow form only reachable via Configure - the schedule/curve values are now
just entities like anything else, immediately visible and editable from the device page, no separate step needed.

Point the blueprint's FLARE Sensor input (or your own template reading the same attributes into
`apply_lighting`'s `brightness`/`color_temp_kelvin`/`rgb_color` fields) at whichever sensor's
`sensor.<name_>adaptive_lighting` you want. A sensor's whole device is removable later from the
integration's page; there's no reconfigure form since there's nothing left to reconfigure that way - edit the
`time.*`/`number.*`/`switch.*` entities directly, or rename the device, instead.

For a dashboard, [dashboard/adaptive-lighting-section.yaml](https://github.com/danrspencer/flare/blob/main/dashboard/adaptive-lighting-section.yaml) is a
copy-paste section with the curve graph, the phase override and sticky-override switch, and all thirteen
schedule/curve entities laid out as tiles - or skip the dashboard entirely and use the sensor's own device page
(Settings → Devices → the sensor's device), which already shows the same entities grouped for free, since
they're tagged `entity_category: config`.
