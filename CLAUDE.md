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
- **The blueprint's `prefer_rgb_color_template` input is a deliberate,
  one-off exception to "the blueprint doesn't know phase names."** Its
  default - `{{ states(adaptive_sensor) in ['Evening', 'Night'] }}` -
  hardcodes the four phase-name strings `curve.py`/`sensor.py` produce,
  something every other blueprint input/condition/trigger up to this
  point deliberately avoided (target resolution, occupancy, scene
  handoff - none of it cares what phase it is). Accepted anyway,
  explicit user call: "I had been avoiding coupling the blueprint to
  the specific adaptive lighting phase names, but I think it's time."
  Still just a *default* - it's a template input like `scene_template`/
  `brightness_multiplier_template`, so anyone who doesn't want this
  coupling can override it with their own condition, or a flat
  `{{ true }}`/`{{ false }}`, same as before. Renamed from
  `prefer_rgb_color` (a plain boolean) since the value is now a
  template, matching the existing `_template`-suffix naming convention
  for optional template inputs - a room automation with the old
  `prefer_rgb_color: true` override still works unchanged (HA's
  `variables:` block only template-renders string values, so a stored
  literal boolean passes through as-is), just without the new
  phase-based default until that input's cleared.

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

13. **`ha_import_blueprint` derives the installed path from the GitHub
    repo *owner* in the URL, not from this repo's own blueprint folder
    name.** This repo's blueprint lives at
    `blueprints/automation/danspencer/adaptive_lighting.yaml` (no 'r'),
    but the actual GitHub account is `danrspencer` (with an 'r') - a
    mismatch that predates this file. Importing from this repo's GitHub
    URL installs to `danrspencer/adaptive_lighting.yaml` on the live
    instance, a *different* path from the `danspencer/...` one already
    in use, rather than overwriting it - `overrides_existing: false` in
    the response is the tell. The same "danspencer" vs "danrspencer"
    collision lesson 6 warns about, surfacing here through a tool's own
    path-derivation logic instead of a symlink. `blueprints/` is
    read-only through every available file-editing tool, so there's no
    way to fix the orphaned old path directly - repoint the automation's
    `use_blueprint.path` at the new, correctly-updated file instead, and
    leave the old one as a harmless (not domain-scanned, unlike lesson
    9's `.bak-*` incident) orphaned leftover.

14. **A `target:` selector's `entity:` sub-key has a different schema
    from the plain (non-target) `entity:` selector - the multi-filter
    list goes directly under `entity:`, with no nested `filter:` key.**
    `selector: entity: filter: [...]` is correct for a standalone entity
    selector (`EntitySelectorConfig.filter`), but the identical shape
    under a `target:` selector (`selector: target: entity: filter: [...]`)
    fails blueprint import outright with `extra keys not allowed` -
    `TargetSelectorConfig.entity` (`homeassistant/helpers/selector.py`)
    *is* the list of filters itself:
    `selector: target: entity: [{domain: light}, {domain: binary_sensor,
    device_class: occupancy}]`. Confirmed against HA core source before
    fixing, not guessed from the error text alone - caught immediately
    by a live blueprint import failing, not by any local validation
    (plain `yaml.safe_load` has no opinion on selector schemas).

15. **In a `sections`-view dashboard, a card doesn't inherit its
    section's full width just because the section itself is
    full-width.** Each section is its own 12-column grid, and only
    some card types claim all 12 by default (`heading` cards do); a
    nested `type: grid` card and a custom card without a
    `getLayoutOptions()` implementation don't, and render at whatever
    their own natural size is - about a third of the section, in
    practice - even with `column_span` correctly maxed out on the
    section around them. Caught live: `column_span: 4` on both floor
    sections measured correctly via `getBoundingClientRect()` (1120px
    of 1184px available), yet the curve card and every nested tile
    grid inside them measured only 368px - the section was genuinely
    full-width, its content just wasn't using it. Fixed with
    `grid_options: {columns: full}` on each of those cards
    individually (not on the section) - confirmed via the same
    `getBoundingClientRect()` check, now 1120px across the board. This
    is a general `sections`-view behavior, not anything specific to
    this project's custom card - documented in
    `home-assistant-best-practices`'s dashboard-guide.md under "Card
    Sizing and Responsive Layout" once found, but not something a
    plain `column_span` fix on the section makes you suspect exists.

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
  blueprint calls - see `docs/HELPERS.md`'s "Bring your own sensor"
  section for the full attribute contract (moved there from the README
  - the user flagged it as not belonging on the main landing page).
- RGB colour (`prefer_rgb_color`) is implemented and unit tested, and
  the *routing decision* is confirmed live - `compute_lighting_groups`
  correctly bucketed a real bulb into `combined_rgb` based on its actual
  `supported_color_modes`, not just test fakes. What's **not** yet
  confirmed live is `apply_lighting`'s own `rgb_color` dispatch call
  itself (the `light.turn_on` with `rgb_color` data) - no live sensor
  has exposed an `rgb_color` attribute yet to point `sensor_entity_id`
  at. Judged low-risk to leave that specific gap unverified for now,
  since it's the identical dispatch pattern already confirmed live for
  `color_temp_kelvin`, just a different data key - worth actually
  exercising once a sensor with `rgb_color` exists live.

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
- **`room_target`** - a single entity/device/area/floor/label `target`
  input doing double duty as both what to light and what governs
  occupancy (see the dated note below for the full design history and
  the real correctness gap caught while merging what used to be two
  separate inputs). Occupancy uses HA's native `occupancy` integration
  (2026.4+, HA core-confirmed to filter strictly by
  `device_class: occupancy` - motion-class sensors aren't picked up
  even targeted directly), aggregating every occupancy-class
  `binary_sensor` within the target automatically. Live-verified twice
  now - once before the light+occupancy merge (see lesson 12 for a
  caching gotcha hit while testing that pass), and again after (see the
  dated note below for a real selector-schema bug and a transient
  first-tick blip both caught during that second pass).
- `apply_lighting` is the only thing `action:` dispatches for adaptive/
  scene lighting - no inline `light.turn_on`/two-step/RGB branching in
  the blueprint itself.
- The `adaptive_attr` trigger (dead code - a template trigger whose
  template referenced nothing but `trigger.*`, so it only ever
  evaluated once at startup and never again; also would have bypassed
  occupancy gating had it ever fired, since `condition:` didn't list it
  in the occupied-gated branch) has been removed.
- **`occupancy.cleared` fires per-entity, not per-target** - with more
  than one occupancy-class sensor in `room_target` (the nightlight-
  override pattern described below, real motion sensor + override
  sensor both placed directly in the target), this trigger fires the
  instant *either* one goes from on to off, even while the other is
  still reporting occupied. The `motion_off` action branch used to act
  on that trigger unconditionally - confirmed live via
  `automation.bedroom_hall_lights`'s trace history (not just suspected):
  its nightlight override sensor stayed `on` continuously all night
  (Night phase active), yet the lights still turned off the moment the
  real motion sensor's own off-transition fired. Fixed by adding a
  `not occupancy.is_detected` re-check (the same aggregate check the
  `reconcile` branch already did) to the `motion_off` branch itself -
  if another sensor's still on, that branch's conditions fail and
  `default:` re-applies adaptive lighting instead, which is a harmless
  no-op if the light's already correct.

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
  imports `../custom_components/adaptive_lighting_helpers/www/adaptive-lighting-curve-card.js`)
  and open `dashboard/preview.html`.
