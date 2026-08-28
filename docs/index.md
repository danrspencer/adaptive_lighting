---
title: Home
nav_order: 1
---

# FLARE
{: .no_toc }

**F**lexible **L**ighting **A**utomation & **R**econciliation **E**ngine — phase-based
circadian lighting for Home Assistant, with scene reconciliation and override protection
built in.
{: .fs-6 .fw-300 }

[Quickstart (5 mins)]({{ site.baseurl }}/installation/){: .btn .btn-primary .mr-2 }
[Curve playground]({{ site.baseurl }}/playground/){: .btn .mr-2 }
[GitHub](https://github.com/danrspencer/flare){: .btn }

---

## Four phases, not one curve

Adaptive lighting usually maps brightness and colour onto the sun's position. That tracks
the daylight closely, but the daylight isn't your schedule — and the two drift furthest
apart in the months you spend most of the day indoors.

FLARE works from your schedule instead, dividing the day into four named phases, each with
its own targets:

| Phase | What it's for |
|---|---|
| **Morning** | Bright and cold. [Research](https://pubmed.ncbi.nlm.nih.gov/36058557/) found 1.5h of bright morning light for a week improved office workers' sleep efficiency and reduced morning sleepiness. |
| **Day** | Bright, easing steadily from Morning's colour toward Evening's across the whole afternoon. |
| **Evening** | Dimming and warming, anchored to your actual sunset. |
| **Night** | Warm and low, flat until morning. |

Each boundary has its own transition: how long beforehand to start easing into the next
phase, so the new values land exactly as it begins. Set one to zero for a visible step
instead.

Morning, Day and Night are wall-clock times. Only Evening tracks the sun, clamped between
an earliest and a latest time so it moves with the season without drifting into the small
hours.

{: .tip }
> [Play with the curve]({{ site.baseurl }}/playground/) — every boundary, value and
> transition is a slider, and the chart is the same code the dashboard card runs.

---

## Two ways in

### Standard setup — plug and play
{: .no_toc }

Install via HACS, add the integration, import the blueprint, point it at a room. Everything
below is handled for you: reachability, colour-temperature tolerances, bulbs that can't take
brightness and colour in one command, occupancy timing, and leaving a light alone once
somebody else has taken it.

[Start here →]({{ site.baseurl }}/installation/){: .btn .btn-outline }

### Power users & builders
{: .no_toc }

The blueprint is a worked example, not the product. Every piece of it is a plain Home
Assistant action you can call yourself from YAML, scripts, Node-RED or AppDaemon — and the
override-protection machinery is available standalone, whether or not you use the rest.

[Go deeper →]({{ site.baseurl }}/advanced/){: .btn .btn-outline }

---

## What "reconciliation" means

FLARE expects to share a room. A scene can take some lights, someone can grab a switch, and
another automation can write the same bulb — none of that is treated as a fault:

- **Override protection** notices when a light no longer matches what FLARE last asked for,
  and stops driving it until it's released.
- **Scene handoff** lets a scene own part of a room while FLARE keeps the rest on the curve.
- **Tracking scopes** are named, configurable devices — one per room, typically — so you can
  see at a glance which lights FLARE is currently driving and which have been taken.
