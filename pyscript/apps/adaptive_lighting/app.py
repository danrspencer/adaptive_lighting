"""
Adaptive Lighting - pyscript app.

Exposes pyscript.compute_lighting_groups, which the blueprint's action:
calls (with response_variable) to get back the multiplier groups it
needs to command, instead of computing them in ~150 lines of Jinja.
All the actual logic lives in pyscript/modules/adaptive_lighting - this
file is just the thin adapter to real Home Assistant state.

NOT YET VALIDATED against a running pyscript install - this is Phase 1
of the plan, written before the Phase 0 spike. Before trusting it,
confirm:
  - is_state / state_attr / device_id / labels are available as plain
    pyscript globals with these names/signatures (they're assumed to
    mirror the same-named Jinja template functions).
  - `import adaptive_lighting` resolves to pyscript/modules/adaptive_lighting
    from an app under pyscript/apps/.
  - supports_response="only" makes the dict below available via
    response_variable in the calling automation's next action step,
    and confirm whether it comes back as dict-style or attribute-style
    access in templates (plan assumes `{{ plan.groups }}` works; may
    need `{{ plan['groups'] }}` instead).
"""

from adaptive_lighting import EntityLookup, build_groups


def _lookup() -> EntityLookup:
    return EntityLookup(
        is_state=is_state,
        state_attr=state_attr,
        device_id=device_id,
        labels=labels,
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
