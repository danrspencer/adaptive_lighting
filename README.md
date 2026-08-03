# Adaptive Lighting

A Home Assistant blueprint (with a [pyscript](https://github.com/custom-components/pyscript) backend) for
per-room lighting: brightness and colour temperature follow a solar schedule, motion controls on/off, scenes can
take over partially or entirely, manual changes are respected, and lights that don't reach their target get corrected
automatically.

![Adaptive Lighting Curve card, showing brightness and colour temperature through the day](dashboard/curve-preview.svg)

## Features

### Solar-driven brightness & colour temperature

Tracks a target brightness and Kelvin value that changes through the day — full brightness and cool white during
the day, warming and dimming through the evening, dim and warm at night — following a configurable
morning/day/evening/night schedule (evening tracks sunset, clamped between an earliest and latest bound). Applied
roughly once a minute while the room is occupied, so lights drift with the schedule instead of jumping.

The [dashboard curve card](#previewing-the-dashboard-card) also plots today's actual sunrise/sunset (from
`sun.sun`) against the schedule, so it's easy to see at a glance how far the configured boundaries and earliest/
latest clamps are actually tracking the sun.

### Motion-driven on/off

Turns a room on when motion starts and off `no_motion_wait` seconds after it stops. A motion/occupancy sensor is
optional — without one, the blueprint still keeps already-on lights updated with the adaptive curve, it just
won't turn anything on by itself.

### Manual override detection

A light changed directly — wall switch, app, voice assistant — is left alone rather than being overwritten on
the next adaptive tick. Detected via `context.user_id`: a real person's action through the UI always carries a
user id, while automations and a device regaining power after an outage don't. The latter case is not treated as
an override, so a bulb reconnecting after a power or Zigbee blip is brought back in line automatically rather
than left stuck at its last known state.

### Scene handoff

An optional template returns the entity_id of a scene to activate instead of the adaptive curve — for example,
`scene.kitchen_night` when a day-phase sensor reads `Night`. The template is written directly by whoever sets up
the room, so the mapping is explicit rather than guessed from a naming convention. A scene only qualifies if
every entity it touches is within the blueprint's own scope (the controlled lights, plus sibling entities on the
same device, such as a light strip's effect selector); a scene reaching outside that scope, or one that doesn't
exist (a typo, a renamed scene), is treated the same as the template returning nothing.

### Per-light brightness scaling

An optional template maps `entity_id` to a brightness multiplier:

| Value | Effect |
|---|---|
| a number | scales that light's brightness, floored at 1 |
| `0` | turns the light off during the adaptive step |
| `null` / `false` | skips the light entirely on power-on (for another automation or a fixed scene to own), but still includes it when the room turns off |

### Additional triggers

Both templates above are re-rendered fresh on every run, regardless of what triggered it — so an entity that
one of them depends on (a TV, for a brightness multiplier that dims the room while it's on; whatever a scene
template checks) can be added to Additional Triggers to take effect immediately, rather than waiting for the
next adaptive tick.

### Two-step transitions

Bulbs that can't transition brightness and colour temperature together (some IKEA TRÅDFRI models) can be tagged
with a `no_combined_transition` label and are sent as two sequential half-length transitions instead of one.
Everything else gets a single combined call.

### Reachability and redundancy filtering

Lights reported `unavailable` or `unknown` are skipped. Lights already within tolerance of the target
brightness/colour-temperature (±2 brightness, ±10K, to absorb rounding differences some bulbs report back) are
left alone rather than recommanded on every tick.

### Self-healing

On a configurable interval, if the room is unoccupied but a light is still on, the off command is retried. This
recovers from dropped commands (a missed Zigbee message, for example) without manual intervention.

## Repository layout

```
blueprints/automation/danspencer/adaptive_lighting.yaml
    The automation blueprint: triggers, conditions, target resolution,
    and the action sequence (which service to call, with what target).
    Deliberately named differently from any prior "Adaptive Lighting
    Unified" blueprint so the two can run side by side while rooms are
    migrated over individually, rather than one replacing the other
    in place.

pyscript/modules/adaptive_lighting/
    curve.py     brightness/colour-temperature schedule
    grouping.py  reachability, multiplier bucketing, tolerance checks,
                 and two-step/combined transition routing
    Pure Python, no Home Assistant dependency.

pyscript/apps/adaptive_lighting/
    Pyscript service wrapper exposing the modules above to Home
    Assistant state.

www/adaptive-lighting-curve-card.js
    Custom Lovelace card rendering the day's curve as a rendered-colour
    chart, with a live "now" marker.

packages/adaptive_lighting_sync.yaml
    shell_command + automation that polls this repo for new commits
    and redeploys automatically. See "Staying up to date automatically"
    below.

dashboard/
    house-settings-card.yaml   card config to add to a view
    preview.html                renders the real card against synthetic
                                 data, no Home Assistant instance needed
    generate_preview_data.py    generates that synthetic data
    render_preview_svg.py       renders the screenshot above as a
                                 standalone SVG

tests/
    pytest suite for curve.py and grouping.py.
```

Triggers, conditions, and target resolution stay in the blueprint; Home Assistant `condition:` blocks can't call
a service, so anything a condition depends on has to remain template-based. Multiplier bucketing, tolerance
checks, and transition routing are implemented in `pyscript/modules` and unit tested. See `CLAUDE.md` for
further implementation notes.

## Installation

On the Home Assistant host itself (not via a network share — see `CLAUDE.md` for why), clone this repository
under `/config` (e.g. via the Advanced SSH & Web Terminal add-on) and run:

```bash
./scripts/link_into_ha.sh          # symlinks everything into /config
./scripts/link_into_ha.sh --dry-run   # preview first, if you'd rather
```

This links the blueprint, both `pyscript/` directories, and the dashboard card into place; backs up anything
already at those paths (renamed with a `.bak-<timestamp>` suffix) rather than overwriting it; and is safe to
re-run. Pass a directory as an argument to target something other than `/config`.

Note: the blueprint's inputs have changed (`scene_sensor`/`scene_name_prefix` → `scene_template`/
`extra_triggers`) — every room automation using the old inputs will show as misconfigured once this is linked
in, until updated. Worth doing deliberately, room by room, rather than all at once.

For the dashboard card, register `www/adaptive-lighting-curve-card.js` as a Lovelace resource (Settings →
Dashboards → Resources → Add Resource, URL `/local/adaptive-lighting-curve-card.js`, type JavaScript Module) and
add the card config from `dashboard/house-settings-card.yaml` to a view.

### Staying up to date automatically

`packages/adaptive_lighting_sync.yaml` (linked in by the step above, since `packages/` is part of `/config`)
adds a `shell_command` and a `time_pattern` automation that runs `git pull` in the repo every 15 minutes and
re-runs `link_into_ha.sh` if anything changed — so a `git push` to this repo shows up in Home Assistant on its
own, no manual re-run needed after the first install. It polls rather than reacting to a webhook, so there's no
need to expose Home Assistant to the internet.

Two things this can't do for itself:
- **Bootstrapping**: the automation can't deploy itself before it exists, so the first `link_into_ha.sh` run has
  to be manual (the step above).
- **New `shell_command` entries need a full HA restart**, not just a config/automation reload, before the
  sync automation can actually run.

`shell_command` runs inside whichever container hosts Home Assistant Core, which may not have `git` installed
depending on your setup — if the sync automation's traces show a failure, check `ha_get_logs` for the actual
error before assuming something else is wrong.

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
| Wait time | no | Seconds to keep lights on after motion stops (default 120) |
| Reconcile Interval | no | Self-healing check interval (default every 5 minutes) |
| Motion On / Motion Off / Adaptive Transition | no | Transition durations for each trigger type |

## Previewing the dashboard card

```bash
python3 dashboard/generate_preview_data.py
python3 -m http.server 8934
# open http://localhost:8934/dashboard/preview.html
```

Renders the actual card component against generated data, without a Home Assistant instance.

## Testing

```bash
pip install pytest
pytest
```

No Home Assistant or pyscript dependency; `tests/fakes.py` provides a fake state/registry lookup. CI
(`.github/workflows/tests.yml`) runs the suite on push and PR across Python 3.9 and 3.13.

## Status

The pure-Python core (`curve.py`, `grouping.py`) is complete and tested. The pyscript service wrapper
(`pyscript/apps/`) has not yet been validated against a live pyscript install, and the blueprint has not yet been
updated to call it — see `CLAUDE.md` for details.
