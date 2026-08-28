---
title: Blueprint reference
nav_order: 4
permalink: /blueprint/
render_with_liquid: false
# Liquid is off for this page: it contains Home Assistant Jinja,
# which shares Liquid's {{ }} delimiters. With Liquid on, those
# examples render as empty strings and nothing errors. That also
# means no relative_url filter here - links are plain relative
# paths, which need no baseurl to be right.
---

# The FLARE blueprint — feature reference
{: .no_toc }

<details open markdown="block">
  <summary>On this page</summary>
  {: .text-delta }
- TOC
{:toc}
</details>


Every input, feature by feature. To install it, see the
[Quickstart](../installation/); for the services underneath, the
[integration reference](../advanced/reference/).

## Brightness & colour temperature schedule

The room's lights follow the [Morning/Day/Evening/Night schedule](../#four-phases-not-one-curve),
reapplied once a minute to whichever of them are already on so they drift with the curve rather than jumping.
This runs whether or not the room reads as occupied; occupancy only decides whether to switch lights on or off.

The blueprint reads `brightness`/`color_temp`/`rgb_color` off the FLARE Sensor and passes them to
`apply_lighting` as plain values.

### Bring your own sensor

The FLARE Sensor input can point at any entity exposing this attribute shape, not just this
integration's own schedule sensors:

| Attribute | Type | Required |
|---|---|---|
| `brightness` | 0-255 | yes |
| `color_temp` | Kelvin | yes |
| `rgb_color` | `[r, g, b]` | no — only needed if you're using `prefer_rgb_color` |

A minimal hand-written template sensor satisfying that contract:

```yaml
template:
  - sensor:
      - name: "My Room's FLARE"
        # The state can be anything. Only the phase-keyed inputs (Prefer RGB
        # During, the per-phase scenes and exclusions) read a phase name, and
        # those read it from this integration's own sensor.
        state: "{{ 'Evening' if now().hour >= 18 else 'Day' }}"
        attributes:
          brightness: "{{ 180 if now().hour >= 18 else 255 }}"
          color_temp: "{{ 3200 if now().hour >= 18 else 5500 }}"
          # Optional - only needed for prefer_rgb_color
          rgb_color: "{{ [255, 200, 150] if now().hour >= 18 else [255, 255, 255] }}"
```

The picker is filtered to this integration's own sensors, so a hand-written one won't appear in it — point at
it through the automation's **Edit in YAML** view instead.

