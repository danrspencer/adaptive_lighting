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

Most adaptive-lighting tools map brightness and colour straight onto sun elevation. That
sounds right and behaves oddly: the sun is the same height at 8am and 5pm, so your kitchen
gets identical light for breakfast and for cooking dinner. In midwinter it never gets high
enough for bright light at all.

FLARE divides the day into four named phases instead, each with its own targets:

| Phase | What it's for |
|---|---|
| **Morning** | Bright and cold, deliberately. [Research](https://pubmed.ncbi.nlm.nih.gov/36058557/) found 1.5h of bright morning light for a week improved office workers' sleep efficiency and reduced morning sleepiness. |
| **Day** | Bright, cooling gradually towards the afternoon. |
| **Evening** | Dimming and warming, anchored to your actual sunset. |
| **Night** | Warm and low, flat until morning. |

Only the Evening boundary tracks the sun — clamped between an earliest and a latest time,
so it moves with the season without drifting into the small hours. Morning, Day and Night
are wall-clock times, because that's what your routine actually is.

{: .tip }
> [Play with the curve]({{ site.baseurl }}/playground/) — every boundary and value is a slider,
> and the chart is the same code the dashboard card runs.

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
