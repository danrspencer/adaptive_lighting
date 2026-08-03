"""
Adaptive Lighting - pyscript app.

Exposes pyscript.compute_lighting_groups, which the blueprint's action:
calls (with response_variable) to get back the multiplier groups it
needs to command, instead of computing them in ~150 lines of Jinja.
All the actual logic lives in pyscript/modules/adaptive_lighting - this
file is just the thin adapter to real Home Assistant state.

This app folder is deliberately named adaptive_lighting_app, NOT
adaptive_lighting - sharing the exact name with the module package it
imports from (pyscript/modules/adaptive_lighting) sent pyscript's
import resolution into infinite recursion (module_import -> load_file
-> module_import -> ... until RecursionError), rather than resolving
the sibling module. See CLAUDE.md lesson 9.

Must be named __init__.py, not app.py - pyscript only autoloads a
folder-based app from exactly `pyscript/apps/<name>/__init__.py`;
anything else in the folder is silently ignored (no error, no log
line). See CLAUDE.md lesson 8.

This app also needs an explicit (even empty) entry under `pyscript:
apps:` in YAML config - a folder in pyscript/apps/ existing on disk is
not enough by itself, pyscript logs `skipping ... because config not
present` and does nothing otherwise. See packages/adaptive_lighting_pyscript.yaml.

_context_user_id below is still the one unverified part of the
original Phase 0 spike - states[entity_id].context.user_id is how
Jinja gets it; there's no obviously-equivalent pyscript global, so
this guesses hass.states.get(entity_id).context.user_id instead. Not
yet exercised by a real manual-override scenario.
"""

from adaptive_lighting import EntityLookup, build_groups


def _context_user_id(entity_id):
    state_obj = hass.states.get(entity_id)
    context = getattr(state_obj, "context", None)
    return getattr(context, "user_id", None)


def _lookup() -> EntityLookup:
    return EntityLookup(
        is_state=is_state,
        state_attr=state_attr,
        device_id=device_id,
        labels=labels,
        context_user_id=_context_user_id,
    )


@service(supports_response="only")
def compute_lighting_groups(
    entities=None,
    brightness_multipliers=None,
    sensor_brightness=None,
    sensor_color_temp_kelvin=None,
):
    """pyscript.compute_lighting_groups(entities=[...], brightness_multipliers={...}, sensor_brightness=200, sensor_color_temp_kelvin=3000)

    Returns: {"groups": [{"multiplier", "brightness", "needing_off", "combined", "two_step"}, ...]}
    """
    groups = build_groups(
        entities=entities or [],
        brightness_multipliers=brightness_multipliers or {},
        sensor_brightness=sensor_brightness or 0,
        sensor_color_temp_kelvin=sensor_color_temp_kelvin or 3000,
        lookup=_lookup(),
    )
    return {
        "groups": [
            {
                "multiplier": g.multiplier,
                "brightness": g.brightness,
                "needing_off": g.needing_off,
                "combined": g.combined,
                "two_step": g.two_step,
            }
            for g in groups
        ]
    }