Two triggers drive the cadence. The Update Interval time pattern (default every minute) does the routine work,
and is what keeps the room correcting itself during Morning and Night, where the curve is flat and nothing else
would fire. The sensor's state trigger fires only on an actual phase change, so a room doesn't wait up to a full
interval to notice one. The same tick drives [self-healing](#self-healing).

Update Jitter (default up to 15s) delays both, so rooms sharing a sensor don't all command in the same second —
most noticeable at a phase boundary. Motion, manual runs and Additional Triggers are never delayed.

## One target, two jobs

The Room input is a single entity/device/area/floor/label target that does double duty: the light entities within
it are what gets controlled, and any occupancy-class `binary_sensor` entities within it govern occupancy — no
separate sensor input to fill in. Pointing Room at a room's area picks up every light *and* every occupancy sensor
in that area automatically, including ones added later; picking specific entities instead lets you mix and match
(e.g. your lights plus one specific sensor from elsewhere, or a "virtual occupancy" template sensor — see below).

Occupancy detection uses Home Assistant's built-in Occupancy triggers/conditions, which only count `binary_sensor`
entities with `device_class: occupancy` — motion-class sensors aren't picked up this way, so a room with only
motion sensors won't have anything to trigger on here. To drive a room from a motion-class sensor, have a
separate automation of your own watch it and call this one (see [Additional triggers](#additional-triggers)).
Area/device selections only resolve *light* entities via entity/device/area (not floor/label) — a floor or label
selection works for occupancy but won't control any lights, so pick specific entities directly if you need one of
those to also light a room.

## Occupancy-driven on/off

Occupancy turns a room on when it's detected, and off `no_motion_wait` seconds after it clears. That is its
entire scope. It's optional: a Room with no occupancy-class sensor just never turns anything on by itself.
Lights handed off via a `null` multiplier are exempt from the turn-off — see
[Per-light brightness scaling](#per-light-brightness-scaling).

A room with no real occupancy sensor at all (or one you want to override manually — e.g. a nightlight mode) can
use a template `binary_sensor` with `device_class: occupancy` as a stand-in - Home Assistant's occupancy machinery
can't tell it apart from a real sensor. Pick it directly as an entity in Room (rather than relying on area
membership) so it's a deliberate addition, not something automatically swept in.

If Room contains more than one occupancy-class sensor (for example, a real motion sensor *plus* the nightlight
override sensor above), the room only counts as clear once **all** of them report clear — one sensor switching
off while another is still "on" doesn't turn the lights off. This is what makes the nightlight pattern actually
work as an override: leaving the override sensor "on" keeps the room lit through the night even while real motion
has stopped, rather than racing against whichever sensor happens to report clear first.

## When a light is allowed to turn on

Only three things may bring an off light on: motion actually being detected, running the automation manually, or
the room already being in active use (at least one of its *other* lights is already on). Every other trigger that
updates a room's lighting — the periodic adaptive tick, an Additional Trigger firing, or a light recovering from a
dropped connection (see [Override detection](#override-detection) below) — may only ever update lights that are
already on; it never switches a dark room's light on by itself.

The rule matters most after a Zigbee drop or a power cut: a light that reconnects off, in a room with nothing
else on, stays off rather than coming back on simply because it reconnected.

The "room already in use" branch looks at the whole room, not the individual light, so a tick can top up one
lamp that's off while the others are lit.

## Override detection

A light changed by anything other than this integration's own last write — a wall switch, an app, a voice
assistant, or another automation entirely (including one with no identifiable "user" of its own, such as one
triggered directly by a physical button) — is left alone rather than being overwritten on the next adaptive tick.
Switching a light **off** by hand counts as an override too, not just dimming or recolouring it — FLARE leaves
it off rather than relighting it on the next tick. Its scope releases every claim once none of its lights are
on, which is what ends that.

A light with no recorded write yet — brand new, or just after a restart — counts as free to manage.
[Override protection](../advanced/reference/#override-protection) covers the mechanism in full.

The blueprint declares no ownership, so there is no input for it. Which **state device** tracks a light is
resolved by the integration from its own configuration. Two automations driving the same room share that room's
claims and co-operate; give them separate state devices to track them apart.

**Running the automation manually** — "Run" in the UI, or `automation.trigger` — forces the tick through
regardless of override protection, the same as calling `apply_lighting` with `force: true`.

A device regaining power reports its own state under a fresh context, indistinguishable from an external change.
The integration handles that by clearing a light's record when it is observed going unavailable, so it comes back
free to manage.

The blueprint adds promptness: a `recovered` trigger arms while *every* light in the room is
`unavailable`/`unknown` and fires as the first one returns, running a tick immediately instead of waiting for the
next unrelated trigger.

{: .note }
> It asks whether *anything* is reachable rather than whether nothing is unavailable. One permanently
> unavailable entity — an orphaned Zigbee group, say — would make the second form false forever and disable
> recovery for the whole room. The trade-off is that a single flaky bulb returning alongside healthy siblings
> doesn't move the aggregate, so `recovered` won't fire for it; the periodic tick picks it up instead.

To force a light back under control from your own script without turning it off first, call `apply_lighting`
with `force: true`.

## Scene handoff

Two ways to hand a room over to a scene instead of the adaptive curve, usable together:

- **Per-phase scene pickers** - four optional entity pickers, one per phase (e.g. pick `scene.kitchen_night` for
  Night). The simple, explicit case: no template to write.
- **Scene Template** - an optional template returning the entity_id of a scene to activate, for cases a phase
  alone can't express — a different scene while the TV is on, say.

**The template wins whenever it returns a valid scene** - the matching phase picker is only used as the fallback,
for phases the template doesn't have an opinion on (or when no template is set at all). A scene only qualifies -
from either source - if every entity it touches is within the blueprint's own scope (the controlled lights, plus
sibling entities on the same device, such as a light strip's effect selector); a scene reaching outside that
scope, or one that doesn't exist (a typo, a renamed scene), is treated the same as returning nothing.

## Per-light brightness scaling

Two ways to scale brightness down, usable together:

- **Per-phase "lights off" lists** - four optional multi-entity pickers, one per phase. Any light picked for the
  current phase gets turned off during the adaptive step - the simple, explicit case for "this light should
  always be off during Night," with no template to write.
- **Brightness Multiplier Template** - an optional template mapping `entity_id` to a brightness multiplier, for
  anything the lists above can't express (a specific dim level rather than fully off, or a condition unrelated to
  phase - illuminance, a TV being on, etc.):

  | Value | Effect |
  |---|---|
  | a number | scales that light's brightness, clamped to 1-255 |
  | `0` | turns the light off during the adaptive step |
  | `null` / `false` | hands the light off entirely — this automation never touches it, on or off |

  Values above `1` are allowed and simply mean *"as bright as this bulb goes"* — the result is capped at 255,
  so a template can say `1.5` without having to know what the curve is currently at and do arithmetic to avoid
  overshooting.

  **`0` and `null` are not the same thing.** `0` means *"turn this light off"* — it's still this automation's
  light, it just wants it dark right now. `null` means *"this light belongs to something else"* — another
  automation, a fixed scene, a gradient effect — so it's excluded from the adaptive step *and* from both
  turn-off paths ([occupancy clearing](#occupancy-driven-onoff) and the [self-healing](#self-healing) retry).
  Handing a light off is all-or-nothing; if you want it dark when the room empties, that's something the owning
  automation has to do.

**The template's own per-entity values always win over the phase lists** on any light both mention - the lists
only fill in lights the template doesn't already cover. This is additive, not a replacement: a room whose
template already fully covers its own dimming logic doesn't need the phase lists at all, and adding one only
affects lights the template leaves untouched.

## Additional triggers

Both templates above are re-rendered fresh on every run, regardless of what triggered it — so an entity that
one of them depends on (a TV, for a brightness multiplier that dims the room while it's on; whatever a scene
template checks) can be added to Additional Triggers to take effect immediately, rather than waiting for the
next adaptive tick.

If you want an event to actually *light* the room rather than just refresh it, don't reach for this input — it
deliberately can't turn a dark room on. Have a separate automation watch whatever the event is and call
`automation.trigger` on this room's automation, which counts as a manual run and is allowed to turn lights on
(see [When a light is allowed to turn on](#when-a-light-is-allowed-to-turn-on)).

## Two-step transitions

Bulbs that can't transition brightness and colour temperature in one command (some IKEA TRÅDFRI models) are
tagged with a `no_combined_transition` label and sent as two sequential half-length transitions instead.
Everything else gets a single combined call. There is nothing to configure in the blueprint — it's a label you
add to a light or its device, and `apply_lighting` does the rest.

If a bulb whose model is known to need this isn't labelled, the integration raises a repair with a Fix button.
See [two-step transition bulbs](../advanced/reference/#two-step-transition-bulbs) for the label rules and the
model list.

## RGB colour

Prefer RGB During is a multi-select - pick which phases send RGB colour instead of colour temperature to lights
that support it (auto-detected per light, nothing to configure per bulb). Some bulbs render colour more
accurately in RGB mode than colour-temperature mode, which is the main reason to turn it on. Lights without RGB
support are unaffected either way.

Defaults to Evening and Night selected, Morning and Day not - at the high colour temperatures used during
Morning/Day (the Kelvin→RGB conversion saturates the blue channel above ~6600K), RGB can render as a blue-tinted
white rather than the clean white a bulb's native colour-temperature mode produces at the same value. Evening/Night's
warmer values don't hit that saturation point, so RGB there looks correct. Select all four (or none) if you want it
on/off unconditionally.

## Transition durations

Three separate transition times, so a room can respond quickly to someone actually walking in while still
drifting smoothly the rest of the time:

| Duration | Used for |
|---|---|
| Background Transition | The periodic Update tick, Additional Triggers, and a light recovering from a dropped connection (see [Override detection](#override-detection)) - none of these are a person waiting on a response in real time, so there's no reason to snap. Covers both the scene-activation step and the main FLARE dispatch |
| Motion On Transition | Motion being detected, and running the automation manually - "something happened, respond promptly" triggers |
| Motion Off Transition | Turning lights off - both the motion-cleared turn-off and the [self-healing](#self-healing) retry |

## Reachability and redundancy filtering

Lights reported `unavailable` or `unknown` are skipped. Lights already within tolerance of the target
(±2 brightness, ±10K, absorbing the rounding some bulbs report back) are left alone rather than re-commanded on
every tick.

## Self-healing

On each Update tick, if the room's occupancy sensors have been continuously clear for the full Wait time but a
light is still on, the off command is retried instead of the normal reapply. This recovers from a dropped
command — a missed Zigbee message, say — without intervention.

The Wait time is measured over the whole period rather than read instantaneously, so an occupancy sensor that
blips clear and back doesn't trip an early turn-off.

Lights handed off via a `null` multiplier are excluded, and don't count as "still on" for triggering it.

## Configuration

Add an automation using the "FLARE" blueprint per room, and set:

| Input | Required | Description |
|---|---|---|
| FLARE Sensor | yes | Sensor providing brightness/colour temperature - filtered to this integration's own sensors (see [Bring your own sensor](#bring-your-own-sensor) for pointing at a different one) |
| Room | no | Entity/device/area/floor/label - lights within it are controlled, occupancy sensors within it govern on/off (see [One target, two jobs](#one-target-two-jobs)) |
| Additional Triggers | no | Entities that trigger immediate re-evaluation (see [Additional triggers](#additional-triggers)) |
| **Colour** section | | |
| Prefer RGB During | no | Phases to send RGB colour instead of colour temperature to lights that support it - defaults to Evening/Night selected (see [RGB colour](#rgb-colour)) |
| **Scene Handoff** section | | |
| Scene Template | no | Template returning a scene entity_id to hand the room over to - wins over the phase pickers below when it returns one |
| Morning / Day / Evening / Night Scene | no | Per-phase scene to hand the room over to - the fallback for phases Scene Template doesn't cover (see [Scene handoff](#scene-handoff)) |
| **Brightness & Exclusions** section | | |
| Brightness Multiplier Template | no | Per-light brightness scaling - its own per-entity values win over the phase lists below |
| Lights Off During Morning / Day / Evening / Night | no | Lights to turn off during that phase - fills in whatever Brightness Multiplier Template doesn't already cover (see [Per-light brightness scaling](#per-light-brightness-scaling)) |
| **Timing** section | | |
| Wait time | no | Seconds to keep lights on after motion stops (default 120) |
| Update Interval | no | How often to reapply the schedule on a fixed interval - also the self-healing check interval, there's no separate one (default every minute, see [Brightness & colour temperature schedule](#brightness--colour-temperature-schedule)) |
| Update Jitter | no | Max random delay (seconds) on a phase change or the periodic tick, so many rooms don't fire in the same instant (default 15s, 0 disables) |
| Motion On / Motion Off / Background Transition | no | Transition durations for each trigger type (see [Transition durations](#transition-durations)) |
