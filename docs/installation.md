---
title: Quickstart
nav_order: 2
permalink: /installation/
---

# Quickstart
{: .no_toc }

Five minutes to a room running on the curve. This covers the standard setup; anything
unusual is in [Power users]({{ site.baseurl }}/advanced/).

{: .note }
> **Prerequisites** — Home Assistant 2026.4.0 or newer (the blueprint uses the native
> `occupancy.*` triggers), [HACS](https://hacs.xyz) installed, and your lights assigned to
> areas. Areas aren't strictly required, but FLARE offers to set itself up per room from
> them, which saves most of the work.

1. TOC
{:toc}

---

## Step 1 — install via HACS

HACS → three-dot menu → **Custom repositories**. Add:

```
https://github.com/danrspencer/flare
```

with type **Integration**. Then find **FLARE** in the HACS list and download it.

{: .tip }
> The dashboard card ships inside the integration and registers itself. There's no separate
> Lovelace resource to add, and nothing to keep in sync by hand.

## Step 2 — restart, then add FLARE

Restart Home Assistant, then **Settings → Devices & Services → Add Integration → FLARE**.

Adding it once creates both of FLARE's entries. The only thing it asks for is which
rooms to track:

- **FLARE Schedules** — the day-phase and colour curve. Add a schedule sensor per part of
  the house that should share a rhythm; one for the whole house is fine to start.
- **FLARE Tracking** — which lights FLARE is currently driving. Every area containing
  lights is offered, pre-selected, and each one you keep becomes a tracking scope. Trim
  the list if you like; anything you leave out simply isn't tracked.

They stay two entries because schedules and tracking are different kinds of thing, and
Home Assistant groups an integration's page by entry. You can add, retarget or remove
scopes at any time from the Tracking entry.

Each schedule sensor gets its own device carrying the five phase boundaries and eight
brightness/Kelvin values as ordinary entities. Adjust them any time — no reconfigure flow.

## Step 3 — import the blueprint and create an automation

[![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fdanrspencer%2Fflare%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fdanspencer%2Fflare.yaml)

Create an automation from it and fill in two things:

| Input | What to put |
|---|---|
| **FLARE Sensor** | The schedule sensor from step 2. |
| **Lights & Occupancy** | One target for the room — pick the **area**. Lights inside it get driven; occupancy-class binary sensors inside it decide when. |

That's the minimum. Everything else has a working default.

{: .note }
> Occupancy is optional. With no occupancy sensor in the target, FLARE keeps the room's
> lights on the curve but never turns them on or off by itself.

Repeat per room. Rooms can share a schedule sensor — the update jitter setting exists so
they don't all issue commands in the same instant.

## Step 4 — add the card (optional)

Add a **Manual** card to any dashboard:

```yaml
type: custom:flare-curve-card
sensor: ground_floor
```

`sensor` is the schedule sensor's slug — `ground_floor` for `sensor.ground_floor_flare`. The
card draws the day's brightness and colour curve with a marker at the current time.

---

## What now

- Lights not behaving as you expect? Each tracking scope has **Controlled** and
  **Overridden** counters and a **Clear** button — see
  [the integration reference]({{ site.baseurl }}/advanced/reference/#override-protection).
- Want a scene to own the room at certain times?
  [Scene handoff]({{ site.baseurl }}/advanced/scenes/).
- Want to skip the blueprint entirely?
  [Building without it]({{ site.baseurl }}/advanced/custom-automations/).
- Every blueprint input, with defaults: [Blueprint reference]({{ site.baseurl }}/blueprint/).