- **Dashboard card now ships inside the integration and self-registers
  with the frontend - no manual Lovelace resource, no separate
  deployment path to drift out of sync.** Moved to
  `custom_components/adaptive_lighting_helpers/www/`; `manifest.json`
  gained `dependencies: ["http", "frontend"]`; a new `async_setup`
  calls `hass.http.async_register_static_paths` then
  `homeassistant.components.frontend.add_extra_js_url` once per domain
  setup. Verified against HA core source before using it, not the
  earlier "confirmed via a gist" guess this replaces: `add_extra_js_url`
  is a real, docstringed public API "to register extra js or module to
  load" for custom integrations, and is a cleaner fit than the
  originally-scoped `lovelace.resources.async_create_item` approach -
  no stored dashboard config to create or dedupe, just an in-memory
  registration that's naturally redone on every setup.
  `cache_headers=False` on the static path is deliberate - the file has
  no versioned URL, so aggressive browser caching here would trade the
  stale-deployed-file bug below for a stale-browser-cache one instead
  of actually fixing it.

  Prompted directly by a real live incident, not a theoretical
  cleanliness pass: the card served from the old root-level `www/`
  folder had been frozen since 2026-08-03 - stale for over a week,
  through this session's entire multi-sensor rework - while the
  integration itself stayed current via HACS the whole time, because
  the card's only deployment path was `scripts/link_into_ha.sh`
  (symlinking it), which nothing in this session's actual workflow ever
  ran. The old deployed version didn't even understand the `sensor:`
  config field, so two cards pointed at different sensors would have
  silently rendered identical, broken content. Confirmed live via
  `ha_read_file` before concluding it wasn't just a browser cache issue
  - the served bytes themselves were stale - then fixed immediately via
  `ha_write_file`, ahead of this permanent fix.

  **`scripts/link_into_ha.sh` has been deleted entirely**, not just
  fixed - once the card travels with `custom_components/adaptive_lighting_helpers/`
  (which the script already copied as a whole directory) and HACS
  handles updating that, there was nothing left for the script to do
  that the already-established GitHub-based deployment paths (HACS for
  the integration, `ha_import_blueprint` for the blueprint) didn't
  already cover - and this session never actually used it even once,
  which is exactly how the card went stale unnoticed for over a week
  in the first place. Kept as untested, easy-to-forget tooling it would
  have just been a liability going forward.
