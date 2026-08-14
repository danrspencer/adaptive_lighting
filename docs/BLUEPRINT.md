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
The Adaptive Lighting Sensor input isn't limited to this integration's own sensor - any entity following the
[attribute contract in docs/HELPERS.md](HELPERS.md#bring-your-own-sensor) works.

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

Prefer RGB Color sends RGB colour instead of colour temperature to lights that support it - auto-detected per
light, nothing to configure per bulb. Some bulbs render colour more accurately in RGB mode than colour-temperature
mode, which is the main reason to turn it on. Lights without RGB support are unaffected either way.

It's a template, not a fixed on/off - the default is `{{ states(adaptive_sensor) in ['Evening', 'Night'] }}`, so
RGB is used automatically for Evening/Night (colour reads as "relaxed/night" better than a plain warm white) and
skipped for Morning/Day (colour temperature is closer to neutral daylight, which fits those phases better).
Override with your own template if you want it on/off unconditionally, or keyed off something else entirely -
`adaptive_sensor` is in scope, so `{{ true }}`/`{{ false }}` or any other condition works the same as any other
template input here.

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
| Adaptive Lighting Sensor | yes | Sensor providing brightness/colour temperature |
| Room | no | Entity/device/area/floor/label - lights within it are controlled, occupancy sensors within it govern on/off (see [One target, two jobs](#one-target-two-jobs)) |
| Additional Triggers | no | Entities that trigger immediate re-evaluation (see [Additional triggers](#additional-triggers)) |
| Scene Template | no | Template returning a scene entity_id to hand the room over to |
| Brightness Multiplier Template | no | Per-light brightness scaling |
| Prefer RGB Color | no | Template for whether to send RGB colour instead of colour temperature to lights that support it - defaults to on for Evening/Night, off for Morning/Day (see [RGB colour](#rgb-colour)) |
| Wait time | no | Seconds to keep lights on after motion stops (default 120) |
| Reconcile Interval | no | Self-healing check interval (default every 5 minutes) |
| Motion On / Motion Off / Adaptive Transition | no | Transition durations for each trigger type |

Note: if migrating from an older, pre-rewrite version of this blueprint, the inputs have changed
(`scene_sensor`/`scene_name_prefix` → `scene_template`/`extra_triggers`) — every room automation using the old
inputs will show as misconfigured until updated. Worth doing deliberately, room by room, rather than all at once.

Note: `prefer_rgb_color` (a fixed on/off toggle) was renamed `prefer_rgb_color_template` (a template) so it could
default per phase instead of a single fixed value - a room automation still setting the old `prefer_rgb_color: true`
override keeps behaving exactly as before (always on), just without the new phase-based default. Clear that input
(leave it blank) to pick up the new default, or replace it with your own template.
