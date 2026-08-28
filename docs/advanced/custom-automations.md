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

Every service the blueprint calls is a plain Home Assistant action, so anything that can
call one — a YAML automation, a script, Node-RED, AppDaemon — can drive FLARE directly.

<details open markdown="block">
  <summary>On this page</summary>
  {: .text-delta }
1. TOC
{:toc}
</details>

## What you actually need

{: .note }
> A FLARE **schedule sensor** for the values, and — if you want override protection — the
> `tracking_device_id` of a **tracking scope** covering the lights. Omit it and the call still
> writes the light, it just isn't tracked, so nothing is ever left alone.

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
        tracking_device_id: "{{ device_id('sensor.kitchen_flare_tracking') }}"
```

That is the whole contract. `apply_lighting` handles reachability, tolerance checks,
two-step bulbs, RGB routing and override protection itself — the last of those only for
whichever scope you name.

### Using override protection standalone

Override protection is also two standalone services — `claims_check` (read-only) and
`claims_record` — usable on your own entities, with or without `apply_lighting`. Unlike
`apply_lighting`, both exist only to read or write tracking claims, so `tracking_device_id`
is **required** on each — there's nothing useful either can do without one:

```yaml
action: flare.claims_check
data:
  entities: [light.kitchen_1]
  tracking_device_id: "{{ device_id('sensor.kitchen_flare_tracking') }}"
response_variable: control
# control.results["light.kitchen_1"] ->
#   {"blocked": false, "status": "controlled", "matched_via": "latest-context", "scope": "Kitchen"}
# matched_via is "context" (a direct match on either of the claim's context ids) or "value" (the
# delayed-echo/mired rescue above, against either claim's target) for a `controlled` status, null
# otherwise - useful for understanding *why* a light is considered ours, not just that it is.
# "scope" simply echoes tracking_device_id's own title back.
```

```yaml
# After actually issuing your own light.turn_on, so a later claims_check call recognises it as yours:
action: flare.claims_record
data:
  entities: [light.kitchen_1]
  tracking_device_id: "{{ device_id('sensor.kitchen_flare_tracking') }}"
  targets:
    light.kitchen_1: { brightness: 200, color_temp_kelvin: 3000 }
```

`claims_check`'s `status` values are the same ones a state device's `claims` attribute
shows, and `targets` is the same shape `apply_lighting` records on every write.

`claims_clear` is the escape hatch for a light stuck on `overridden`. An excluded light
never gets a fresh claim recorded, so on a ramping curve its stored target keeps drifting
further from the current one and it cannot recover on its own:

```yaml
action: flare.claims_clear
data:
  entities: [light.kitchen_1]
  tracking_device_id: "{{ device_id('sensor.kitchen_flare_tracking') }}"
```

The next write to a cleared entity is treated like a brand-new entity's first write. Each
state device exposes the same thing as a `button.<name>_flare_clear` entity, which clears
every claim in that scope.

## Doing your own dispatch

If you want to issue `light.turn_on` yourself — because you need an effect, a colour mode
FLARE doesn't handle, or a device outside the light domain — use the planner instead of
the dispatcher:

1. `flare.claims_check` to ask which entities you should leave alone.
2. Your own writes for the rest.
3. `flare.claims_record` immediately afterwards, so FLARE recognises your write next time
   instead of reading it as an external change.

{: .tip }
> Pass `targets` to `claims_record`. Without it a context mismatch is always treated as
> external, so a bulb that confirms slowly gets misread as overridden.

`flare.compute_lighting_groups` is the same planner `apply_lighting` uses internally,
exposed without the dispatch — it returns the groups it *would* have written, so you can
apply them however you like.
