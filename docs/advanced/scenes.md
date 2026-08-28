---
title: Scene handoff & reconciliation
parent: Power users
nav_order: 2
permalink: /advanced/scenes/
render_with_liquid: false
# Liquid is off for this page: it contains Home Assistant Jinja, which
# shares Liquid's {{ }} delimiters. With Liquid on, those examples render
# as empty strings and nothing errors - see tests/test_docs_site.py.
---

# Scene handoff & reconciliation
{: .no_toc }

FLARE is built to share a room. A scene, a wall switch, or another automation can take
a light at any time; reconciliation is how FLARE notices and steps back, and how it
picks the light up again afterwards.

<details open markdown="block">
  <summary>On this page</summary>
  {: .text-delta }
1. TOC
{:toc}
</details>

## How a handoff works

When the blueprint hands a room to a scene, it does two things: activates the scene, and
calls `flare.clear_claims` for the lights the scene covers. Clearing matters — without it
FLARE would keep its old claim, see the scene's values, and read them as an override
rather than as something it deliberately gave away.

Lights the scene *doesn't* cover keep getting the adaptive values, which is the point of
the coverage check below: a scene naming half a room leaves the other half to FLARE.

## `flare.compute_scene_coverage`

Given a candidate scene and the entities you want a default behaviour applied to, works out which of those
entities the scene actually covers — hand covered ones to the scene, apply your default (adaptive lighting or
anything else) to whatever's left. A scene only counts if it exists and everything it covers is within
`scope_entities`; a scene reaching outside that scope, or one that doesn't exist, is treated the same as no scene
at all. Nothing here is specific to adaptive lighting, or even to lighting.

```yaml
action: flare.compute_scene_coverage
data:
  scene_entity_id: scene.kitchen_night
  scope_entities: [light.kitchen_1, light.kitchen_2, light.kitchen_strip_effect]
  target_entities: [light.kitchen_1, light.kitchen_2]
response_variable: coverage
# coverage.scene_active / scene_valid / covered_entities / uncovered_entities
```

## When someone takes a light by hand

Nothing special happens, and that's deliberate. The light stops matching FLARE's recorded
claim, so it classifies as `overridden` and is excluded from the next tick — see
[override protection](../reference/#override-protection). It comes back
under FLARE's control when it is turned off and on again, when the device drops and
reconnects, or when you press the scope's **Clear** button.
