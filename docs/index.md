---
title: Home
nav_order: 1
---

# Adaptive Lighting

Your lights, matched to the shape of your day — bright and cool to help you wake up, gradually warming
through the afternoon, dimming to a relaxed glow as evening sets in, and low and warm once the house is
asleep. Rooms turn their lights on and off as people come and go, manual changes are left alone until
you're done with them, and anything a scene already has covered is left to the scene.

[Play with the curve]({{ '/playground.html' | relative_url }}){: .btn .btn-purple }
[Install it]({{ '/installation.html' | relative_url }}){: .btn }
[View on GitHub](https://github.com/danrspencer/adaptive_lighting){: .btn }


Two pieces, installed separately and useful separately:

| Piece | What it is |
|---|---|
| **[Adaptive Lighting Helpers]({{ '/helpers/' | relative_url }})** | A Home Assistant integration. The phase schedule, per-light grouping, override protection, and scene gap-filling, exposed as plain services any automation can call. |
| **[The blueprint]({{ '/blueprint/' | relative_url }})** | A per-room automation built on those services — triggers, occupancy, target resolution, scene handoff. |

They're deliberately loosely coupled: the blueprint calls `apply_lighting` the same way it calls
`light.turn_on`, without assuming anything about how it's implemented. You can use the services on their
own and never touch the blueprint.

---

## Why four phases, not a continuous curve

Most "adaptive lighting" tools compute one continuous curve straight from the sun's position — brightness
and colour temperature interpolated smoothly between sunrise and sunset, nothing else to it. That's a
reasonable default, but it treats every part of your day the same way: just "more or less light," rather
than light with a *purpose*.

This project instead uses four named phases, each justified on its own terms rather than just given its
own numbers on a curve.

### Morning

Exists to help you **wake up**, not to track sunrise. It starts at a fixed time before you'd normally be
up, independent of the season — a sun-tracking curve would have it arrive at 5am in June and 8am in
December, which isn't what a wake-up light is for.

Bright, cool-white light in the morning has been linked to better alertness later in the day:
[one study](https://pubmed.ncbi.nlm.nih.gov/36058557/) found office workers given 1.5 hours of bright
morning light for a week had higher sleep efficiency and less morning sleepiness than under regular
office lighting.

### Day

The long middle stretch, gradually warming as it runs toward evening so the eventual transition doesn't
feel abrupt.

### Evening

When relaxed, warm lighting takes over — and the one phase that *does* track the sun, so your indoor
lighting shifts in step with what's actually happening outside.

It's clamped between an earliest and a latest bound, though, so a 4pm winter sunset doesn't dump you into
"relaxed evening" mode the moment you walk in from work, and a 10pm midsummer sunset doesn't mean evening
never really arrives. [The playground]({{ '/playground.html' | relative_url }}) has presets for both ends
of that, if you want to see the clamp work.

### Night

Not tied to any solar event at all — it's just what the house should look like once everyone's asleep:
dim and warm, the lighting you want on at 3am without waking yourself up further.

---

## What else it does

Beyond the schedule, the parts that turn a curve into something you can actually live with:

- **Override protection.** Change a light by hand and the automation backs off it rather than fighting
  you, until the room empties or the light goes off. It re-checks live state on every tick rather than
  remembering that an override happened once.
- **Occupancy.** Rooms light up when someone arrives and turn off once they've gone, with a wait time
  sized for the fact that most motion sensors report "clear" the moment you sit still.
- **Scene handoff.** A scene can take a room over completely, or cover just some of its lights and let
  the schedule fill in the rest.
- **Self-healing.** Bulbs that don't reach what they were told — or that drop off the network and come
  back — get corrected on the next tick rather than sitting wrong until someone notices.
- **Two-step transitions.** Some bulbs can't change brightness and colour in one command. Those get
  detected and driven in two steps, automatically.

Full detail in the [integration reference]({{ '/helpers/' | relative_url }}) and the
[blueprint reference]({{ '/blueprint/' | relative_url }}).