- **Integration icon fixed, not just added.** The icon originally
  shipped from this repo's root-level `brand/` folder, which HA never
  actually reads - a custom integration's bundled brand icon has to
  live inside the integration's own folder
  (`custom_components/adaptive_lighting_helpers/brand/{icon.png,icon@2x.png}`,
  a mechanism added in HA 2026.3.0, no `manifest.json` changes needed).
  The original plan to submit to `home-assistant/brands` instead is no
  longer viable at all, confirmed live via the GitHub API before
  documenting it - that repo has stopped accepting PRs for custom
  integrations. `brand/` at the repo root is now design/authoring
  tooling only (`generate_icon.py` + `icon.svg`, the source of truth);
  the served PNGs need re-rendering by hand into the integration
  folder after any design change (no scripted step for that yet).
  Separate, unrelated gap outside this repo's control: HACS's own
  store/dashboard icon has an open bug
  ([hacs/integration#5171](https://github.com/hacs/integration/issues/5171))
  that ignores HA's local brands API entirely - HA's native UI
  (Settings → Devices & Services) is unaffected by that bug.
- A handful of stale `service_not_found` repairs may still be showing
  under Settings → Repairs from the pyscript era - cosmetic only,
  dismiss by hand if still present; HA doesn't auto-clear a repair just
  because a later run succeeds.

**Motion Sensor and Light merged into one `room_target` input.** Occupancy
detection went through two prior shapes first - a single-entity Motion
Sensor, then a dedicated Occupancy Sensor `target` selector added
alongside it and immediately simplified down to replace it entirely
(the user rejected keeping both: "I don't like having both occupancy
and motion") - before the user's own framing ("at its simplest you'd
just choose a room") prompted merging that Occupancy Sensor input with
the separate Light input into one. `room_target` is a single
entity/device/area/floor/label target that does double duty: light
entities within it get controlled (`resolved_entities`/`scope_entities`,
filtered to `^light\.`), and any occupancy-class `binary_sensor` within
it governs occupancy, via HA's native `occupancy` integration
(2026.4+) - `occupancy.detected`/`occupancy.cleared` triggers and an
`occupancy.is_detected` condition, confirmed directly against HA core
source before building on it: both filter strictly by
`device_class: occupancy` (motion-class sensors are never picked up,
even targeted directly - `filter_by_domain_specs` in
`homeassistant/helpers/automation.py` applies the same check regardless
of how an entity was reached), and both schemas require `target:` to be
present (`vol.Required(CONF_TARGET): cv.TARGET_FIELDS`) though every
field inside it is individually optional, so an empty `target: {}` is
valid config - it just never matches anything. This is why
`room_target` defaults to `{}`, not `null`.

Nightlight-style overrides (forcing a room "occupied" regardless of
real motion, previously the user's own template-sensor workaround)
don't need a dedicated mechanism - a template `binary_sensor` with
`device_class: occupancy`, picked directly as an entity in `room_target`
(not swept in via area membership), already gets full native
`occupancy.*` support for free, since the trigger/condition machinery
only looks at entity state, not entity origin. A dedicated boolean-
override input (its own trigger, its own asymmetric on/off semantics,
condition/reconcile changes) was considered and explicitly rejected as
solving a problem this mechanism already covers.

**Real correctness gap caught before shipping, not after**: merging the
inputs removes the free "was occupancy configured at all" signal the
old separate `occupancy_target | length > 0` check provided (used both
to disable the occupancy triggers and to pick the condition block's
fallback branch) - `room_target` is basically always non-empty, since
it's also what's being lit. The naive fix (always trust
`occupancy.is_detected` unconditionally) is wrong, not just
theoretically: `occupancy.is_detected`'s default `any`-across-target
behaviour is vacuously **false** over a target matching zero entities,
so a light-only room (no occupancy sensor at all - an explicitly
supported, common configuration) would permanently fail the condition
and stop receiving adaptive ticks entirely, a real regression, not a
hypothetical edge case. Fixed with a new `room_occupancy_entities`
variable - the same entity/device/area resolution pattern
`resolved_entities` already uses, filtered to `binary_sensor` +
`device_class: occupancy` instead of `light` - used purely to decide
*whether* the room has an occupancy sensor at all (gating the
condition/reconcile branches), while `occupancy.is_detected` itself
still does the actual native detection. The occupancy *triggers*
(`occupancy.detected`/`cleared`) don't need the equivalent gating - a
trigger with nothing to track is inert, not vacuously wrong, since
there's no boolean being evaluated over an empty set - so those are
left unconditionally attached rather than re-adding an `enabled:` guard.
`resolved_entities`/`scope_entities`/the `manual` trigger's light
resolution all also gained an explicit `^light\.` filter on their
`entity_id` branch (previously safe to skip, since the old Light
input's picker could only ever select light-domain entities in the
first place - `room_target`'s picker can now also select occupancy
sensors directly, so a mixed entity_id list needs filtering before
either purpose consumes it).

**Deployed and confirmed live, 2026-08-13** (`pytest` 43/43 plus real
traces against "Living Room Lights (New)", repointed at
`room_target: {area_id: living_room}`). Two real things caught along
the way, neither hypothetical:

- **A genuine selector-schema bug, caught by the live import itself
  failing outright** - the first version wrote the two-domain filter as
  `entity: filter: [...]`, matching the plain (non-target) `entity:`
  selector's shape. `target: entity:` has a different schema
  (`TargetSelectorConfig.entity: EntityFilterSelectorConfig | list[...]`
  in `homeassistant/helpers/selector.py`) - the list of filters goes
  directly under `entity:`, no nested `filter:` key at all; the nested
  form isn't silently ignored, it's a hard `extra keys not allowed`
  error. Confirmed against HA core source before fixing, not guessed
  from the error text alone.
- **A transient false negative on the very first tick right after the
  automation reload**: `room_occupancy_entities | length > 0` evaluated
  `false` for `living_room` on the first post-reload trace despite the
  area demonstrably having two real occupancy sensors (confirmed
  correct on every subsequent tick, and reproducible standalone via
  `ha_eval_template` with the identical resolution logic) - looked
  exactly like a bug at first, but never recurred once the automation
  had been running a few seconds. Plausibly the same class of
  reload-timing gap lesson 2's `_time_ts()` incident was about (a
  registry/cache not fully warm immediately after a reload), just
  self-healing here since nothing latches a bad value the way a crashed
  coordinator refresh did there. Worth knowing as a real, if narrow,
  possibility - not fully root-caused, but not a design flaw in the
  merge logic either, confirmed by direct comparison of the failing and
  passing traces' condition results.

Once past that first tick, traces showed the full intended behaviour:
`room_occupancy_entities` correctly resolved both real occupancy
sensors in the area, and `occupancy.is_detected` was actually evaluated
(not short-circuited) and correctly reported the room as unoccupied,
matching real sensor state confirmed via `ha_get_history` moments
earlier. `resolved_entities`/`adaptive_target_entities` also confirmed
correct via a `skip_condition: true` manual trigger, resolving all four
real living-room lights with the right brightness multipliers.
`config_check` stayed valid and zero repairs appeared throughout.

**Auto-seeded "Default" sensor removed entirely - "Add Integration" now
creates zero devices, zero entities.** Prompted by the user noticing
their live "Default"-titled sensor's device had been renamed to "Ground
Floor" (Settings → Devices → rename, which only ever changes the
*displayed* name) but its entity_ids were still `sensor.default_*` -
tracked back to two real, HA-core-confirmed facts, not assumptions:
(1) HA's own "integration added" dialog (`step-flow-create-entry.ts` in
`home-assistant/frontend`) shows an unconditional device-rename +
area-picker form whenever a config flow creates at least one device -
no flag exists for an integration to suppress it - so auto-seeding a
device on "Add Integration" always triggered that popup for a device
the user hadn't asked to create yet. (2) That same dialog is the *only*
place HA auto-renames entity_ids to match a device name
(`getAutomaticEntityIds` + `updateEntityRegistryEntry`, called once at
that first-run moment) - a later rename via Settings → Devices never
touches entity_ids again, by design, the same way no HA integration
auto-propagates entity_id renames later (doing so would silently break
whatever already references the old ones). Confirmed by reading the
actual frontend source before concluding either point, not guessed.

Fix: `async_step_user` (the main entry) no longer creates any
subentry - just `async_create_entry(title=..., data={})`, exactly the
same "nothing to configure" shape the entry already had, minus the
auto-seeded device. `DEFAULT_SENSOR_NAME` removed - there's now exactly
one way to add a sensor (Add Sensor) and exactly one moment its name is
ever set (what you type there), so getting the name right the first
time actually matters, rather than a rename-later escape hatch that
only fixed how it *looked*, not what it was *called*.

**Live migration performed and confirmed the same session.** Before
deleting anything, all 14 config entities on the old "Default"/"Ground
Floor" subentry (5 `time.*`, 8 `number.*`, 1 `switch.*`) were checked
against their documented defaults - every one matched, so nothing
needed manually preserved. The subentry was then deleted
(`ha_remove_helpers_integrations`, `helper_type: config_subentry`) and
recreated via Add Sensor (`ha_config_set_helper`,
`subentry_type: sensor`) named "Ground Floor" properly this time - a
follow-up `ha_search` confirmed all 17 entities exist under the
`ground_floor` prefix, values still at defaults. Two dependents then
needed repointing, both confirmed done, not just attempted:
`automation.living_room_lights_new`'s `adaptive_sensor` input from
`sensor.default_adaptive_lighting` to `sensor.ground_floor_adaptive_lighting`
(confirmed via a manual `automation.trigger` producing a clean trace -
`state: stopped`, `execution: finished`, no error), and the
`lovelace/house-settings` dashboard's curve-card section from
`sensor: "default"` to `sensor: "ground_floor"` (confirmed via the
write tool's own `post_write_verified: true`). `config_check` stayed
valid throughout.

**Dashboard card title went through two designs before landing** - the
first (deriving a default from the sensor's own `friendly_name`) was
tried and explicitly superseded the same session, not layered on top
of. First pass: two identically-titled cards side by side (one per
sensor) were indistinguishable at a glance - both just said "Adaptive
Lighting Curve" - so the header was changed to read `friendly_name`
off the phase entity, since every sensor's device is named by the user
and every entity on it uses `has_entity_name=True`, so that name is
already sitting on an attribute already being fetched. That worked,
but once a real dashboard actually paired the curve card with a
heading card naming the same sensor (see `dashboard/adaptive-lighting-section.yaml`
below), the two headers just repeated each other - worse than the
original problem in a different way. Final design: `config.title`
absent defaults to a static **"Adaptive Lighting"** (not derived from
any entity - simple and predictable); `config.title` explicitly set to
`""` renders no header at all (`ha-card` treats a falsy `header` as
"no header"), for exactly the case a heading card above it already
names the sensor; any other string is used verbatim. The `friendly_name`
read and the caching of it were removed entirely - dead code once the
static default replaced it, not left as an unused fallback. The
computation is a small shared `cardHeader(config)` function used by
both the error and normal render paths, rather than duplicated inline
in each (a divergence between the two is exactly how the "Adaptive
Lighting Curve" vs "Adaptive Lighting" mismatch happened the first
time). `dashboard/preview.html` renders two cards side by side to
exercise both ends - one with no `title` (shows "Adaptive Lighting"),
one with `title: ''` (shows no header) - verified visually via the
Browser pane against the live preview server.

**`dashboard/adaptive-lighting-section.yaml` added, and deployed live**:
a fuller copy-paste dashboard section than `house-settings-card.yaml`'s
curve-graph-only snippet - the curve card (`title: ''`, since the
heading card above it already names the sensor - see above) plus the
phase override/sticky-override switch plus all 13 schedule/curve
config entities, laid out as heading-grouped tile grids. Still just a
find-and-replace-the-slug template (no integration-side dashboard
auto-creation exists - see the file's own header comment for why), and
still not the only option: each sensor's own device page (Settings →
Devices → the sensor's device) already shows the same entities grouped
for free, since they're tagged `entity_category: config` - the new
section file is for a main dashboard, the device page needs nothing
shipped or pasted at all. Deployed to the live `lovelace/house-settings`
dashboard the same session: the old curve-only "Adaptive Lighting
Curve" section (two bare `custom:adaptive-lighting-curve-card`s) was
replaced with two full sections built from this template, one for
`ground_floor` and one for `first_floor` (built via a `python_transform`
for-loop over both slugs rather than duplicating the section by hand,
since the config-subentry mode used for the migration above forbids
`FunctionDef` nodes in that sandbox - a plain for-loop is allowed,
a `def` is not). The unrelated "Times"/"Harrison Bedtime" sections
(the older, separate pre-migration schedule helpers - `input_datetime.*`,
`sensor.day_phase`) were deliberately left untouched - out of scope for
this change, not part of "the old version of this."

**Boundary labels moved from always-on chart text to the hover tooltip;
sections widened to full page width.** Two related complaints about the
same live deployment: the "Morning 06:00"/"Day 08:00"/etc. text above
the chart became illegible once real sections used a narrower
`column_span`, and the two floor sections (`column_span: 2` each, side
by side) left a wide dashboard mostly empty either side - "only uses
half the page." Fixed both, not just the one raised first:
- `www/adaptive-lighting-curve-card.js`: the four boundary `<text>`
  labels are gone - only the dashed vertical marker `<line>`s remain.
  The information didn't disappear, it moved to the chart's existing
  hover tooltip (`onMove`), which now leads with the phase name (a new
  `phaseAt()` mirroring `curve.py`'s `phase_at()` exactly - same four
  half-open-interval boundaries) and also reports sun-up/sun-down (a
  plain interval check against `sunriseTs`/`sunsetTs`, already computed
  once per render and closed over by `onMove`). `PAD_TOP` dropped from
  44 to 20 now that no text needs clearance above the chart, which
  incidentally makes the bars taller too. The unused `.boundary-label`
  CSS rule was removed along with the code that used it, not left
  behind dead.
- Both `dashboard/adaptive-lighting-section.yaml` and the live
  `lovelace/house-settings` dashboard: each floor's section
  `column_span` went from 2 to 4 (`max_columns` on this view) - full
  width, stacked one below the other rather than side by side - with
  the Schedule tile grid's `columns` bumped 3→5 (all five boundaries
  in one row) and the Curve tile grid's 2→4 (two rows of four instead
  of four rows of two), so the extra width is actually used rather
  than just stretching existing tiles wider. The template file's own
  header comment now says to widen the section to fill the row after
  pasting, rather than leaving that undiscoverable.

Verified visually via the Browser pane both ways: against the local
preview server (hover tooltip shows e.g. "Day · 12:00" / "Sun up" /
"255 bri" / "5759K" plus the colour swatch, confirmed at both a
midday and a night sample) and against the live `lovelace/house-settings`
dashboard (both floor sections now full-width, tile grids filling out
5-and-4-across). The card-code half of this (hover tooltip, dropped
labels) is not live yet as of this note - same as the title-default
change above, it ships once this branch's PR merges and HACS
update+restart runs; the dashboard-YAML half (section width, grid
`columns`) needed no card-code change and is already live.

## Testing

`pip install pytest && pytest` from the repo root. No Home Assistant
dependency for the test suite - `tests/conftest.py` puts
`custom_components/adaptive_lighting_helpers/` on `sys.path` and tests
import `curve`/`grouping` as bare modules (not through the package,
which would pull in `homeassistant` via `__init__.py`); `tests/fakes.py`
provides a fake `EntityLookup` so `grouping.py` is exercised with plain
dicts. CI runs this on push/PR (`.github/workflows/tests.yml`) across
Python 3.9 and 3.13.
