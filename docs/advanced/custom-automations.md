---
title: Building without the blueprint
parent: Power users
nav_order: 3
permalink: /advanced/custom-automations/
render_with_liquid: false
# Liquid is off for this page: it contains Home Assistant Jinja, which
# shares Liquid's {{ }} delimiters. With Liquid on, those examples render
# as empty strings and nothing errors - see tests/test_docs_site.py.
---

# Building without the blueprint
{: .no_toc }

The blueprint is a worked example, not the product. Every service it calls is a plain
Home Assistant action, so anything that can call an action — a YAML automation, a script,
Node-RED, AppDaemon, an ESPHome-driven button — can drive FLARE directly.

<details open markdown="block">
  <summary>On this page</summary>
  {: .text-delta }
1. TOC
{:toc}
</details>

## What you actually need

{: .note }
> A FLARE **schedule sensor** for the values, and a **tracking scope** covering the lights
> if you want override protection. A light matching no scope still works — it just isn't
> tracked, so nothing is ever left alone.

The smallest useful automation reads the schedule sensor and applies it:

```yaml
- alias: Kitchen lighting
  triggers:
    - trigger: state
      entity_id: sensor.ground_floor_flare
  actions:
    - action: flare.apply_lighting
      data:
        entities: "{{ area_entities('kitchen') | select('match', '^light\\.') | list }}"
        brightness: "{{ state_attr('sensor.ground_floor_flare', 'brightness') }}"
        color_temp_kelvin: "{{ state_attr('sensor.ground_floor_flare', 'color_temp') }}"
        transition: 30
```

That is the whole contract. `apply_lighting` handles reachability, tolerance checks,
two-step bulbs, RGB routing and override protection itself.

### Using override protection standalone

Everything above is also its own pair of services - `check_control` (read-only) and `record_write`
(records a write) - not specific to lights, or to this integration's own `apply_lighting`. Any automation can
use them directly on its own entities:

```yaml
action: flare.check_control
data:
  entities: [light.kitchen_1]
response_variable: control
# control.results["light.kitchen_1"] ->
#   {"blocked": false, "status": "controlled", "matched_via": "latest-context", "scope": "Kitchen"}
# matched_via is "context" (a direct match on either of the claim's context ids) or "value" (the
# delayed-echo/mired rescue above, against either claim's target) for a `controlled` status, null
# otherwise - useful for understanding *why* a light is considered ours, not just that it is.
```

```yaml
# After actually issuing your own light.turn_on, so a later check_control call recognises it as yours:
action: flare.record_write
data:
  entities: [light.kitchen_1]
  targets:
    light.kitchen_1: { brightness: 200, color_temp_kelvin: 3000 }
```

`apply_lighting`/`compute_lighting_groups` use the exact same underlying logic internally (a direct Python
call, not a service-to-service round trip) - `check_control`'s `status` values are the same ones
each state device's `claims` attribute shows (see below), and `targets` is the same shape `apply_lighting`
itself records automatically on every write it makes.

A third service, `clear_claims`, is the manual escape hatch for a light stuck reporting `overridden` with no
other way back - possible because `apply_lighting`/`compute_lighting_groups` never call `record_write`
internally for anything already excluded, so an overridden light's own `latest` claim can go permanently stale
(most concretely: during a ramping curve, once its recorded target drifts more than a tick or two away from
where the curve has since moved on to):

```yaml
action: flare.clear_claims
data:
  entities: [light.kitchen_1]
```

The next write to a cleared entity, from anyone, is treated exactly like a brand-new entity's first write - no
owner-conflict check is possible until a fresh claim exists to compare against. The **FLARE Write
Tracking** dashboard card exposes this as a "Clear" button on every row, no confirmation prompt - it's a
diagnostic bookkeeping entry, not the light itself, and a fresh claim gets re-established the moment anything
next writes to that entity.

## Doing your own dispatch

If you want to issue `light.turn_on` yourself — because you need an effect, a colour mode
FLARE doesn't handle, or a device outside the light domain — use the planner instead of
the dispatcher:

1. `flare.check_control` to ask which entities you should leave alone.
2. Your own writes for the rest.
3. `flare.record_write` immediately afterwards, so FLARE recognises your write next time
   instead of reading it as an external change.

{: .tip }
> Pass `targets` to `record_write`. Without it a context mismatch is always treated as
> external, so a bulb that confirms slowly gets misread as overridden.

`flare.compute_lighting_groups` is the same planner `apply_lighting` uses internally,
exposed without the dispatch — it returns the groups it *would* have written, so you can
apply them however you like.
