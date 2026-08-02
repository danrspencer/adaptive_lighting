# Adaptive Lighting

A Home Assistant automation blueprint (plus a bit of [pyscript](https://github.com/custom-components/pyscript)) for
per-room lighting that:

- follows a solar brightness/colour-temperature curve through the day
- turns on with motion, off when it's been empty a while
- backs off completely the moment a human touches a light directly
- can hand a room over to a scene instead of the adaptive curve
- fixes itself when a command doesn't land, instead of staying wrong until someone notices

It started as a single blueprint that grew about 150 lines of nested Jinja for the
part that decides *what* to actually send each light. That part is now plain,
unit-tested Python; the blueprint keeps the triggers, conditions, and the
list of `light.turn_on`/`light.turn_off` calls, because that's what Home
Assistant's automation engine is actually good at.

![Adaptive Lighting Curve card, showing brightness and colour temperature through the day](dashboard/curve-preview.svg)

*Rendered by `dashboard/render_preview_svg.py` from synthetic data — the same curve math that drives the real
sensors, with no Home Assistant instance required. See [Dashboard card](#dashboard-card).*

## Features

### Solar-driven brightness & colour temperature

A sensor (you provide it — see [`pyscript/apps`](#pyscript) for the reference implementation) reports a target
brightness and Kelvin value that changes through the day: full brightness and cool/blue-white during the day,
warming and dimming through the evening, dim and warm at night, following your own morning/day/evening/night
schedule (evening itself tracks sunset, clamped between an earliest and latest time you set). The blueprint
re-applies this roughly once a minute while the room is occupied, so lights drift with the schedule instead of
jumping.

### Motion on, motion off

Give it a motion or occupancy sensor and it turns the room on when motion starts, and off `no_motion_wait`
seconds after motion stops. Without a motion sensor, it still works — it just won't turn anything on by itself,
only keep already-on lights updated with the adaptive curve.

### Manual override, respected properly

If you turn a light on or off yourself — wall switch, app, voice assistant — the automation notices and leaves
it alone rather than fighting you a minute later on the next adaptive tick. This is detected via
`context.user_id`: Home Assistant tags every state change with who or what caused it, and a real person's action
via the UI always carries a user id, while automations and a bulb simply regaining power after an outage don't.
That second case matters — a bulb reconnecting after a power blip isn't a person expressing a preference, so it's
*not* treated as an override; it just gets caught up to the correct brightness on the next tick or reconcile pass
(see below), typically within a minute or five rather than instantly, in exchange for not needing bespoke
recovery-detection logic.

### Scene handoff

Point it at a sensor or `input_select` (e.g. one that names a day-phase) and a naming prefix, and if a scene
exists matching `scene.<prefix>_<state>` — say, `scene.kitchen_night` — it's activated instead of the adaptive
curve, *but only if that scene stays within this automation's own scope*: every entity the scene touches has to
be one of the lights (or a sibling entity on the same device, like a light strip's effect selector) this
blueprint already controls. A scene that reaches outside that scope is treated as not existing, so it can never
silently hand control of something unexpected to a scene.

### Per-light brightness scaling

An optional template returns a dict of `entity_id: multiplier`, letting you scale specific lights differently
from the rest of the room:

- a number scales that light's brightness (floored at 1, so a small multiplier never accidentally turns it off)
- `0` turns it off entirely during the adaptive step
- `null` (or `false`) skips it completely on power-on — no command sent at all, so something else (another
  automation, a fixed scene) can own that light — but it's *still* included when the room turns off, since this
  blueprint remains the source of truth for that.

### Bulbs that can't combine a transition

Some bulbs (older IKEA TRÅDFRI ones, notably) can't transition brightness and colour temperature in the same
call. Tag the affected light or device with a `no_combined_transition` label and it'll get sent as two
sequential half-length transitions instead of one — brightness first, then colour. Everything else gets a single
combined call.

### Only touches what's reachable, only sends what's needed

Before sending anything, every light is checked against Home Assistant's own state: anything `unavailable` or
`unknown` is skipped outright (there's no point commanding something HA already knows it can't reach), and
anything already within tolerance of the target brightness/colour-temperature (±2 brightness, ±10K — some bulbs
round-trip these values slightly differently than what was sent) is left alone rather than recommanded on every
single tick.

### Self-healing

Every few minutes (configurable), if the room is unoccupied but something's still on, it retries turning just
that off. This is what actually matters in practice: Zigbee networks drop the occasional command, and without
this a light left on from one dropped message could stay on indefinitely.

## Architecture

```
blueprints/automation/danspencer/adaptive_lighting_unified.yaml
    The blueprint. Owns triggers, conditions, target resolution, and the
    action structure (which service to call, with what target) — native
    Home Assistant automation, kept native because it's a good fit and
    gets HA's own trace/debug tooling for free.

pyscript/modules/adaptive_lighting/
    Pure Python, zero Home Assistant dependency.
      curve.py     the brightness/colour-temperature schedule
      grouping.py  reachability, multiplier bucketing, the tolerance
                   check, and two-step-vs-combined routing — this is
                   the part that used to be ~150 lines of Jinja

pyscript/apps/adaptive_lighting/
    The thin pyscript service wrapper that gives the modules above real
    Home Assistant state. The only part of this repo that touches `hass`.

www/adaptive-lighting-curve-card.js
    A custom Lovelace card rendering the day's curve as an actual
    rendered-colour chart. Reads sensor state; computes nothing itself.

dashboard/
    The card config snippet, plus generate_preview_data.py and
    render_preview_svg.py — regenerate the screenshot above any time
    with `python3 dashboard/generate_preview_data.py && python3
    dashboard/render_preview_svg.py`. Also preview.html, which renders
    the *actual* card (not a static image) in a browser against
    synthetic data — see the comment at the top of that file.

tests/
    pytest, covering curve.py and grouping.py. No HA/pyscript
    dependency required.
```

### Why split it this way

Home Assistant `condition:` blocks can't call a service — only `action:` steps can — so anything a condition
needs to gate on has to stay as a template, not a pyscript call. That's most of what stayed in the blueprint:
target resolution, occupancy, scene-scope checking. What moved to pyscript is specifically the part that was
Jinja only because there was nowhere better to put it: bucketing lights by multiplier, checking each one's
current state against a tolerance, deciding which transition style to use. That logic benefits from being real
Python — actual lists and dataclasses instead of namespace-loop tricks, and pytest instead of "reload and see
what the trace says."

More detail, including a couple of hard-won Home Assistant/pyscript gotchas worth knowing before changing
anything here, is in `CLAUDE.md`.

## Dashboard card

`www/adaptive-lighting-curve-card.js` is a custom Lovelace card that reads the produced sensors and renders the
day's curve, with a live "now" marker. It needs:

1. Registering as a dashboard resource: Settings → Dashboards → Resources → Add Resource, URL
   `/local/adaptive-lighting-curve-card.js`, type JavaScript Module.
2. The card config from `dashboard/house-settings-card.yaml` added to a view.
3. The sensors it reads to exist with the entity ids and attribute shapes documented at the top of that file —
   produced by `pyscript/apps/adaptive_lighting` (or your own equivalent).

To see the card itself without any of that — no Home Assistant instance, no dashboard — generate synthetic data,
serve this repo over HTTP, and open `dashboard/preview.html`:

```bash
python3 dashboard/generate_preview_data.py
python3 -m http.server 8934
# then open http://localhost:8934/dashboard/preview.html
```

It loads the real card against synthetic data (`dashboard/preview_data.json`, generated by
`generate_preview_data.py` using the actual `curve.py`), so what you see is the genuine component, not a
lookalike.

## Deploying

On the Home Assistant host itself (not over a network share — see `CLAUDE.md` for why that matters), clone this
repo somewhere under `/config` and symlink:

```
/config/blueprints/automation/danspencer/adaptive_lighting_unified.yaml
    -> <checkout>/blueprints/automation/danspencer/adaptive_lighting_unified.yaml
/config/pyscript/modules/adaptive_lighting  -> <checkout>/pyscript/modules/adaptive_lighting
/config/pyscript/apps/adaptive_lighting     -> <checkout>/pyscript/apps/adaptive_lighting
/config/www/adaptive-lighting-curve-card.js -> <checkout>/www/adaptive-lighting-curve-card.js
```

Then, per room, add an automation using the blueprint and fill in: which lights (entities, a device, or an
area), your adaptive sensor, optionally a motion sensor, optionally a scene sensor/prefix, and optionally a
brightness-multiplier template.

## Testing

```bash
pip install pytest
pytest
```

No Home Assistant or pyscript dependency — `tests/fakes.py` provides a fake state/registry lookup so
`grouping.py` is exercised with plain dicts. CI (`.github/workflows/tests.yml`) runs this on every push and PR
across Python 3.9 and 3.13.

## Status

Mid-migration. The pure-Python core (`curve.py`, `grouping.py`) is done and tested. The pyscript service wrapper
(`pyscript/apps/`) is written but not yet validated against a real pyscript install, and the blueprint hasn't
been rewired to call it yet — it's still the fully-Jinja version, kept as the migration's working baseline. See
`CLAUDE.md` for exactly what's left and why.
