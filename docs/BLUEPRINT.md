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

The [dashboard curve card](../README.md#previewing-the-dashboard-card) also plots today's actual sunrise/sunset
(from `sun.sun`) against the schedule, so it's easy to see at a glance how far the configured boundaries and
Evening's earliest/latest clamp are actually tracking the sun.

## Motion-driven on/off

Turns a room on when motion starts and off `no_motion_wait` seconds after it stops. A motion/occupancy sensor is
optional — without one, the blueprint still keeps already-on lights updated with the adaptive curve, it just
won't turn anything on by itself.

## Manual override detection

A light changed directly — wall switch, app, voice assistant — is left alone rather than being overwritten on
the next adaptive tick. Detected via `context.user_id`: a real person's action through the UI always carries a
user id, while automations and a device regaining power after an outage don't. The latter case is not treated as
an override, so a bulb reconnecting after a power or Zigbee blip is brought back in line automatically rather
than left stuck at its last known state.

## Scene handoff

An optional template returns the entity_id of a scene to activate instead of the adaptive curve — for example,
`scene.kitchen_night` when a day-phase sensor reads `Night`. The template is written directly by whoever sets up
the room, so the mapping is explicit rather than guessed from a naming convention. A scene only qualifies if
every entity it touches is within the blueprint's own scope (the controlled lights, plus sibling entities on the
same device, such as a light strip's effect selector); a scene reaching outside that scope, or one that doesn't
exist (a typo, a renamed scene), is treated the same as the template returning nothing.

## Per-light brightness scaling

An optional template maps `entity_id` to a brightness multiplier:

| Value | Effect |
|---|---|
| a number | scales that light's brightness, floored at 1 |
| `0` | turns the light off during the adaptive step |
| `null` / `false` | skips the light entirely on power-on (for another automation or a fixed scene to own), but still includes it when the room turns off |

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

Prefer RGB Color (off by default) sends RGB colour instead of colour temperature to lights that support it -
auto-detected per light, nothing to configure per bulb. Some bulbs render colour more accurately in RGB mode than
colour-temperature mode, which is the main reason to turn it on. Lights without RGB support are unaffected
either way.

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
| Light | yes | Entities, a device, or an area to control |
| Adaptive Lighting Sensor | yes | Sensor providing brightness/colour temperature |
| Motion Sensor | no | Enables motion-driven on/off |
| Additional Triggers | no | Entities that trigger immediate re-evaluation (see [Additional triggers](#additional-triggers)) |
| Scene Template | no | Template returning a scene entity_id to hand the room over to |
| Brightness Multiplier Template | no | Per-light brightness scaling |
| Prefer RGB Color | no | Send RGB colour instead of colour temperature to lights that support it (see [RGB colour](#rgb-colour)) |
| Wait time | no | Seconds to keep lights on after motion stops (default 120) |
| Reconcile Interval | no | Self-healing check interval (default every 5 minutes) |
| Motion On / Motion Off / Adaptive Transition | no | Transition durations for each trigger type |

Note: if migrating from an older, pre-rewrite version of this blueprint, the inputs have changed
(`scene_sensor`/`scene_name_prefix` → `scene_template`/`extra_triggers`) — every room automation using the old
inputs will show as misconfigured until updated. Worth doing deliberately, room by room, rather than all at once.
