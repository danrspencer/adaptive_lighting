# Adaptive Lighting

Your lights, matched to the shape of your day — bright and cool to help you wake up, gradually warming through
the afternoon, dimming to a relaxed glow as evening sets in, and low and warm once the house is asleep. Rooms
turn their lights on and off as people come and go, manual changes are left alone until you're done with them,
and anything a scene already has covered is left to the scene.

**📖 [Documentation site](https://danrspencer.github.io/adaptive_lighting/)**

The clearest way to see what this does is the
**[interactive curve playground](https://danrspencer.github.io/adaptive_lighting/playground.html)** — it renders
the real dashboard card, and you can drag the schedule and curve settings around and watch it redraw.

## Contents

- [Why four phases, not a continuous curve](#why-four-phases-not-a-continuous-curve)
- [Adaptive Lighting Helpers (the integration)](#adaptive-lighting-helpers-the-integration)
- [The blueprint](#the-blueprint)
- [Installation](#installation)
  - [1. Install Adaptive Lighting Helpers](#1-install-adaptive-lighting-helpers)
  - [2. Install the blueprint](#2-install-the-blueprint)
  - [3. Add the dashboard card (optional)](#3-add-the-dashboard-card-optional)

## Why four phases, not a continuous curve

Most "adaptive lighting" tools compute one continuous curve straight from the sun's position — brightness and
colour temperature interpolated smoothly between sunrise and sunset, nothing else to it. That's a reasonable
default, but it treats every part of your day the same way: just "more or less light," rather than light with a
*purpose*. This project instead uses four named phases — Morning, Day, Evening, Night — each justified on its own
terms, not just given its own numbers on a curve:

- **Morning** exists to help you wake up, not to track sunrise. It starts at a fixed time before you'd normally
  be up, independent of the season — a sun-tracking curve would have it arrive at 5am in June and 8am in
  December, which isn't what a wake-up light is for. Bright, cool-white light in the morning has been linked to
  better alertness later in the day: [one study](https://pubmed.ncbi.nlm.nih.gov/36058557/) found office workers
  given 1.5 hours of bright morning light for a week had higher sleep efficiency and less morning sleepiness than
  under regular office lighting.
- **Day** is the long middle stretch, gradually warming as it runs toward evening so the eventual transition
  doesn't feel abrupt.
- **Evening** is when relaxed, warm lighting takes over — the one phase that *does* track the sun (sunset), so
  your indoor lighting shifts in step with what's actually happening outside. It's clamped between an earliest
  and latest bound, though, so a 4pm winter sunset doesn't dump you into "relaxed evening" mode the moment you
  walk in from work, and a 10pm midsummer sunset doesn't mean evening never really arrives.
- **Night** isn't tied to any solar event at all — it's just what the house should look like once everyone's
  asleep: dim and warm, the lighting you want on at 3am without waking yourself up further.

Each boundary is independently configurable, each phase's brightness/colour temperature is too, and any phase
can be pinned manually when you want to override the schedule for a while. You can also run any number of these
schedules at once, each with its own name and settings — see
[docs/HELPERS.md](docs/HELPERS.md#optional-day-phasecurve-sensors).

## Adaptive Lighting Helpers (the integration)

Exposes the phase schedule above, plus per-light grouping (reachability, tolerance, override protection,
two-step transitions, optional RGB colour) and scene-coverage gap filling, as four plain HA services —
`compute_lighting_groups` and `compute_curve` are pure planners that hand back data; `apply_lighting` wraps the
same grouping logic and actually turns lights on/off for you; `compute_scene_coverage` is the scene-handoff
helper. All usable from your own automations with no blueprint required. Can optionally run the schedule
continuously as sensors instead of calling `compute_curve` yourself. Full service contracts, YAML examples, and
the sensor/entity list: **[docs/HELPERS.md](docs/HELPERS.md)**.

## The blueprint

A per-room automation built on the services above (loosely coupled — it calls `apply_lighting` the same way it
calls `light.turn_on`, without assuming anything about how that service is implemented). Brightness and
colour temperature follow the phase schedule, motion controls on/off, scenes can take over partially or entirely,
manual changes are respected, and lights that don't reach their target get corrected automatically. Full
feature-by-feature breakdown and the input reference: **[docs/BLUEPRINT.md](docs/BLUEPRINT.md)**.

## Installation

Two separate installs, done in order: the integration first, then the blueprint (which depends on it). The
dashboard card is optional and needs neither a HACS entry of its own nor a manual Lovelace resource — it comes
along with the integration automatically.

### 1. Install Adaptive Lighting Helpers

Requires [HACS](https://hacs.xyz) already set up in your Home Assistant instance — this repo isn't (yet)
published to the HACS default store, so it's added as a *custom repository* first.

1. In Home Assistant, open **HACS** in the sidebar.
2. Click the **⋮** (three-dot) menu in the top-right corner → **Custom repositories**.
3. Paste this repository's URL, set **Type** to **Integration**, then click **Add**:
   ```
   https://github.com/danrspencer/adaptive_lighting
   ```
4. Still in HACS, search for **Adaptive Lighting Helpers**, open it, and click **Download**.
5. Restart Home Assistant: **Settings → System → Restart**. This one-time restart is required because Home
   Assistant only scans for brand-new custom integrations at startup — every later update installs with just a
   HACS download, no restart needed.
6. Go to **Settings → Devices & Services → Add Integration**, search for **Adaptive Lighting Helpers**, and add
   it. There's nothing to fill in on this screen — it just registers the services and the dashboard card.
7. *(Optional, but needed for the dashboard card, the phase-schedule sensors, or the blueprint)* Open the
   integration's page (**Settings → Devices & Services → Adaptive Lighting Helpers**) and click **Add Sensor**.
   Give it a name (e.g. "Living Room") — this creates one day-phase/curve schedule (brightness, colour
   temperature, and timings) you can point a room's blueprint automation or dashboard card at. Add as many as
   you like, one per room or zone. Every field is optional and has a sensible default — see
   [docs/HELPERS.md](docs/HELPERS.md) if you want to change any of them.

### 2. Install the blueprint

Requires step 1 to be done first — the blueprint calls the `apply_lighting` service that step registers, so
importing it beforehand will still work but the automation won't run correctly until the integration exists.

1. Go to **Settings → Automations & Scenes → Blueprints** tab → **Import Blueprint** (top right).
2. Paste this URL into the box, click **Preview**, then **Import Blueprint**:
   ```
   https://github.com/danrspencer/adaptive_lighting/blob/main/blueprints/automation/danspencer/adaptive_lighting.yaml
   ```
3. Go to **Settings → Automations & Scenes** → **Create Automation** → **Use existing blueprint** → choose
   **Adaptive Lighting**. Fill in a room's lights/target and (optionally) the sensor you created in step 1, then
   save. Repeat once per room — see [docs/BLUEPRINT.md](docs/BLUEPRINT.md) for what every input does.

> **Migrating from an older, pre-rewrite version of this blueprint?** The inputs changed
> (`scene_sensor`/`scene_name_prefix` → `scene_template`/`extra_triggers`) — every room automation still using the
> old inputs will show as misconfigured until you update it. Do this deliberately, one room at a time, rather
> than all at once.

### 3. Add the dashboard card (optional)

The card ships inside the integration and registers itself with Home Assistant's frontend the moment step 1's
integration is added — there's no separate HACS entry for it and nothing to add under
**Settings → Dashboards → Resources**.

1. Open the dashboard you want the card on → **Edit Dashboard** → **Add Card** → scroll to the bottom →
   **Manual** (this lets you paste YAML directly instead of using the visual picker).
2. Paste in the contents of [`dashboard/house-settings-card.yaml`](dashboard/house-settings-card.yaml), then
   change `sensor: living_room` to match the sensor you named in step 1 — spaces become underscores and
   everything is lowercased, so a sensor named "Living Room" becomes `sensor: living_room`.
3. Click **Save**. For a fuller layout — the curve graph plus the phase-override switch and every schedule/curve
   setting as tiles — paste [`dashboard/adaptive-lighting-section.yaml`](dashboard/adaptive-lighting-section.yaml)
   instead (see that file's own header comment for one extra step it needs).

## Contributing

Repository layout, running the test suite, previewing the dashboard card without a live Home Assistant instance,
and current project status all live in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
