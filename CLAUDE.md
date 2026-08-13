# CLAUDE.md

Context for resuming work on this repo in a fresh session. See README.md
for the user-facing description; this file is about *how* to work on it
and *why* it's shaped the way it is. This file documents current
architecture and decisions, not a session-by-session change log - the
git history is the changelog; a fact only belongs here once it's stayed
true.

## Where this came from

This started as a single Home Assistant blueprint - still live, at
`blueprints/automation/danspencer/adaptive_lighting_unified.yaml` on
the actual HA instance - that grew ~150 lines of namespace-loop Jinja
for target resolution, reachability filtering, multiplier bucketing,
tolerance-based "already correct" checks, and manufacturer-based
two-step transition detection. It worked, but became unreadable and
hard to change safely. This repo is the migration of the genuinely
computational parts (not triggers/conditions) out of Jinja, while
keeping the blueprint native for the parts HA already does well.

That migration went through two different backends. The first attempt
moved the computation into pyscript - working, unit-tested Python, but
getting it to actually *load* inside pyscript cost an entire session
chasing dormant bugs that all looked identical from the outside
("nothing happens, no error") - see lesson 8 below. That experience is
why the computation now lives in
`custom_components/adaptive_lighting_helpers/` instead - a real Home
Assistant integration that registers its own services directly, with no
pyscript dependency and none of that machinery to go wrong. **pyscript
is entirely gone from this repo now** - lesson 8 is kept only in case
pyscript comes up again in some other project.

