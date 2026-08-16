# The Adaptive Lighting blueprint — feature reference

> Part of [Adaptive Lighting](../README.md) — see there for why this project is shaped the way it is
> (in particular, [why the schedule has four named phases](../README.md#why-four-phases-not-a-continuous-curve)
> rather than a single continuous curve) and how to install it.

Built on the [Adaptive Lighting Helpers](HELPERS.md) services, but the two are only loosely coupled — the
blueprint just calls `apply_lighting` the same way it calls `light.turn_on`, and doesn't otherwise assume
anything about how that service is implemented.

## Brightness & colour temperature schedule

Tracks a target brightness and Kelvin value that follows the [Morning/Day/Evening/Night schedule](../README.md#why-four-phases-not-a-continuous-curve),
applied roughly once a minute to whichever of the room's lights are already on, so they drift with the schedule
instead of jumping - regardless of whether the room currently reads as occupied (see
[Occupancy-driven on/off](#occupancy-driven-onoff) - occupancy decides whether to turn lights on or off, never
whether an already-on light keeps tracking the curve). `apply_lighting` itself isn't limited to this integration's
own sensor - any entity following the
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
motion sensors won't have anything to trigger on here — put those in
[Additional Triggers - Turn Lights On](#additional-triggers) instead, which is the input that can actually light
the room off the back of them (the plain Additional Triggers input only updates lights already on).
Area/device selections only resolve *light* entities via entity/device/area (not floor/label) — a floor or label
selection works for occupancy but won't control any lights, so pick specific entities directly if you need one of
those to also light a room.

## Occupancy-driven on/off

Occupancy has exactly two jobs: turning a room on when it's detected, and turning it off `no_motion_wait` seconds
after it clears. That's the whole scope - it has no say over whether an already-on light keeps tracking the curve
(see [Brightness & colour temperature schedule](#brightness--colour-temperature-schedule)), only over switching
lights on or off in the first place. Occupancy is entirely optional either way - a Room with no occupancy-class
sensor in it just won't turn anything on by itself (see [When a light is allowed to turn on](#when-a-light-is-allowed-to-turn-on)).
Lights handed off via a `null` multiplier are exempt from the turn-off too — see
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

Only four things may bring an off light on: motion actually being detected, running the automation manually, an
entity in [Additional Triggers - Turn Lights On](#additional-triggers) firing, or the room already being in
active use (at least one of its *other* lights is already on). Every other trigger that updates a room's
lighting — the periodic adaptive tick, a plain Additional Trigger firing, or a light recovering from a dropped
connection (see [Override detection](#override-detection) below) — may only ever update lights that are already
on; it never switches a dark room's light on by itself.

This matters most for a light reconnecting after a Zigbee drop or a power cut: without this rule, a light that
was deliberately left off would come back on the moment it reconnects to the network, purely because it just
reconnected — not because anyone actually wanted it on. A light that reconnects off, in a room with nothing else
on, stays off.

The "room already occupied" branch is what lets a *different* off light in an already-in-use multi-light room
still switch on to match the rest — for example, a periodic tick topping up a room where one lamp's off but the
others are already lit. This looks at the whole room's state, not just the individual light being considered.

## Override detection

A light changed by anything other than this integration's own last write — a wall switch, an app, a voice
assistant, or another automation entirely (including one with no identifiable "user" of its own, such as one
triggered directly by a physical button) — is left alone rather than being overwritten on the next adaptive tick.
Detected by comparing the light's current `context.id` against the `context.id` [Adaptive Lighting
Helpers](HELPERS.md) itself last wrote that light with: if they still match, nothing has touched it since our own
last update and it's updated normally; if they don't, something else has, and it's left alone. A light with no
recorded write at all yet (brand new, or right after this integration's own restart) is treated the same way —
free to manage — rather than getting stuck unmanaged until it happens to change some other way.

The blueprint identifies itself to this check via `apply_lighting`'s `owner_id` parameter, set to its own
`this.entity_id` — so a room's automation only ever recognises its *own* previous writes as "not overridden";
even a write from a different room's automation counts as external. There's no blueprint input for this, and
none needed — it's automatic per room. `owner_id` is passed on every single call the blueprint makes, including
the manual-run case below that bypasses the check with `force: true` — force and owner_id aren't opposites;
forcing *with* an owner_id still attributes the write, so the room's next regular tick correctly recognises it
as its own rather than getting stuck treating its own forced write as external.

**Running the automation manually** (hitting "Run" in the UI, or calling `automation.trigger` directly, rather
than one of its own configured triggers firing) forces the whole tick through regardless of override
protection - the same "I ran this on purpose, take it back" intent as calling `apply_lighting` yourself with
`force: true`. If you'd rather it respect protection even on a manual run, there's currently no input for
that - open an issue if you need it.

**A device regaining power after an outage does *not* fall under the "not treated as an override" umbrella** —
its own reconnect state report gets a fresh context too, indistinguishable from a real external change. [Adaptive
Lighting Helpers](HELPERS.md) itself closes this gap directly: it clears a light's override-protection record the
moment it's *observed* going unavailable, so by the time it reconnects there's no stale record left for its new
context to conflict with - it's simply "free to manage" again, the same as a brand new light, through completely
ordinary means. No forced write, no scoped call, nothing blueprint-specific at all - a genuinely different light
in the same room, under its own real override, was never at risk either way, since only the entity that actually
went unavailable ever has its record cleared.

The blueprint's only remaining role here is promptness: a dedicated `recovered` trigger fires the moment one of
its lights recovers from `unavailable`/`unknown` (a Zigbee mesh drop, or someone physically cutting and restoring
power to the room), causing an ordinary tick to run right away rather than waiting for the room's next unrelated
trigger. That tick treats the recovered light exactly like any other light on any other trigger - subject to
[the "when a light is allowed to turn on" rule](#when-a-light-is-allowed-to-turn-on) above like everything else:
a light that reconnects already on gets brought to the adaptive target, one that reconnects *off*, in a room with
nothing else currently on, is left off rather than switched on.

If you want to deliberately force a light back under adaptive control from your own script without turning it
off first, call `apply_lighting` directly with `force: true` (with or without an `owner_id` of your own) - see
[docs/HELPERS.md](HELPERS.md#override-protection) for the full contract.

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
  | `null` / `false` | hands the light off entirely — this automation never touches it, on or off |

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

There are two of these inputs, differing in exactly one respect — whether firing is allowed to switch a light
on:

| Input | Updates lights already on | Switches off lights on |
|---|---|---|
| Additional Triggers | yes | no |
| Additional Triggers - Turn Lights On | yes | yes |

Use the plain one for a dependency of a template — it should take effect *if* the room is lit, but shouldn't
light a dark room by itself. Use the turn-on variant for something whose whole purpose is to light the room,
e.g. a helper that switches on at dusk. Everything else about the two is identical: same re-evaluation, same
[transition duration](#transition-durations), same [override protection](#override-detection).

Two things worth knowing before picking entities for the turn-on variant:

- **Pick something whose state changes once, when the thing actually happens.** The trigger fires on any state
  change of the chosen entity, *including attribute-only changes*. `sun.sun` is the classic trap: its elevation
  and azimuth attributes update every ~30 seconds all day long, so it would grant turn-on permission almost
  continuously. Its state also flips twice a day — at dusk *and* at dawn. A template `binary_sensor` or an
  `input_boolean` that goes on once at the moment you care about is the right shape.
- **It grants a one-shot permission, not a lasting "this room is in use" state.** In a room that has real
  occupancy sensors and currently reads as clear, [self-healing](#self-healing) will still switch those lights
  back off within the reconcile interval, exactly as it would after any other one-off turn-on. In a room with
  no occupancy sensor that branch never runs at all, so the lights simply stay on.

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

## Transition durations

Three separate transition times, so a room can respond quickly to someone actually walking in while still
drifting smoothly the rest of the time:

| Duration | Used for |
|---|---|
| Adaptive Transition | The periodic adaptive tick, plain Additional Triggers, and a light recovering from a dropped connection (see [Override detection](#override-detection)) - none of these are a person waiting on a response in real time, so there's no reason to snap. Covers both the scene-activation step and the main adaptive-lighting dispatch |
| Motion On Transition | Motion being detected, an [Additional Trigger - Turn Lights On](#additional-triggers) firing, and running the automation manually - "light the room now" triggers |
| Motion Off Transition | Turning lights off - both the motion-cleared turn-off and the [self-healing](#self-healing) retry |

## Reachability and redundancy filtering

Lights reported `unavailable` or `unknown` are skipped. Lights already within tolerance of the target
brightness/colour-temperature (±2 brightness, ±10K, to absorb rounding differences some bulbs report back) are
left alone rather than recommanded on every tick.

## Self-healing

On a configurable interval, if the room is unoccupied but a light is still on, the off command is retried. This
recovers from dropped commands (a missed Zigbee message, for example) without manual intervention.

Lights handed off via a `null` multiplier (see
[Per-light brightness scaling](#per-light-brightness-scaling)) are excluded from this retry, and don't count as
"still on" for the purpose of triggering it — so a room whose only lit light is one this automation doesn't own
is treated as already settled, rather than retrying an off command against it every interval.

## Configuration

Add an automation using the "Adaptive Lighting" blueprint per room, and set:

| Input | Required | Description |
|---|---|---|
| Adaptive Lighting Sensor | yes | Sensor providing brightness/colour temperature - filtered to this integration's own sensors (see [Brightness & colour temperature schedule](#brightness--colour-temperature-schedule) for the "bring your own sensor" case) |
| Room | no | Entity/device/area/floor/label - lights within it are controlled, occupancy sensors within it govern on/off (see [One target, two jobs](#one-target-two-jobs)) |
| Additional Triggers | no | Entities that trigger immediate re-evaluation, updating only lights already on (see [Additional triggers](#additional-triggers)) |
| Additional Triggers - Turn Lights On | no | Same, but also allowed to switch the room's lights on when they fire (see [Additional triggers](#additional-triggers)) |
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
| Motion On / Motion Off / Adaptive Transition | no | Transition durations for each trigger type (see [Transition durations](#transition-durations)) |

Note: if migrating from an older, pre-rewrite version of this blueprint, the inputs have changed
(`scene_sensor`/`scene_name_prefix` → `scene_template`/`extra_triggers`) — every room automation using the old
inputs will show as misconfigured until updated. Worth doing deliberately, room by room, rather than all at once.

Note: `prefer_rgb_color_template` (a template) was replaced by **Prefer RGB During** (a multi-select of phases) -
same breaking-rename situation as `scene_sensor`/`scene_name_prefix` above, every room automation still using the
old `prefer_rgb_color_template` input will show as misconfigured until updated. There's no template fallback for
this one any more - select all four phases if you want RGB unconditionally, the same as the old template set to
`{{ true }}`.
