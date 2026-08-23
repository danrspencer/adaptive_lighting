# Adaptive Lighting Helpers — service & sensor reference

> Part of [Adaptive Lighting](../README.md) — see there for why this project is shaped the way it is
> (in particular, [why the schedule has four named phases](../README.md#why-four-phases-not-a-continuous-curve)
> rather than a single continuous curve) and how to install it.

Seven services, each documented in full in `services.yaml` (visible in Home Assistant's Developer Tools → Actions
once installed) — call them directly from your own automations or scripts, no blueprint required.

## `adaptive_lighting_helpers.apply_lighting`

The "just make it happen" service: given a target brightness/colour-temperature (and optionally RGB colour) as
plain values, actually turns entities on/off via `light.turn_on`/`light.turn_off`, handling reachability,
tolerance, override protection, two-step transitions, and RGB-vs-colour-temp dispatch internally. Neither this
nor `compute_lighting_groups` reads any sensor entity - if you're feeding these values from a sensor's own
attributes (the adaptive_lighting blueprint in this repo does exactly that, reading its own Adaptive Lighting
Sensor input - see [docs/BLUEPRINT.md](BLUEPRINT.md#bring-your-own-sensor) for the attribute contract that
relies on), that's an ordinary template on the caller's side, not something this service does for you.

```yaml
action: adaptive_lighting_helpers.apply_lighting
data:
  entities: [light.kitchen_1, light.kitchen_2]
  brightness: 200
  color_temp_kelvin: 3200
  transition: 2
  brightness_multipliers: { light.kitchen_2: 0.5 }
  prefer_rgb_color: true # optional - see "RGB colour" below
  owner_id: "{{ this.entity_id }}" # optional - see "Override protection" below
  force: false # optional - see "Override protection" below
```

### Override protection

A light already on gets left alone once something other than this integration's own last write has touched
it since — a person, another automation, or a device regaining power. Two independent pieces of information
feed into that check:

- **`context.id`** — not something you set; it's Home Assistant's own built-in causality marker. Every state
  change carries one, and every service call gets one too - either passed explicitly, or a fresh, unrelated
  one HA generates for it. Every write `apply_lighting` itself issues is recorded against the `context.id` it
  used, so a later call can tell "has anything touched this light since my last write" just by comparing the
  light's current context against what was recorded.
- **`owner_id`** (optional, any string) — entirely your own invention, how a caller identifies *itself*. The
  blueprint passes its own `this.entity_id` (e.g. `automation.living_room_lights`), so each room's writes
  carry a stable identity distinguishing "the Living Room automation" from "the Kitchen automation" - two
  calls that otherwise look identical to Home Assistant.

Each entity keeps not one recorded write but two - `confirmed` (a write some *earlier* call actually observed
landing) and `pending` (the most recent write attempted, not yet verified either way). This is what lets a
single dropped write self-heal instead of permanently locking the light out: `apply_lighting` records the
`context.id` it *issued*, not the one the physical bulb actually adopted - those two calls are asynchronous,
so nothing here waits to confirm a command really landed before recording it. If it silently failed, a
single-record design would compare the light's real, unchanged context against the one recorded, find a
mismatch, and conclude - permanently - that the light was touched externally, since nothing that happens
afterward can retroactively make the live context equal a value the device never adopted. (Confirmed live: a
kitchen light dropped a colour-mode command at a phase boundary and sat stuck on the stale colour temperature
for over an hour, excluded from every tick in between.)

The fix doesn't need to know *why* a write failed, only to notice when one did and try again: on each call,
if the light's live context matches `pending`, that previous attempt is now known to have landed and gets
promoted (`confirmed <- pending`) before the new attempt overwrites `pending`. If live context instead still
matches the *old* `confirmed`, that means `pending` never landed - `confirmed` is left exactly as it was and
only `pending` is replaced. Either way the light is retried on the very next tick rather than locked out;
`confirmed` is only ever replaced by an *observed* match, never by assumption, so it survives any number of
consecutive dropped writes.

A light's very first-ever write has no earlier `confirmed` to fall back on if it drops - so the context.id
live *before* that first write (almost certainly not this integration's own) is recorded as the baseline
instead, with no owner attached. That's not claiming the pre-existing state as this caller's write; it's
using "the light hasn't changed" as the same retry signal every later dropped write relies on - if the first
write drops, the light's context stays at exactly that pre-write value, and the next call recognises the
match and retries cleanly rather than treating a brand-new attempt as having no history at all.

The check itself asks, in this order, for one specific light:

1. **Is the light currently off?** Free to manage - nothing to protect.
2. **Was `force: true` passed?** Free to manage, unconditionally (the write is still recorded normally - see
   below).
3. **Was `owner_id` omitted?** Free to manage, unconditionally (recorded with `owner_id: null`).
4. **Is there no `confirmed` and no `pending` claim at all for this light?** A genuinely brand new entity,
   never yet considered - free to manage.
5. **Does the light's current `context.id` match `pending`?** Its owner claims it, unless that owner is a
   *different* `owner_id` than the one asking now - in which case, externally set.
6. **Otherwise, does it match `confirmed`?** Same check: that claim's owner decides it, a different
   `owner_id` (including the synthetic first-write baseline's, which claims nobody) means the write is free
   to proceed.
7. **Neither matches, and a `confirmed` claim exists.** Something has touched this light since either
   recorded write - externally set; left alone. Unless: `pending` recorded what its write actually asked for
   (brightness/colour-temperature, or brightness/RGB), and the light's *current* value still matches that
   within the same tolerance `apply_lighting` uses to decide "already correct" - see below for why that's not
   external either.

A `context.id` mismatch alone isn't conclusive proof of an external touch, even with both claims present:
Home Assistant's own `Entity._context` expires 5 seconds after the service call that set it, so a real device
whose Zigbee/MQTT round-trip confirmation takes longer than that reports its state back under a brand-new,
unrelated context - even though it's echoing exactly the value that was asked for. Without step 7's exception
above, that echo reads as an external touch, and - since nothing here ever un-marks a light while it stays
continuously on (only turning it off does) - it's excluded from every future tick indefinitely, invisible
until the phase next changes and it silently doesn't follow. Confirmed live: two kitchen spotlights sat
excluded this way for over an hour, still correctly lit the entire time.

Step 7's value comparison also recognises a colour-temperature match that a flat Kelvin tolerance alone would
miss: Zigbee bulbs communicate colour temperature in **mireds** (`1,000,000 / kelvin`, always a whole number),
not Kelvin, so Home Assistant converts a Kelvin value to mireds before sending it to the device and converts
the device's own reported mireds back to Kelvin for display - two lossy `floor()` conversions. Two Kelvin
values that floor to the identical mired reading are indistinguishable to the device, even when the gap
between them (in Kelvin terms) is much larger than the tolerance - confirmed live: `4373K` asked for, `4385K`
reported back (both floor to mired `228`), a 12K gap against a 10K default tolerance, with the bulb having
done exactly what it was told. A single mired step is worth as little as ~5K near 2700K but ~20K+ near 4500K,
so no single flat Kelvin tolerance could reliably cover this on its own - the mired-equivalence check is
always-on, on top of whatever `color_temp_tolerance` is configured.

Step 7 has one more exception on top of the value-match rescue: a `pending` write that's very recent (within
120 seconds of being issued) is never concluded "externally set," even if neither its context nor its value
match yet - a genuine device round-trip simply might not have landed. Without this, a mass simultaneous write
(a whole room's worth of lights at once - a phase transition being the obvious case) reliably misjudged
"overridden" for any device whose confirmation hadn't landed within a single tick, and because an excluded
light is never written again, that one early misjudgment became permanent: confirmed live, a Night-to-Day
transition locked out a dozen-plus kitchen/dining lights simultaneously this way, recoverable only by manually
clicking Clear on the write-tracking card. Past that 120-second window, the ordinary "overridden" conclusion
applies as before - this is a brief grace period on a genuinely fresh write, not a general loosening of the
check.

Steps 5 and 6 are both really the same question ("does a recorded owner conflict with the one asking now?"),
just checked against two different claims instead of one - two different callers writing the *same* light
with *different* `owner_id`s never look "externally set" to *each other* by context alone, since each one's
own write is always the most recent context from its own point of view; only comparing `owner_id` catches it.
Concretely: Kitchen's automation force-writes a light Living Room normally owns (`owner_id:
"automation.kitchen_lights"`, `force: true`), and nothing else touches the light afterward. Living Room's
next regular tick sees a perfectly valid, matching `context.id` - nothing's changed since Kitchen's write -
but the recorded `owner_id` is Kitchen's, not its own, so it still correctly leaves the light alone rather
than "helpfully" overwriting Kitchen's deliberate change. `context.id` answers "has *anything* changed";
`owner_id` answers "was it *me*."

Two ways to bypass the check for a single call:

- **Leave `owner_id` unset entirely** — skips the check and always writes, but doesn't claim the write for
  anyone: a *later* call, even one passing a real `owner_id`, sees no conflicting claim either (a write
  recorded with no owner doesn't count against anybody) — so this is a clean, fully anonymous "just do it."
- **Pass `force: true`** — also skips the check, but *alongside* a real `owner_id`, so the write is
  attributed to that caller. A later, non-forced call under that same `owner_id` then correctly recognises
  it as its own, rather than finding an orphaned record and getting stuck treating its *own* forced write as
  external. Use this when the caller wants to force through **and** keep normal protection working
  afterward — e.g. a script the user runs deliberately to bring a light back under adaptive control without
  turning it off first.

A device regaining power gets a fresh context of its own too, so at this level alone it looks identical to a
real external change - but this integration handles that case for you automatically, regardless of who's
calling `apply_lighting`: it clears an entity's own protection record the moment it's *observed* going
unavailable/unknown, so by the time it reconnects there's no stale record left for its new context to
conflict with - a perfectly ordinary, non-forced call manages it again, the same as a brand new entity. No
caller-side handling needed for this specific case - the blueprint's own `recovered` trigger exists purely so
this happens *promptly* (the moment a light actually recovers, rather than waiting for whatever next calls
`apply_lighting` for that room) - see [docs/BLUEPRINT.md](BLUEPRINT.md#override-detection).

A plain Home Assistant restart needs its own handling, separate from the above: it gives *every* entity a
fresh context the moment HA comes back up, even a light that never actually went unavailable and whose
reported value hasn't changed at all - a genuine drop is only one of the two ways a light's context can change
without this integration seeing it happen. Left alone, every already-tracked light would look externally set
the moment HA restarts, and - since an externally-set light is never written - stay that way until something
else happened to touch it. This integration closes the gap two ways together, since either alone misses some
lights: on startup, it snapshots the current live context of every tracked entity that's *already* reporting a
real state as its new baseline (the same "no real claim, but nothing's changed" logic a first-ever write
already uses); and, since a real restart puts nearly every entity through `unavailable`/`unknown` first, and
this one-shot pass runs early enough that some are still mid-reconnect when it does, the clear-on-unavailable
listener above also watches the *opposite* direction - the moment any tracked entity is seen coming back from
unavailable/unknown to a real state, it gets the identical snapshot treatment, live, rather than staying
stuck until whatever caught it at startup happens again. Either way, a plain, non-forced call manages the
light normally on the very next tick instead of treating the restart itself as an override.

### Using override protection standalone

Everything above is also its own pair of services - `check_ownership` (read-only) and `record_ownership`
(records a write) - not specific to lights, or to this integration's own `apply_lighting`. Any automation can
use them directly on its own entities:

```yaml
action: adaptive_lighting_helpers.check_ownership
data:
  entities: [light.kitchen_1]
  owner_id: "{{ this.entity_id }}"
response_variable: ownership
# ownership.results["light.kitchen_1"] -> {"blocked": false, "status": "controlled", "owner_id": "...", "matched_via": "context"}
# matched_via is "context" (a direct context.id match) or "value" (the delayed-echo/mired rescue above) for a
# "pending"/"controlled" status, null otherwise - useful for understanding *why* a light is considered ours,
# not just that it is.
```

```yaml
# After actually issuing your own light.turn_on, so a later check_ownership call recognises it as yours:
action: adaptive_lighting_helpers.record_ownership
data:
  entities: [light.kitchen_1]
  owner_id: "{{ this.entity_id }}"
  targets:
    light.kitchen_1: { brightness: 200, color_temp_kelvin: 3000 }
```

`apply_lighting`/`compute_lighting_groups` use the exact same underlying logic internally (a direct Python
call, not a service-to-service round trip) - `check_ownership`'s `status` values are the same ones
`sensor.adaptive_lighting_write_tracking` shows (see below), and `targets` is the same shape `apply_lighting`
itself records automatically on every write it makes.

A third service, `clear_ownership`, is the manual escape hatch for a light stuck reporting `overridden` with no
other way back - possible because `apply_lighting`/`compute_lighting_groups` never call `record_ownership`
internally for anything already excluded, so an overridden light's own `pending` claim can go permanently stale
(most concretely: during a ramping curve, once its recorded target drifts more than a tick or two away from
where the curve has since moved on to):

```yaml
action: adaptive_lighting_helpers.clear_ownership
data:
  entities: [light.kitchen_1]
```

The next write to a cleared entity, from anyone, is treated exactly like a brand-new entity's first write - no
owner-conflict check is possible until a fresh claim exists to compare against. The **Adaptive Lighting Write
Tracking** dashboard card exposes this as a "Clear" button on every row, no confirmation prompt - it's a
diagnostic bookkeeping entry, not the light itself, and a fresh claim gets re-established the moment anything
next writes to that entity.

### Inspecting write-tracking state

`sensor.adaptive_lighting_write_tracking` makes the mechanism above inspectable directly, rather than only
indirectly through `compute_lighting_groups`'s `combined`/`needing_off` output (which tells you *whether* a
light is currently excluded, never *why*). Its state is the number of lights currently tracked; its `entities`
attribute holds, per light, the raw `confirmed`/`pending` claims plus a computed `status` and `owner_id` -
whichever claim's owner actually matched to produce that `status` (`null` for `off`/`unavailable`/`overridden`/a
claimless `controlled`, since there's nothing to attribute in those cases - the same value `check_ownership`
returns for a given entity, surfaced here without needing to ask on anyone's behalf), plus `matched_via` -
`"context"` or `"value"` for a `pending`/`controlled` status, `null` otherwise - saying *how* that match was
determined, so a viewer doesn't have to guess whether a light is "controlled" because its own reported
context.id matched directly, or because it was rescued via the delayed-echo/mired-equivalence value comparison
described above. The **Adaptive Lighting Write Tracking** dashboard card shows this as a small annotation
under each status badge.

Records are discarded automatically once they've gone a full day without being written or observed - not just
for lights that are still around but quiet, which is harmless (see the numbered check above: no record at all
reads the same as `unclaimed`, never blocked, so a pruned-too-early record for a still-real light simply
re-establishes itself on its next write), but specifically for an entity *deleted from Home Assistant outright*
(a Zigbee2MQTT group removed at the source, say) - the one case none of the recovery/restart handling above can
ever detect, since there's no state left in Home Assistant to observe going away. Runs once at startup and
hourly while running; nothing to configure.

- `controlled` — the light's live `context.id` matches the `confirmed` claim, settled; or it matches neither
  claim's `context.id` but its current value still matches what `pending`'s own `target` asked for - almost
  certainly that write's own delayed confirmation landing under an unrelated context (HA's `Entity._context`
  expires 5s after the service call that set it), not a real external change. Either way, not excluded from
  the next tick.
- `pending` — matches `pending` only, not yet independently reconfirmed by a later tick.
- `overridden` — matches neither claim's `context.id`, and the current value doesn't match `pending`'s own
  target either. Something has genuinely touched this light since either recorded write - whether that means
  "externally set" for a given caller also depends on `owner_id` (see the numbered check above), which this
  sensor can't evaluate without knowing which `owner_id` would be asking.
- `unavailable` — the entity currently has no live state to compare against.
- `off` — the light's live state is `off` (not `unavailable`/`unknown`). Override protection is moot for a
  light that isn't on (see the `is_state(entity_id, "on")` precondition at the very top of the numbered check
  above) - it will be freely managed the next time it's turned on, regardless of any `confirmed`/`pending`
  claim recorded while it was last on.

This entity is entry-scoped, not tied to any one schedule instance (it exists even with zero "Add Sensor"
instances configured), and deliberately has no device of its own - the write-tracking data it shows isn't
naturally owned by any one room's device, and giving it one would mean it shows up in the device-rename/
area-picker dialog the next time the integration is added, which this project avoids elsewhere for the same
reason (see CLAUDE.md's "Auto-seeded Default sensor" entry). It updates immediately on every write or
clear-on-unavailable event, and also polls every 15s - a light's live state can change independently of
`write_tracking.py` ever being touched (a restart, most obviously), and only polling keeps `status` correct in
that case too.

Each claim also carries `recorded_at` (ISO 8601, or `null` for the synthetic first-write baseline - see
`async_record`'s docstring) - when write_tracking.py actually stamped that claim. It's what lets the
**Adaptive Lighting Write Tracking** dashboard card (`custom:adaptive-lighting-write-tracking-card`, ships with
the integration the same way the curve card does - see
[dashboard/write-tracking-card.yaml](../dashboard/write-tracking-card.yaml) for the snippet to paste in) trace a
claim's raw `context.id` back to what actually happened: clicking "Trace" on a claim queries HA's own logbook
(`logbook/get_events`, filtered by `context_id`) over a narrow window around `recorded_at`, rather than this
integration trying to re-derive "what caused this" itself. A claim with no `recorded_at` can't be traced this
way - there's no time window to search.

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

The model list lives in the integration's options (Settings → Devices & Services → Adaptive Lighting Helpers →
**Configure**), one glob per line. The box comes **pre-filled with the shipped defaults**, so what you see there
is the complete list the check uses — you can add to it or delete from it, and a pattern you remove is genuinely
gone rather than being re-added from a hidden layer underneath.

Patterns are case-insensitive globs matched against `"<manufacturer> <model>"`, so both `*TRADFRI bulb*` and
`IKEA*` work. Clearing the box entirely falls back to the shipped defaults rather than disabling detection — to
stop being told about unlabelled bulbs, [ignore the repair](#dismissing-the-repair) instead.

The shipped defaults live in `custom_components/adaptive_lighting_helpers/two_step.py` as
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
| `sensor.<name_>adaptive_lighting` | Combined "right now" reading — state is the phase (Morning/Day/Evening/Night), `attributes.brightness` (0-255), `attributes.color_temp` (Kelvin), and `attributes.rgb_color` (`[r, g, b]`) are exactly the attribute names the blueprint's `adaptive_sensor` input already reads to feed `apply_lighting`'s own `brightness`/`color_temp_kelvin`/`rgb_color` fields (see [docs/BLUEPRINT.md](BLUEPRINT.md#bring-your-own-sensor)), so this can be pointed at directly. Also carries today's four phase-boundary timestamps as `attributes.morning_start`/`day_start`/`evening_start`/`night_start`, plus `attributes.evening_earliest`/`evening_latest` (the two configured bounds Evening was actually clamped between) — no separate boundary sensors, since a phase-change automation only needs a `platform: state, attribute: phase` trigger on this same entity, and anything that specifically wants a boundary time (the dashboard card, in particular) can read it straight off these attributes. `attributes.points` carries the full day as 289 `{t, brightness, kelvin}` samples — what the [dashboard card](../README.md#previewing-the-dashboard-card) reads for its chart, deliberately **not** following a manual phase override (see below) the way the rest of this entity's attributes do, since it's a full-day schedule, not a "right now" value |
| `select.<name_>adaptive_lighting_phase` | Manual override — `Auto` (default) or a specific phase. Pinning a phase holds it until the *schedule itself* next moves on (e.g. override to `Day` during `Evening` and it still becomes `Night` once Evening would naturally have ended, rather than staying on `Day` forever) — see the sticky-override switch below to disable that and keep an override until you clear it yourself instead |
| `time.<name_>morning_time` / `day_time` / `evening_earliest_time` / `evening_latest_time` / `night_time` | The five schedule boundaries — start times for Morning, Day, and Night, and Evening's earliest/latest bound. Each starts at a representative default (06:00/08:00/17:00/20:00/22:00) and is adjustable at any time; the change applies within seconds, not on the next 60s poll |
| `number.<name_>morning_brightness` / `morning_kelvin` / `day_brightness` / `day_end_kelvin` / `evening_brightness` / `evening_kelvin` / `night_brightness` / `night_kelvin` | The eight brightness (0-255)/colour-temperature (1000-10000K) curve values, one pair per phase (`day_end_kelvin` is what Day ramps down to by the time Evening starts). Each starts at the value shown in `compute_curve`'s own field list above, and is adjustable at any time |
| `switch.<name_>sticky_phase_override` | Off by default (an override self-clears at the next phase boundary). Turn on to keep a manual phase override pinned until you clear it back to `Auto` yourself instead |

`time.*`/`number.*`/`switch.*` are all tagged as configuration entities, so Home Assistant groups them under the
device's collapsed "Configuration" section rather than mixing them into the main entity list — present, and
usable from dashboards/automations, without being sixteen always-visible entities cluttering the device page.
This replaces what used to be a config-flow form only reachable via Configure - the schedule/curve values are now
just entities like anything else, immediately visible and editable from the device page, no separate step needed.

Point the blueprint's Adaptive Lighting Sensor input (or your own template reading the same attributes into
`apply_lighting`'s `brightness`/`color_temp_kelvin`/`rgb_color` fields) at whichever sensor's
`sensor.<name_>adaptive_lighting` you want. A sensor's whole device is removable later from the
integration's page; there's no reconfigure form since there's nothing left to reconfigure that way - edit the
`time.*`/`number.*`/`switch.*` entities directly, or rename the device, instead.

For a dashboard, [dashboard/adaptive-lighting-section.yaml](../dashboard/adaptive-lighting-section.yaml) is a
copy-paste section with the curve graph, the phase override and sticky-override switch, and all thirteen
schedule/curve entities laid out as tiles - or skip the dashboard entirely and use the sensor's own device page
(Settings → Devices → the sensor's device), which already shows the same entities grouped for free, since
they're tagged `entity_category: config`.