**This repo is now two independent, loosely-coupled pieces** (see "The
architectural split" below): the `adaptive_lighting_helpers` HACS
integration (curve math + grouping logic, exposed as plain HA services
anyone can call from their own automation) and the `adaptive_lighting`
blueprint (triggers/conditions/target-resolution, built on top of
those services but not assuming anything about how they're
implemented). Keep them decoupled - services should be documented and
useful on their own merits, not written as if the blueprint is their
only consumer.

This repo's blueprint is deliberately named `adaptive_lighting.yaml`
(blueprint name "Adaptive Lighting"), not `adaptive_lighting_unified`
- different file, different in-UI name, so it can be installed and
tested alongside the live `adaptive_lighting_unified.yaml` without
touching it, and rooms migrated over individually. Linking the two
blueprints to the same filename is exactly what caused the incident
in lesson 6 below - don't reintroduce that collision.

**Documentation layout, split for the same reason as the code:**
README.md is the pitch - why this project exists, why the day is
divided into four named phases (Morning/Day/Evening/Night) rather than
a single continuous sun-elevation curve the way most adaptive-lighting
tools work, and how to install it. The mechanical reference detail -
full service contracts and YAML examples for the integration, the full
per-feature/input breakdown for the blueprint - moved out to
`docs/HELPERS.md` and `docs/BLUEPRINT.md` respectively, so someone
deciding whether to use this doesn't have to wade through both to find
the "why." The Morning-phase research citation
([Xiao et al., 2022](https://pubmed.ncbi.nlm.nih.gov/36058557/) - 1.5h
of bright morning light for a week improved office workers' sleep
efficiency and reduced morning sleepiness vs regular office lighting)
was verified via a live web search before being added, not recalled
from memory - worth re-verifying rather than trusting as-is if it's
ever revised, same standard any factual claim in these docs should
meet.

## The architectural split (deliberate, not arbitrary)

**Stays in the blueprint (Jinja/YAML):**
- All triggers, conditions, target resolution (`resolved_entities`),
  occupancy detection (`occupied`, plus the native `occupancy.*`
  trigger/condition path - see "Current status" below), and the action
  *structure* (which service to call, on what target).
- Scene compatibility checking (`scene_active`/`scene_valid`) is a
  partial exception - the *logic* also exists as a standalone service
  (`compute_scene_coverage`), but the blueprint's own inline Jinja
  version is deliberately left in place rather than rewired to call it.
  Reason: it's read by a `condition:` block (see lesson 4 - conditions
  can't call services at all), and moving it server-side would mean
  losing that `condition:`-level suppression (the tick would still fire
  and call `apply_lighting`/`scene.turn_on` every time as an idempotent
  no-op instead of not running at all - functionally harmless, not
  behaviourally identical). See "Parked: scene handling in
  apply_lighting" below for the full design if this gets picked back up.
- The blueprint's `manual` trigger (context.user_id on the *triggering*
  state change) only ever blocks the one automation run where it fires
  - it has no memory, so it does NOT stop a later `adaptive` tick from
  overwriting the same light a minute afterwards. The actual sustained
  protection lives in `grouping.py`'s `EntityLookup.manually_set()`
  (see lesson 5), not here.
- Why any of this stays in Jinja at all: HA `condition:` blocks cannot
  call a service — only `action:` steps can — so anything
  condition:-gating needs to stay template-based. These pieces are also
  relatively compact and get real value from HA's native trace/debug UI
  (`ha_get_automation_traces`), used heavily throughout this project to
  diagnose issues live. Losing that observability isn't worth it for
  logic that isn't actually that bad.

**Lives in `custom_components/adaptive_lighting_helpers/` (a standalone
HACS integration, four services - see "Current status" for the current
contract of each):**
- Reachability filtering, multiplier bucketing, the tolerance-based
  "already at target" check, manual-override protection, and
  two-step-vs-combined / RGB-vs-colour-temp label routing
  (`grouping.py`).
- Why: this was the genuinely gnarly part — nested namespace loops,
  nothing pytest-testable in Jinja, and a real correctness gap
  (exact-match brightness/colour-temp comparisons that silently stopped
  skipping for any bulb with device-side rounding quirks).
- Day-phase brightness/Kelvin curve math (`curve.py`), ported from
  `custom_templates/adaptive_lighting.jinja` in the live HA config - not
  because it was complicated, but because it's exactly the kind of
  small, reusable, independently-useful piece of logic that belongs as
  a documented service in its own right.
- Scene-coverage gap filling (`scenes.py`) - "does this scene exist, is
  it within scope, and which of my target entities does it leave
  uncovered" - explicitly generic: nothing about it is specific to
  adaptive lighting, or lighting at all. "Apply a scene, then a default
  for whatever it doesn't cover" is a reusable pattern on its own.

All services are deliberately written and documented (see
`services.yaml`) as standalone tools - useful to anyone building their
own automation, not just to the blueprint in this repo.

**Considered and explicitly rejected: extracting target resolution**
(`resolved_entities`/`scope_entities`, duplicated three times in the
blueprint - once more in the `manual` trigger, structurally unfixable
since triggers can't call services either). Two blockers, either of
which alone would kill it: (1) `condition:` reads `resolved_entities`
directly and `scene_active` transitively via `scope_entities`, and
`condition:` runs *before* `action:` - a service call's response isn't
available yet at that point, so adopting one would mean moving those
`condition:` checks into an early `action:` bail-out instead, which
changes `mode: restart` behaviour (a trigger that used to be cleanly
rejected by `condition:` would instead interrupt an in-flight two-step
transition before its own bail-out stops it) and erodes the same
trace-visibility benefit cited above. (2) A Jinja-macro alternative
(viable in `trigger:`/`condition:`/`variables:` where a service
categorically isn't, via the `to_json`/`from_json` round-trip lesson 1
describes) would still mean adding a new required manual install step
(`custom_templates/*.jinja`, restart needed per lesson 2) to a blueprint
that's currently fully self-contained. (3) A third option - registering
a genuine Python-backed global Jinja function (callable bare, like
`is_state()`, no `{% import %}` needed) - is technically possible (a
real published integration, PiotrMachowski's Custom Templates, does
exactly this) but its own README warns it "tampers with internal code
of Home Assistant which *might* cause some unforeseen issues"; there's
no documented/supported HA extension point for this, unlike
`hass.services.async_register` or the `custom_templates/` convention.
Monkey-patching the one shared template engine every automation/sensor/
script on the instance depends on was judged not worth it. Don't
re-propose without new information changing this trade-off.

## Hard-won lessons (don't repeat these)

1. **Jinja macros can only return rendered text, never a native Python
   list.** `{% macro x() %}{{ some_list }}{% endmacro %}` returns a
   *string* that looks like a list. If a macro needs to hand back a
   real list to be used *within the same template*, round-trip through
   `to_json`/`from_json`. This is why an earlier attempt at a shared
   `custom_templates/*.jinja` macro for target resolution was abandoned
   in favour of duplicating that logic inline in the blueprint - not
   worth re-attempting without a strong reason.

2. **`custom_templates/*.jinja` files are scanned once at HA startup,
   not on a config/automation reload.** Adding a *new* file there and
   then reloading core config will NOT pick it up — it needs a full HA
   restart. This broke every automation sharing the blueprint in
   production for about a minute before being caught and reverted.

3. **Symlinks for deployment must be created on the HA host itself, not
   through a Samba/SMB mount from another machine.** A symlink's target
   is just a string; if it's written from macOS via a mounted share,
   the target needs to be a path meaningful to *Home Assistant's own
   filesystem* (e.g. `/config/...`), not the mounting machine's path.
   Simplest correct approach: clone this repo directly on the HA host
   and symlink from there.

4. **HA `condition:` blocks cannot call services.** This is *the*
   constraint that shaped the architectural split above — any value a
   `condition:` needs must be computed via template (or a native
   condition type - see the `occupancy.is_detected` note in "Current
   status"), not via a service call.

5. **A trigger firing once does not mean protection persists.** A
   one-shot trigger-level check (the blueprint's `manual` trigger,
   context.user_id-based) is not the same as a standing invariant later
   code respects - it only blocks the one automation run where it
   fires, not a later independent `adaptive` tick. Fixed properly in
   `grouping.py`'s `EntityLookup.manually_set()`: instead of
   remembering that an override happened, it re-checks the entity's
   *current* state's context on every call - room-empty, light-off, and
   device-recovery release conditions all fall out for free from that,
   with no persisted state needed.

6. **A same-named blueprint can take out every room at once.** This
   repo's blueprint used to share a filename with the live, already-
   deployed `adaptive_lighting_unified.yaml`, which 15 room automations
   referenced by that exact path - symlinking a materially different
   blueprint over it broke every one of them at once. Fixed at the root
   by giving this repo's blueprint a different file name *and* a
   different in-blueprint `name:`, so the two install side by side with
   zero interaction. Related: when a Samba-mounted view of `/config`
   disagrees with itself during recovery (a file `ls` won't show but
   which still blocks writes - consistent with a symlink Samba isn't
   surfacing but is still enforcing), stop trying to fix it through that
   mount and go to the host directly.

7. **A symlink's target is only meaningful from the shell session that
   created it.** A working symlink (right path, right name) can still
   fail every read with an opaque error indistinguishable from a
   permissions/sandboxing problem - `dirname "$0"` + `pwd` inside a
   deploy script bakes in whatever path *that* shell session happens to
   see (e.g. `/root/config/...` from an SSH-session alias), which is
   meaningless to a different process reading the same symlink back
   later, even on "the same host". `scripts/link_into_ha.sh` copies the
   blueprint and pyscript-era files instead of symlinking them (see its
   `copy()` function) specifically to sidestep this, not because
   symlinks are fundamentally broken. The dashboard card is still
   symlinked; if it ever shows the same failure mode, this is why.

8. **pyscript-specific loading gotchas (pyscript is no longer part of
   this repo - kept only in case pyscript resurfaces elsewhere):** a
   folder-based app only autoloads from a file named exactly
   `__init__.py` (any other filename is silently skipped, no error at
   any log level); an app can't share its name with a module package it
   imports from (identical names send pyscript's import resolution into
   infinite recursion until Python's recursion limit raises
   `RecursionError`, not a normal import error); and a pyscript app
   needs an explicit entry (even empty) under `pyscript: apps:` in YAML
   config, or it's silently skipped at debug-log level only, invisible
   at the default WARNING level. All three present identically from the
   outside: "nothing happens, no error."

9. **A stray `.bak-<timestamp>` directory under `custom_components/`
   isn't inert - it can break the domain it's a backup of.** Home
   Assistant discovers custom integrations by scanning every directory
   under `custom_components/` for a `manifest.json` and reading its
   `domain` key, not by the directory's own name - a leftover backup
   directory with the same `domain:` in its manifest broke config-flow
   resolution with a bare, unhelpful `404 Invalid handler specified`
   until the stray directory was found (only via grepping
   `home-assistant.log` for the domain name) and removed, and even then
   the fix needed a *second* full restart to take effect - HA's
   flow-handler registry is built once at startup, so un-discovering a
   directory needs the same "restart to rescan" treatment as
   discovering a new one. Clean up `link_into_ha.sh`'s `.bak-*` backups
   promptly, especially under `custom_components/`.

10. **A wrong-but-similarly-shaped constructor argument or a missing
    `@callback` decorator can sit dormant through months of "working"
    code, because unit tests never exercise the real HA event loop.**
    Two real instances in this integration: `DataUpdateCoordinator.__init__`
    was passed `__name__` (a plain string) where a `logging.Logger` was
    expected - fine until the coordinator's own refresh cycle actually
    called `self.logger.isEnabledFor(...)`, which only happened once a
    real schedule instance existed to refresh. Separately, a state-change
    listener called `hass.async_create_task()` without `@callback` -
    fine until it was registered against live entities and actually
    invoked, then raised a thread-safety `RuntimeError` (a plain `def`
    with no `@callback` marker runs in the worker thread pool, where
    `async_create_task` isn't safe to call - confirm via
    `homeassistant/core.py`'s `get_hassjob_callable_job_type` before
    trusting a fix here). Neither could have been caught without a live
    HA event loop actually exercising the code path.

11. **`sun.sun`'s `next_setting` attribute is exactly that - *next* -
    not "today's sunset."** The moment today's sunset passes,
    `next_setting` points at tomorrow's, roughly 24h ahead. Comparing a
    boundary directly against it (`max(earliest, min(next_setting,
    latest))`) silently clamps to the *latest* bound the instant sunset
    passes, instead of holding today's actual sunset time - `curve.py`'s
    boundary computation projects the sunset's local time-of-day onto
    today instead of using the absolute timestamp directly.

12. **A branch-name `raw.githubusercontent.com` URL can serve a stale,
    cached copy for a few minutes after a push, even when the fetching
    tool reports success.** Re-importing a blueprint immediately after
    pushing can silently install the *previous* commit's content -
    `ha_import_blueprint`'s own "re-imported successfully" response is
    not proof of freshness; confirm with `ha_read_file` against what
    actually landed. A commit-SHA-pinned raw URL
    (`.../blob/<full-sha>/...`) is immune to this, since GitHub treats
    that URL as immutable and never serves it stale - use one when
    testing a just-pushed change against a live instance.

## Current status

**Services** (`custom_components/adaptive_lighting_helpers/`,
`__init__.py`) - all four implemented, unit tested, and confirmed
working against the live instance:
- `compute_lighting_groups` / `compute_curve` - pure planners, no side
  effects.
- `compute_scene_coverage` - the standalone scene-handoff helper (see
  `scenes.py`), not currently called by the blueprint (see "Stays in
  the blueprint" above).
- `apply_lighting` - the only side-effecting service; wraps the same
  grouping logic and actually issues `light.turn_on`/`light.turn_off`.
  Takes `sensor_entity_id`, not raw brightness/colour values - reads
  `brightness`/`color_temp` (required - raises `ServiceValidationError`
  naming the missing attribute if absent, rather than silently dimming
  everything to brightness 1 the way an early version did) and
  `rgb_color` (optional) directly off whatever entity it's pointed at,
  generically - it never assumes it's reading this integration's own
  sensor, since multiple independent named sensor instances can exist
  (see "Multi-sensor schedule architecture" below). This is what the
  blueprint calls - see README's "Bring your own sensor" section for
  the full attribute contract.
- RGB colour (`prefer_rgb_color`) is implemented and unit tested but
  **not yet exercised live against an actual RGB-capable bulb** - only
  colour-temperature lights have been confirmed live so far.

**Multi-sensor schedule architecture** (`coordinator.py`, `sensor.py`,
`select.py`, `number.py`, `time.py`, `switch.py`) - the config entry
itself registers only the services above and carries no schedule of its
own; every schedule is a named "sensor" subentry, added via Settings →
Devices & Services → Adaptive Lighting Helpers → Add Sensor (name
required - there's exactly one way to add a schedule, no separate
main-entry special case). `schedule_instances(entry)` in
`coordinator.py` is the single place that enumerates all configured
sensors; every other file iterates its output rather than re-deriving
the subentry lookup. Each subentry gets its own HA device
(`ScheduleInstance.device_info`), and every entity uses
`has_entity_name=True` - renaming the device (Settings → Devices)
renames every entity's displayed name for free, no bulk update needed,
since the display name is computed live rather than stored as a static
string.

Per sensor, six entities:
- `sensor.<slug>_adaptive_lighting` - state is the phase name; current
  `brightness`/`color_temp`/`rgb_color` plus today's boundary
  timestamps (`morning_start`/`day_start`/`evening_start`/`night_start`/
  `evening_earliest`/`evening_latest`) all live as attributes on this
  one entity - no separate boundary-sensor entities (removed as UI
  noise; a `platform: state, attribute: phase` trigger on this entity
  already covers the automation case those existed for).
- `sensor.<slug>_adaptive_lighting_curve` - `attributes.points`, the
  full day as 289 samples, what the dashboard card reads.
  `_unrecorded_attributes = frozenset({"points"})` keeps this out of
  the recorder database (avoiding its 16KB state-attribute size
  warning) without needing a dedicated fetch service - a fetch-on-
  demand alternative was explored and rejected as strictly more
  computation for no real win over this one-line fix.
- `select.<slug>_adaptive_lighting_phase` - manual phase override
  (Auto/Morning/Day/Evening/Night). Self-clears at the next natural
  phase boundary by default; `switch.<slug>_sticky_phase_override`
  disables that and keeps a pinned override until cleared by hand.
  Implemented by comparing against the phase computed at override time
  on every refresh, not a timer - the same "check live state fresh,
  don't invent a persisted expiry" pattern `grouping.py`'s
  `manually_set()` uses.
- `time.<slug>_morning_time` / `day_time` / `evening_earliest_time` /
  `evening_latest_time` / `night_time` and `number.<slug>_morning_brightness`
  / `morning_kelvin` / `day_brightness` / `day_end_kelvin` /
  `evening_brightness` / `evening_kelvin` / `night_brightness` /
  `night_kelvin` - the five schedule boundaries and eight curve values,
  as live `entity_category: config` entities on the device rather than
  config-flow fields, editable at any time with an immediate coordinator
  refresh on change (not a wait for the next 60s poll). `number` has a
  built-in `RestoreNumber` mixin; `time`/`switch` don't, so those
  hand-roll persistence via `RestoreEntity` + `async_get_last_state()`,
  the same pattern `select.py`'s override already used.

Config lives in entity state, not `subentry.data` - `coordinator.py`
reads `hass.states.get(...)` for each `time.*`/`number.*` entity
directly (writing back into `subentry.data` was considered and rejected
since every subentry data change triggers a full entry reload via the
update listener, which would mean recreating every coordinator/entity
just to tweak one brightness number). A genuinely-missing entity (the
coordinator's very first refresh runs *before* platforms are forwarded,
so `time.*` entities don't exist in the state machine yet on a sensor's
first-ever setup, and are left `"unavailable"` rather than removed on
every subsequent reload) falls back to the same default the entity
itself will report moments later, so `phase_at()` never sees a missing
boundary - both the "doesn't exist yet" and "exists but unavailable"
cases route through the same fallback.

**Curve math** (`curve.py`) - every brightness/Kelvin literal that used
to be hardcoded is now a keyword-only parameter with a named default
(`DEFAULT_CURVE_VALUES`, `DEFAULT_SCHEDULE_HOURS`). Two non-obvious
formula facts worth knowing before touching this file:
- The brightness fade's span is `1.6×` the nominal evening-to-night
  window, not a 1:1 ratio - preserved as a ratio (not a literal span)
  so a custom brightness range keeps the same timing shape.
- The Kelvin evening-tail fade is
  `evening_kelvin + (night_kelvin - evening_kelvin) * t`, continuous by
  construction.

`night_floor_kelvin` (an earlier feature letting RGB-capable bulbs
target something warmer than a colour-temp bulb's native range) was
tried and then **fully removed**, at the user's explicit direction -
along with `kelvin_rgb`, the separate return key it powered (once the
feature was gone there was nothing left for it to diverge from
`kelvin`, so the key itself was dropped everywhere: `targets_for_phase`,
the curve points, the dashboard card, and the preview generator). Don't
re-add either without a concrete new reason - both were cut
deliberately. RGB colour is otherwise just the Kelvin→RGB conversion of
`kelvin`
(`curve.kelvin_to_rgb`, same rounding as the dashboard card's
`kelvinToRgb()` - `math.floor(x+0.5)`, not Python's banker's-rounding
`round()`) - there's no separate RGB curve.

**Parked: scene handling in `apply_lighting`.** Not implemented -
recorded here so a future session doesn't have to re-derive it. Two
designs were explored:
1. **Straight port** (smaller, mostly-scoped): `apply_lighting` gains
   optional `scene_entity_id`/`scope_entities`, calls
   `compute_scene_coverage` internally, then `scene.turn_on` (if active)
   plus light dispatch on `uncovered_entities`. Known cost: loses the
   `condition:`-level suppression that stops the adaptive tick from
   even running while a scene owns the room (see "Stays in the
   blueprint" above) - accepted as a fine tradeoff if this gets picked
   up, not a blocker.
2. **Bigger idea**: instead of `scene.turn_on`, read a scene's own
   *stored* per-entity target values and feed them through
   `apply_lighting`'s existing grouping/multiplier pipeline, so a
   brightness multiplier could scale a scene's own brightness (which it
   explicitly cannot today - scene-covered entities are excluded from
   multiplier application entirely). Confirmed live that
   `ha_config_get_scene(...)` does expose full per-entity attributes,
   but real complications: a scene captures whichever colour mode was
   active when recorded (`xy`/`hs`/`color_temp` all possible in the
   same scene, `apply_lighting` only understands colour-temp and RGB
   today), reading stored scene config is a less-trodden HA surface
   than the `hass.states`/registry trio this integration relies on
   everywhere else, and scenes can carry `effect` and non-light domains
   `apply_lighting` has no model for. Judged a genuinely bigger feature
   than the straight port - treat as its own decision, not a
   prerequisite.

**Blueprint** (`blueprints/automation/danspencer/adaptive_lighting.yaml`):
- **Occupancy Sensor** - an entity/device/area/floor/label `target`
  selector, added 2026-08-13, replacing the old single-entity Motion
  Sensor input entirely (not kept alongside it - an initial dual-input
  design was rejected as unnecessary complexity once it was clear
  motion-class sensors can't use the mechanism below anyway). Uses HA's
  native `occupancy` integration (2026.4+) - `occupancy.detected`/
  `occupancy.cleared` triggers and an `occupancy.is_detected` condition
  - which aggregates every occupancy-class `binary_sensor` within
  whatever's targeted, automatically, including sensors added to the
  area/device later. Confirmed against HA core source before building
  on it: both the trigger and condition filter strictly by
  `device_class: occupancy` (motion-class sensors are never picked up,
  even targeted directly - `filter_by_domain_specs` in
  `homeassistant/helpers/automation.py` applies the same check
  regardless of how an entity was reached), and both schemas require
  `target:` to be present, though every field inside it is individually
  optional - `occupancy_target` defaults to `{}`, not `null`, because
  `null` fails that schema outright. `occupancy.is_detected` is a
  native condition *type*, not a template function, so it's spliced
  into `condition:`/`action:` as real `condition: occupancy.is_detected`
  blocks (gated by `{{ occupancy_target | length > 0 }}`) rather than
  folded into the `occupied` Jinja variable, which now only covers the
  no-Occupancy-Sensor light-based fallback. A room with only
  motion-class sensors has no occupancy input to target today -
  Additional Triggers remains the manual escape hatch. Live-verified
  (see lesson 12 for a caching gotcha hit while testing): a real
  occupancy sensor's on→off transition correctly fired the new
  `occupancy.cleared` trigger and turned lights off, and a natural
  adaptive-tick condition trace showed `occupancy.is_detected`
  evaluating correctly against a real area.
- `apply_lighting` is the only thing `action:` dispatches for adaptive/
  scene lighting - no inline `light.turn_on`/two-step/RGB branching in
  the blueprint itself.
- The `adaptive_attr` trigger (dead code - a template trigger whose
  template referenced nothing but `trigger.*`, so it only ever
  evaluated once at startup and never again; also would have bypassed
  occupancy gating had it ever fired, since `condition:` didn't list it
  in the occupied-gated branch) has been removed.

**Deployment / operational notes:**
- pyscript is fully gone, both from this repo and the live host.
- The dev git-sync automation (polling this repo for new commits and
  re-running `link_into_ha.sh` automatically) has been removed - HACS
  handles install/update natively now, which was the whole point of
  this migration.
- `dashboard/preview.html` + `generate_preview_data.py` render the real
  Lovelace card against synthetic data without a running HA instance -
  regenerate data, then serve the **repo root** (not `file://`, not
  `dashboard/` - the card's `fetch()` needs HTTP and `preview.html`
  imports `../www/adaptive-lighting-curve-card.js`) and open
  `dashboard/preview.html`.
- Integration icon (`brand/`, `generate_icon.py`) exists and is
  generated from the real curve, but isn't visible in the HA/HACS UI
  yet - needs a `home-assistant/brands` PR (icons there only, HA/HACS
  don't read icons from arbitrary repos), not yet submitted.
- A handful of stale `service_not_found` repairs may still be showing
  under Settings → Repairs from the pyscript era - cosmetic only,
  dismiss by hand if still present; HA doesn't auto-clear a repair just
  because a later run succeeds.

**Motion Sensor replaced entirely by a native-HA Occupancy Sensor
target, at the user's request** ("newer HA automations let you just
say, pick a room... can we make that part of our blueprint?"). HA 2026.4
added a first-class `occupancy` integration - `occupancy.detected`/
`occupancy.cleared` triggers and an `occupancy.is_detected` condition,
each taking a `target:` (entity/device/area/floor/label) and aggregating
every occupancy-class `binary_sensor` within it automatically. Confirmed
directly against HA core source before building on it (not guessed):
`homeassistant/components/occupancy/trigger.py`/`condition.py` both
filter strictly by `device_class: occupancy` (motion-class sensors are
never picked up, even targeted directly - `filter_by_domain_specs` in
`homeassistant/helpers/automation.py` applies the same device_class
check regardless of how an entity was reached), and both trigger/
condition schemas require `target:` to be present
(`vol.Required(CONF_TARGET): cv.TARGET_FIELDS` in
`homeassistant/helpers/trigger.py`) though every field inside it is
individually optional, so an empty `target: {}` is valid config - it
just never matches anything. This is why the new `occupancy_target`
input defaults to `{}`, not `null`: `null` would fail that schema
outright regardless of whether the trigger is later disabled.

First implementation kept the old single-entity Motion Sensor input
alongside a new Occupancy Sensor target input (Occupancy Sensor taking
precedence if both were set), reasoning that motion-class sensors would
otherwise become unusable. The user pushed back immediately ("I don't
like having both occupancy and motion") - simplicity won over that edge
case, so Motion Sensor is gone entirely, not deprecated alongside. A
room with only motion-class sensors has no occupancy input to target
today (documented in `docs/BLUEPRINT.md`); Additional Triggers remains
the escape hatch for wiring one in manually. Only the "Living Room
Lights (New)" automation used this blueprint at the time, so nothing
else needed migrating.

`occupancy.is_detected` is a native condition *type*, not a template
function - it can't be folded into the existing `occupied` Jinja
variable the way a motion-entity `is_state()` check could, so it's
spliced into the `condition:`/`action:` trees as real
`condition: occupancy.is_detected` blocks (gated by a
`{{ occupancy_target | length > 0 }}` template check), while `occupied`
itself shrank to just the light-based "is anything already on" fallback
for rooms with no Occupancy Sensor configured. Same split for the
trigger side: `occupancy.detected`/`cleared` triggers replace the old
`motion_on`/`motion_off` template triggers outright, sharing their
trigger `id:`s (HA allows multiple trigger definitions to share an id;
whichever one fires sets `trigger.id`) rather than the rest of the
blueprint needing to know which mechanism is active.

Verified live, not just config-checked. Blueprint changes have no
pytest coverage, so verification meant re-importing into the real
instance and reading actual automation traces - and along the way,
`ha_import_blueprint(..., overwrite=true)` reported success while
silently still serving a stale, previously-cached copy from GitHub's
raw CDN (a branch-name raw URL can lag behind a very recent push by a
few minutes); confirmed by reading the file back with `ha_read_file`
after import rather than trusting the tool's own echoed response, and
worked around by importing from the exact commit SHA's raw URL instead
of the branch name, which resolved instantly since GitHub treats a
SHA-pinned raw URL as immutable and never serves it stale. Repointed
"Living Room Lights (New)" at `occupancy_target: {area_id: living_room}`
and got two real traces for free during testing, not staged: a genuine
`binary_sensor.living_room_sensor_2_occupancy` on→off transition fired
the new `occupancy.cleared` trigger (`id: motion_off`) and correctly
turned the lights off, and a natural adaptive-sensor tick's condition
trace showed `occupancy.is_detected` evaluating `true` against the real
`living_room` area, correctly gating the tick through to
`apply_lighting`. `config_check` stayed valid and zero repairs appeared
throughout.

## Open question: dashboard card as a HACS plugin?

The integration half of this used to be an open question - now
resolved (see above). What's left open is just the dashboard card:
HACS's original/core use case is distributing custom Lovelace cards (a
"plugin"/"frontend" repository category, separate from "Integration"),
and `www/adaptive-lighting-curve-card.js` already fits that shape as-is
(single file, no build step, no external deps) - would just need its
own `hacs.json`-equivalent declaration. Confirmed (via a Home Assistant
integration-embedding-a-card gist) that an *integration* can also
bundle and self-register its own frontend resource at setup time
(`manifest.json` depending on `frontend`+`http`, a small
`JSModuleRegistration` class calling `lovelace.resources.async_create_item`)
- so this doesn't have to be a second HACS repository/category if it's
nicer to fold the card into `adaptive_lighting_helpers` directly.
Neither approach has been started; the card still deploys the old way
(manual Lovelace resource registration, see README's Installation
section) until this is decided.

## Testing

`pip install pytest && pytest` from the repo root. No Home Assistant
dependency for the test suite - `tests/conftest.py` puts
`custom_components/adaptive_lighting_helpers/` on `sys.path` and tests
import `curve`/`grouping` as bare modules (not through the package,
which would pull in `homeassistant` via `__init__.py`); `tests/fakes.py`
provides a fake `EntityLookup` so `grouping.py` is exercised with plain
dicts. CI runs this on push/PR (`.github/workflows/tests.yml`) across
Python 3.9 and 3.13.
