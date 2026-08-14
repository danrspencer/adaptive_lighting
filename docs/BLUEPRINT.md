# The Adaptive Lighting blueprint — feature reference

> Part of [Adaptive Lighting](../README.md) — see there for why this project is shaped the way it is
> (in particular, [why the schedule has four named phases](../README.md#why-four-phases-not-a-continuous-curve)
> rather than a single continuous curve) and how to install it.

Built on the [Adaptive Lighting Helpers](HELPERS.md) services, but the two are only loosely coupled — the
blueprint just calls `apply_lighting` the same way it calls `light.turn_on`, and doesn't otherwise assume
anything about how that service is implemented.

## Brightness & colour temperature schedule

Tracks a target brightness and Kelvin value that follows the [Morning/Day/Evening/Night schedule](../README.md#why-four-phases-not-a-continuous-curve),
applied roughly once a minute while the room is occupied, so lights drift with the schedule instead of jumping.
`apply_lighting` itself isn't limited to this integration's own sensor - any entity following the
[attribute contract in docs/HELPERS.md](HELPERS.md#bring-your-own-sensor) works, and that's still true here. The
Adaptive Lighting Sensor input's own picker, though, is filtered to just this integration's sensors (so you don't
have to hunt through every sensor in the house to find the right one) - a hand-written "bring your own" sensor
won't show up in that dropdown. It still works if you point at it via the automation's **Edit in YAML** view
instead of the picker.

The [dashboard curve card](../README.md#previewing-the-dashboard-card) also plots today's actual sunrise/sunset
(from `sun.sun`) against the schedule, so it's easy to see at a glance how far the configured boundaries and
Evening's earliest/latest clamp are actually tracking the sun.

## One target, two jobs

The Room input is a single entity/device/area/floor/label target that does double duty: the light entities within
it are what gets controlled, and any occupancy-class `binary_sensor` entities within it govern occupancy — no
separate sensor input to fill in. Pointing Room at a room's area picks up every light *and* every occupancy sensor
in that area automatically, including ones added later; picking specific entities instead lets you mix and match
(e.g. your lights plus one specific sensor from elsewhere, or a "virtual occupancy" template sensor — see below).

Occupancy detection uses Home Assistant's built-in Occupancy triggers/conditions, which only count `binary_sensor`
entities with `device_class: occupancy` — motion-class sensors aren't picked up this way, so a room with only
motion sensors won't have anything to trigger on here (Additional Triggers can still cover that case manually).
Area/device selections only resolve *light* entities via entity/device/area (not floor/label) — a floor or label
selection works for occupancy but won't control any lights, so pick specific entities directly if you need one of
those to also light a room.

## Occupancy-driven on/off

Turns a room on when occupancy is detected and off `no_motion_wait` seconds after it clears. Occupancy is entirely
optional — a Room with no occupancy-class sensor in it still keeps already-on lights updated with the adaptive
curve, it just won't turn anything on by itself.

A room with no real occupancy sensor at all (or one you want to override manually — e.g. a nightlight mode) can
use a template `binary_sensor` with `device_class: occupancy` as a stand-in - Home Assistant's occupancy machinery
can't tell it apart from a real sensor. Pick it directly as an entity in Room (rather than relying on area
membership) so it's a deliberate addition, not something automatically swept in.

## Override detection

A light changed by anything other than this integration's own last write — a wall switch, an app, a voice
assistant, or another automation entirely (including one with no identifiable "user" of its own, such as one
triggered directly by a physical button) — is left alone rather than being overwritten on the next adaptive tick.
Detected by comparing the light's current `context.id` against the `context.id` [Adaptive Lighting
Helpers](HELPERS.md) itself last wrote that light with: if they still match, nothing has touched it since our own
last update and it's updated normally; if they don't, something else has, and it's left alone. A device simply
regaining power after an outage gets a fresh context of its own too, so it's covered by the same mechanism as
everything else — not treated as an override, so a bulb reconnecting after a power or Zigbee blip is brought back
in line automatically rather than left stuck at its last known state. A light with no recorded write at all yet
(brand new, or right after this integration's own restart) is treated the same way — free to manage — rather than
getting stuck unmanaged until it happens to change some other way.

## Scene handoff

Two ways to hand a room over to a scene instead of the adaptive curve, usable together:

- **Per-phase scene pickers** - four optional entity pickers, one per phase (e.g. pick `scene.kitchen_night` for
  Night). The simple, explicit case: no template to write.
- **Scene Template** - an optional template returning the entity_id of a scene to activate, for cases a phase
  alone can't express - for example, a different scene while the TV is on, regardless of what phase it is. Written
  directly by whoever sets up the room, so the mapping is explicit rather than guessed from a naming convention.

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
  | a number | scales that light's brightness, floored at 1 |
  | `0` | turns the light off during the adaptive step |
  | `null` / `false` | skips the light entirely on power-on (for another automation or a fixed scene to own), but still includes it when the room turns off |

**The template's own per-entity values always win over the phase lists** on any light both mention - the lists
only fill in lights the template doesn't already cover. This is additive, not a replacement: a room whose
template already fully covers its own dimming logic doesn't need the phase lists at all, and adding one only
affects lights the template leaves untouched.

## Additional triggers

Both templates above are re-rendered fresh on every run, regardless of what triggered it — so an entity that
one of them depends on (a TV, for a brightness multiplier that dims the room while it's on; whatever a scene
template checks) can be added to Additional Triggers to take effect immediately, rather than waiting for the
next adaptive tick.

## Two-step transitions

Bulbs that can't transition brightness and colour temperature together (some IKEA TRÅDFRI models) can be tagged
with a `no_combined_transition` label and are sent as two sequential half-length transitions instead of one.
Everything else gets a single combined call. Entirely handled inside `apply_lighting` (see
[docs/HELPERS.md](HELPERS.md)) — the blueprint itself has no branching for this, it's just a label you add to a
light or device.

## RGB colour

Prefer RGB During is a multi-select - pick which phases send RGB colour instead of colour temperature to lights
that support it (auto-detected per light, nothing to configure per bulb). Some bulbs render colour more
accurately in RGB mode than colour-temperature mode, which is the main reason to turn it on. Lights without RGB
support are unaffected either way.

Defaults to Evening and Night selected, Morning and Day not - colour reads as "relaxed/night" better than a
plain warm white does, but isn't worth it for the rest of the day. Select all four (or none) if you want it
on/off unconditionally.

## Reachability and redundancy filtering

Lights reported `unavailable` or `unknown` are skipped. Lights already within tolerance of the target
brightness/colour-temperature (±2 brightness, ±10K, to absorb rounding differences some bulbs report back) are
left alone rather than recommanded on every tick.

## Self-healing

On a configurable interval, if the room is unoccupied but a light is still on, the off command is retried. This
recovers from dropped commands (a missed Zigbee message, for example) without manual intervention.

## Configuration

Add an automation using the "Adaptive Lighting" blueprint per room, and set:

| Input | Required | Description |
|---|---|---|
| Adaptive Lighting Sensor | yes | Sensor providing brightness/colour temperature - filtered to this integration's own sensors (see [Brightness & colour temperature schedule](#brightness--colour-temperature-schedule) for the "bring your own sensor" case) |
| Room | no | Entity/device/area/floor/label - lights within it are controlled, occupancy sensors within it govern on/off (see [One target, two jobs](#one-target-two-jobs)) |
| Additional Triggers | no | Entities that trigger immediate re-evaluation (see [Additional triggers](#additional-triggers)) |
| **Colour** section | | |
| Prefer RGB During | no | Phases to send RGB colour instead of colour temperature to lights that support it - defaults to Evening/Night selected (see [RGB colour](#rgb-colour)) |
| **Scene Handoff** section | | |
| Scene Template | no | Template returning a scene entity_id to hand the room over to - wins over the phase pickers below when it returns one |
| Morning / Day / Evening / Night Scene | no | Per-phase scene to hand the room over to - the fallback for phases Scene Template doesn't cover (see [Scene handoff](#scene-handoff)) |
| **Brightness Scaling** section | | |
| Brightness Multiplier Template | no | Per-light brightness scaling - its own per-entity values win over the phase lists below |
| Lights Off During Morning / Day / Evening / Night | no | Lights to turn off during that phase - fills in whatever Brightness Multiplier Template doesn't already cover (see [Per-light brightness scaling](#per-light-brightness-scaling)) |
| **Timing** section | | |
| Wait time | no | Seconds to keep lights on after motion stops (default 120) |
| Reconcile Interval | no | Self-healing check interval (default every 5 minutes) |
| Motion On / Motion Off / Adaptive Transition | no | Transition durations for each trigger type |

Note: if migrating from an older, pre-rewrite version of this blueprint, the inputs have changed
(`scene_sensor`/`scene_name_prefix` → `scene_template`/`extra_triggers`) — every room automation using the old
inputs will show as misconfigured until updated. Worth doing deliberately, room by room, rather than all at once.

Note: `prefer_rgb_color_template` (a template) was replaced by **Prefer RGB During** (a multi-select of phases) -
same breaking-rename situation as `scene_sensor`/`scene_name_prefix` above, every room automation still using the
old `prefer_rgb_color_template` input will show as misconfigured until updated. There's no template fallback for
this one any more - select all four phases if you want RGB unconditionally, the same as the old template set to
`{{ true }}`.
