# adaptive_lighting

A room-lighting automation: brightness/colour-temperature follow a solar
schedule, motion turns things on and off, a scene can take over
entirely, and it self-heals when a command doesn't land. Originally one
increasingly unreadable blueprint; now a blueprint plus pyscript, split
so the gnarly parts are plain, unit-tested Python and the rest stays as
native Home Assistant automation (triggers/conditions/actions), where
it's genuinely a better fit.

## Layout

```
blueprints/automation/danspencer/adaptive_lighting_unified.yaml
    The automation blueprint. Owns triggers, conditions, target
    resolution, occupancy/scene detection, and the action structure
    (which service to call, with what target) - the parts that are
    either genuinely simple or benefit from HA's native trace/debug UI.

pyscript/modules/adaptive_lighting/
    Pure Python, no HA dependency. curve.py is the brightness/colour
    schedule math; grouping.py decides what needs commanding (which
    lights are reachable, bucketed by brightness multiplier, filtered
    to only what's not already at target, split by whether a bulb can
    take a combined brightness+colour transition). Both are exactly
    what used to be Jinja - same behaviour, real data structures now.

pyscript/apps/adaptive_lighting/
    The thin pyscript service wrapper around the modules above - the
    only part of this repo that actually touches Home Assistant state.

www/adaptive-lighting-curve-card.js
    Custom Lovelace card rendering the day's brightness/colour curve as
    an actual rendered-color chart. Reads sensor.adaptive_lighting_curve
    (produced by the pyscript app) and a couple of live sensors; doesn't
    compute anything itself.

dashboard/house-settings-card.yaml
    The card config to paste into a dashboard view, plus what it
    depends on. Storage-mode dashboards aren't files, so this isn't
    auto-deployed - see the comment in that file.

tests/
    pytest, covering curve.py and grouping.py. Zero HA/pyscript
    dependency - `pip install -e .[dev] && pytest`.
```

## Deploying

On the Home Assistant host (not via any network share - see below for
why): clone this repo somewhere under `/config`, then symlink:

```
/config/blueprints/automation/danspencer/adaptive_lighting_unified.yaml
    -> <checkout>/blueprints/automation/danspencer/adaptive_lighting_unified.yaml
/config/pyscript/modules/adaptive_lighting
    -> <checkout>/pyscript/modules/adaptive_lighting
/config/pyscript/apps/adaptive_lighting
    -> <checkout>/pyscript/apps/adaptive_lighting
/config/www/adaptive-lighting-curve-card.js
    -> <checkout>/www/adaptive-lighting-curve-card.js
```

Symlinks are created and resolved on the HA host itself, deliberately -
if this repo is checked out on a different machine (e.g. edited over a
Samba share) and symlinked from there, the link targets would be paths
meaningful only on that other machine, and would be dangling as far as
Home Assistant's own filesystem access is concerned.

Register `www/adaptive-lighting-curve-card.js` as a dashboard resource
(`/local/adaptive-lighting-curve-card.js`, JavaScript Module) and add
the card from `dashboard/house-settings-card.yaml` to a view, if wanted.

## Status

Mid-migration from an all-Jinja blueprint. `pyscript/apps/` hasn't been
validated against a real pyscript install yet - see its module
docstring for what to confirm first.
