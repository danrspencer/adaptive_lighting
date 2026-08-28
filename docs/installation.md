---
title: Installation
nav_order: 3
---

# Installation
{: .no_toc }

Two separate installs, done in order: the integration first, then the blueprint that depends on it. The
dashboard card comes along with the integration — there's nothing extra to install for it.

<details open markdown="block">
  <summary>On this page</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## 1. Install Adaptive Lighting Helpers

Requires [HACS](https://hacs.xyz) already set up. This repo isn't in the HACS default store yet, so it's
added as a *custom repository* first.

1. In Home Assistant, open **HACS** in the sidebar.
2. Click the **⋮** menu in the top-right → **Custom repositories**.
3. Paste this repository's URL, set **Type** to **Integration**, then click **Add**:
   ```
   https://github.com/danrspencer/adaptive_lighting
   ```
4. Still in HACS, search for **Adaptive Lighting Helpers**, open it, and click **Download**.
5. Restart Home Assistant: **Settings → System → Restart**.

   This one-time restart is required because Home Assistant only scans for brand-new custom integrations
   at startup. Every *later* update installs with just a HACS download — no restart.
   {: .note }

6. Go to **Settings → Devices & Services → Add Integration** and search for **Adaptive Lighting
   Helpers**.

You'll end up with **two** entries, not one:

| Entry | What it holds |
|---|---|
| **Adaptive Lighting Schedules** | Your day-phase schedules and their curve settings. One subentry per schedule. |
| **Adaptive Lighting Tracking** | The services, the override-protection claim registry, and the state devices. |

They're deliberately separate. Home Assistant renders one section per subentry with no way to group them
by type, so a single entry would flatten schedules and tracking scopes into one long list of peers.

## 2. Add a schedule

Adding the integration creates no devices and no entities on its own — that's deliberate. A schedule is
something you add explicitly:

1. Open **Settings → Devices & Services → Adaptive Lighting Schedules**.
2. Click **Add Sensor** and give it a name — "Ground Floor", "First Floor", "Living Room", whatever
   matches how you want to group rooms.
3. Every field is optional and has a sensible default. You can change them all later from the dashboard
   or the device page.

Each schedule you add creates its own device with:

- `sensor.<name>_flare` — the current phase, brightness, colour temperature, today's boundary
  timestamps, and the full 289-point day curve.
- `select.<name>_flare_phase` — manual phase override, self-clearing at the next natural
  boundary unless you turn on `switch.<name>_sticky_phase_override`.
- Five `time.*` boundaries and eight `number.*` curve values, all live config entities.

Add as many schedules as you like. A house split by floor works well: everyone upstairs generally wants
the same lighting at the same time, and it's one fewer thing to keep in sync than a schedule per room.

[Try the settings out first]({{ '/playground.html' | relative_url }}){: .btn .btn-purple }

## 3. Install the blueprint

Requires step 1. The blueprint calls the `apply_lighting` service that the integration registers —
importing it early works, but the automation won't run correctly until the integration exists.

1. Go to **Settings → Automations & Scenes → Blueprints** → **Import Blueprint**.
2. Paste this URL, click **Preview**, then **Import Blueprint**:
   ```
   https://github.com/danrspencer/adaptive_lighting/blob/main/blueprints/automation/danspencer/flare.yaml
   ```
3. Go to **Settings → Automations & Scenes → Create Automation → Use existing blueprint** and choose
   **Adaptive Lighting**.
4. Fill in the room's lights and (optionally) the schedule sensor from step 2, then save.

Repeat once per room. The [blueprint reference]({{ '/blueprint/' | relative_url }}) covers what every
input does.

### Setting the room target

The single **Room target** input does double duty: lights inside it are controlled, and any
occupancy-class `binary_sensor` inside it governs occupancy. You can point it at an area, a floor, a
label, specific devices, or specific entities.

Occupancy filters strictly by `device_class: occupancy`. Motion-class sensors are never picked up, even
if you target them directly.
{: .warning }

## 4. Add the dashboard card

The card ships inside the integration and registers itself the moment the integration is added. There's
no separate HACS entry and nothing to add under **Settings → Dashboards → Resources**.

1. Open the dashboard you want it on → **Edit Dashboard** → **Add Card** → scroll to the bottom →
   **Manual**.
2. Paste this, changing `ground_floor` to match your schedule's name (lowercased, spaces become
   underscores — "Ground Floor" becomes `ground_floor`):
   ```yaml
   type: custom:flare-curve-card
   sensor: ground_floor
   ```
3. Click **Save**.

For a fuller layout — the curve graph plus the phase override and every schedule and curve setting as
tiles — use
[`dashboard/adaptive-lighting-section.yaml`](https://github.com/danrspencer/adaptive_lighting/blob/main/dashboard/adaptive-lighting-section.yaml)
instead. See that file's header comment for the one extra step it needs.

In a `sections` view, add `grid_options: {columns: full}` to the card or it renders at about a third
width, even in a full-width section.
{: .note }

## Migrating from an older version

If you're coming from a pre-rewrite version of this blueprint, the inputs changed
(`scene_sensor`/`scene_name_prefix` became `scene_template`/`extra_triggers`). Every room automation
still using the old inputs shows as misconfigured until updated.

Do this deliberately, one room at a time, rather than all at once.
{: .warning }
