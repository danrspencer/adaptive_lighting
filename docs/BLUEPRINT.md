# The Adaptive Lighting blueprint — feature reference

> Part of [Adaptive Lighting](../README.md) — see there for why this project is shaped the way it is
> (in particular, [why the schedule has four named phases](../README.md#why-four-phases-not-a-continuous-curve)
> rather than a single continuous curve) and how to install it.

Built on the [Adaptive Lighting Helpers](HELPERS.md) services, but the two are only loosely coupled — the
blueprint just calls `apply_lighting` the same way it calls `light.turn_on`, and doesn't otherwise assume
anything about how that service is implemented.

## Brightness & colour temperature schedule

Tracks a target brightness and Kelvin value that follows the [Morning/Day/Evening/Night schedule](../README.md#why-four-phases-not-a-continuous-curve),
applied once a minute to whichever of the room's lights are already on, so they drift with the schedule
instead of jumping - regardless of whether the room currently reads as occupied (see
[Occupancy-driven on/off](#occupancy-driven-onoff) - occupancy decides whether to turn lights on or off, never
whether an already-on light keeps tracking the curve). The blueprint reads `brightness`/`color_temp`/`rgb_color`
straight off the Adaptive Lighting Sensor's own attributes and passes them to `apply_lighting` as plain values -
`apply_lighting` itself doesn't read any sensor entity at all (see [docs/HELPERS.md](HELPERS.md) for its full
field contract), so the Adaptive Lighting Sensor input isn't limited to this integration's own sensor - see
["Bring your own sensor"](#bring-your-own-sensor) below.

### Bring your own sensor

The Adaptive Lighting Sensor input can point at any entity exposing this attribute shape, not just this
integration's own named schedule sensors:

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
        state: "{{ 'Evening' if now().hour >= 18 else 'Day' }}" # anything - the blueprint only reads the phase off this integration's own sensor for phase-name-keyed inputs (rgb_phases, phase scenes/exclusions) - a custom sensor doesn't need a matching state to work for brightness/colour
        attributes:
          brightness: "{{ 180 if now().hour >= 18 else 255 }}"
          color_temp: "{{ 3200 if now().hour >= 18 else 5500 }}"
          # Optional - only needed for prefer_rgb_color
          rgb_color: "{{ [255, 200, 150] if now().hour >= 18 else [255, 255, 255] }}"
```

The Adaptive Lighting Sensor input's own picker is filtered to just this integration's sensors (so you don't have
to hunt through every sensor in the house to find the right one) - a hand-written "bring your own" sensor won't
show up in that dropdown. It still works if you point at it via the automation's **Edit in YAML** view instead of
the picker.

That cadence comes from two triggers, not one, with two different jobs. A plain time pattern - Update Interval
below (default every minute) - is the one actually doing the routine work: it reapplies the schedule on a fixed
interval regardless of whether anything changed, which is what makes "the next tick will correct it" true at
every hour of the day, including Morning and Night's *flat* stretches (constant brightness and Kelvin for the
whole phase, where nothing would otherwise trigger an update at all). The sensor's own state trigger only fires
on an actual phase change (Morning→Day and so on) — not on the attribute-only ticks in between, which the
periodic tick already covers — so a room isn't left waiting up to a full tick interval to notice it just entered
a new phase. This same periodic tick also drives the [Self-healing](#self-healing) check below - there's no
separate interval for that.

Both of these, plus a genuine phase change, are also where Update Jitter applies: a random delay (default
up to 15 seconds) so that many rooms sharing one Adaptive Lighting Sensor don't all send commands in the same
wall-clock second — most noticeable right at a phase boundary, when every such room would otherwise fire at
literally the same instant. Motion, manual runs, and Additional Triggers are never delayed - self-healing is,
since it shares the same tick.

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
motion sensors won't have anything to trigger on here. To drive a room from a motion-class sensor, have a
separate automation of your own watch it and call this one (see [Additional triggers](#additional-triggers)).
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

Only three things may bring an off light on: motion actually being detected, running the automation manually, or
the room already being in active use (at least one of its *other* lights is already on). Every other trigger that
updates a room's lighting — the periodic adaptive tick, an Additional Trigger firing, or a light recovering from a
dropped connection (see [Override detection](#override-detection) below) — may only ever update lights that are
already on; it never switches a dark room's light on by itself.

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
Detected by comparing the light's current `context.id` against the `context.id`(s) [Adaptive Lighting
Helpers](HELPERS.md) itself last wrote that light with: if either still matches, nothing has touched it since our
own last update and it's updated normally; if neither does, something else has, and it's left alone. A light with
no recorded write at all yet (brand new, or right after this integration's own restart) is treated the same way —
free to manage — rather than getting stuck unmanaged until it happens to change some other way. [Adaptive Lighting
Helpers](HELPERS.md#override-protection) covers the full mechanism, including how a single write that silently
fails to land self-heals on the next tick instead of locking the light out permanently.

The blueprint declares no ownership of its own. Which **state device** tracks a light is resolved by the
integration from its own configuration — by area, or by a target you point at devices or specific lights — so
there's no blueprint input for this and none needed. Two automations driving the same room therefore share
that room's claims and co-operate, rather than each treating the other's write as external; give them separate
state devices if you want them tracked apart. See
[docs/HELPERS.md](HELPERS.md#override-protection) for the resolution rules.

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

The blueprint's only remaining role here is promptness: a dedicated `recovered` trigger fires when the room comes
back from being entirely unreachable (a Zigbee mesh drop, or someone physically cutting and restoring power to
the room), causing an ordinary tick to run right away rather than waiting for the room's next unrelated trigger.

Precisely, it arms while *every* light in the room is `unavailable`/`unknown` and fires as the first one returns.
It deliberately does **not** ask "is nothing unavailable", which sounds equivalent but isn't: a single orphaned
entity that is permanently unavailable — a deleted Zigbee group whose entity was never cleaned up, for instance —
would hold that condition false forever and silently disable recovery for the entire room. Asking whether
*anything* is reachable makes a dead entity just one more member of the dark set rather than a permanent veto.

The trade-off is that one flaky bulb dropping and returning while its siblings stay up doesn't move the aggregate,
so `recovered` won't fire for it. That case is left to the periodic tick described in
[Brightness & colour temperature schedule](#brightness--colour-temperature-schedule), which runs regardless. That tick treats the recovered light exactly like any other light on any other trigger - subject to
[the "when a light is allowed to turn on" rule](#when-a-light-is-allowed-to-turn-on) above like everything else:
a light that reconnects already on gets brought to the adaptive target, one that reconnects *off*, in a room with
nothing else currently on, is left off rather than switched on.

If you want to deliberately force a light back under adaptive control from your own script without turning it
off first, call `apply_lighting` directly with `force: true` - see
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

Bulbs that can't transition brightness and colour temperature together (some IKEA TRÅDFRI models) can be tagged
with a `no_combined_transition` label and are sent as two sequential half-length transitions instead of one.
Everything else gets a single combined call. Entirely handled inside `apply_lighting` (see
[docs/HELPERS.md](HELPERS.md)) — the blueprint itself has no branching for this, it's just a label you add to a
light or device.

The label can go on either the **entity** or its **device** — device is better, since it survives entity renames
and covers every light entity that device exposes. What matters is the label's *id* (`no_combined_transition`),
not its display name; the lookup is an exact match, so a label whose id doesn't line up silently does nothing at
all — no error, no log, just a bulb quietly back on combined transitions.

Because that failure is invisible, the integration watches for it: if a bulb whose model is known to need
two-step transitions isn't labelled, it raises a **repair** with a Fix button that applies the label for you
(creating it correctly if it doesn't exist). The list of known models ships with the integration and can be
extended per-install — see [docs/HELPERS.md](HELPERS.md#two-step-transition-bulbs) for the model patterns and
how to add one.

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
| Background Transition | The periodic Update tick, Additional Triggers, and a light recovering from a dropped connection (see [Override detection](#override-detection)) - none of these are a person waiting on a response in real time, so there's no reason to snap. Covers both the scene-activation step and the main adaptive-lighting dispatch |
| Motion On Transition | Motion being detected, and running the automation manually - "something happened, respond promptly" triggers |
| Motion Off Transition | Turning lights off - both the motion-cleared turn-off and the [self-healing](#self-healing) retry |

## Reachability and redundancy filtering

Lights reported `unavailable` or `unknown` are skipped. Lights already within tolerance of the target
brightness/colour-temperature (±2 brightness, ±10K, to absorb rounding differences some bulbs report back) are
left alone rather than recommanded on every tick.

## Self-healing

On every Update tick (the same periodic tick that drives ordinary brightness/colour tracking — see
[Brightness & colour temperature schedule](#brightness--colour-temperature-schedule) — there's no separate
interval for this), if the room's occupancy sensors have been continuously clear for the full Wait time but a
light is still on, the off command is retried instead of the normal reapply. This recovers from dropped commands
(a missed Zigbee message, for example) without manual intervention. There used to be a second, independent
interval just for this check — merged away once it became clear there was no real reason for two separate
periodic ticks per room.

The Wait time check here is debounced against a momentary sensor blip, not just an instantaneous "is it clear
right now" read — a noisy occupancy sensor that briefly reports clear before going occupied again won't trip an
early turn-off just because a tick happens to land in that gap.

Lights handed off via a `null` multiplier (see
[Per-light brightness scaling](#per-light-brightness-scaling)) are excluded from this retry, and don't count as
"still on" for the purpose of triggering it — so a room whose only lit light is one this automation doesn't own
is treated as already settled, rather than retrying an off command against it every interval.

## Configuration

Add an automation using the "Adaptive Lighting" blueprint per room, and set:

| Input | Required | Description |
|---|---|---|
| Adaptive Lighting Sensor | yes | Sensor providing brightness/colour temperature - filtered to this integration's own sensors (see [Bring your own sensor](#bring-your-own-sensor) for pointing at a different one) |
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

Note: if migrating from an older, pre-rewrite version of this blueprint, the inputs have changed
(`scene_sensor`/`scene_name_prefix` → `scene_template`/`extra_triggers`) — every room automation using the old
inputs will show as misconfigured until updated. Worth doing deliberately, room by room, rather than all at once.

Note: `prefer_rgb_color_template` (a template) was replaced by **Prefer RGB During** (a multi-select of phases) -
same breaking-rename situation as `scene_sensor`/`scene_name_prefix` above, every room automation still using the
old `prefer_rgb_color_template` input will show as misconfigured until updated. There's no template fallback for
this one any more - select all four phases if you want RGB unconditionally, the same as the old template set to
`{{ true }}`.
