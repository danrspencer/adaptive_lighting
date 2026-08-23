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
- Override protection lives in `grouping.py`/`override_protection.py`,
  not in a trigger. A one-shot trigger-level check can't provide a
  standing invariant (lesson 5); the real check re-evaluates against
  live state on every call. See "Override protection" under Current
  status.
- **The blueprint knows phase names in three places** - `rgb_phases`,
  the four per-phase scene pickers, and the four per-phase exclude
  lists. A deliberate exception to "the blueprint doesn't know phase
  names", chosen explicitly because editing a Jinja template in the HA
  UI is poor ergonomics for something this simple. Precedence in both
  templated cases: the *template* wins, the per-phase picker is the
  fallback for what it doesn't cover.
- Why any of this stays in Jinja at all: HA `condition:` blocks cannot
  call a service - only `action:` steps can - so anything
  condition:-gating must stay template-based. These pieces are also
  compact and get real value from HA's native trace UI
  (`ha_get_automation_traces`), used heavily to diagnose issues live.

Blueprint input mechanics worth knowing:

- Inputs are grouped with `sections:` (HA 2024.6.0+). Nesting an input
  in a section does **not** change its name for `!input` purposes.
- The blueprint declares `homeassistant.min_version: 2026.4.0` - what
  the `occupancy.*` triggers require, not `sections`' lower floor.
- `adaptive_sensor` uses `entity: filter: [{integration: ..., domain:
  sensor}]` - the `filter:` list is the only documented shape combining
  `integration:` with `domain:`; don't mix a bare top-level `domain:`
  with a sibling `filter:` (same class of trap as lesson 14). Accepted
  trade-off: this hides a hand-rolled "bring your own sensor" entity
  from the picker, so `docs/BLUEPRINT.md` documents pointing at one via
  "Edit in YAML".
- **Input renames are breaking.** A stored input simply stops matching
  any input the blueprint declares, so every already-migrated room
  automation needs the old key removed outright, not left blank, as
  part of deploying a rename.

**Lives in `custom_components/adaptive_lighting_helpers/` (a standalone
HACS integration - see Current status for the services):**
- Reachability filtering, multiplier bucketing, the tolerance-based
  "already at target" check, override protection, and
  two-step-vs-combined / RGB-vs-colour-temp label routing
  (`grouping.py`). This was the genuinely gnarly part - nested namespace
  loops, nothing pytest-testable in Jinja, and a real correctness gap
  (exact-match comparisons that silently stopped skipping for any bulb
  with device-side rounding quirks).
- Day-phase brightness/Kelvin curve math (`curve.py`) - not because it
  was complicated, but because it's a small, reusable, independently
  useful piece of logic that belongs as a documented service.
- Scene-coverage gap filling (`scenes.py`) - explicitly generic;
  nothing about it is specific to lighting.

All services are deliberately written and documented as standalone
tools, useful to anyone building their own automation, not just to the
blueprint in this repo. Keep them that way.

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
   one-shot trigger-level check (the blueprint's old `manual` trigger,
   context.user_id-based, since removed entirely - see the
   architectural-split note above) is not the same as a standing
   invariant later code respects - it only blocked the one automation
   run where it fired, not a later independent `adaptive` tick. Fixed
   properly in `grouping.py`'s `EntityLookup` (originally
   `manually_set()`, later renamed `externally_set()` when the
   underlying check moved from `context.user_id` to `context.id`
   equality - see "Current status"): instead of remembering that an
   override happened, it re-checks the entity's *current* state on
   every call - room-empty, light-off, and device-recovery release
   conditions all fall out for free from that. The one piece that
   *does* now need persisted state, contrary to this lesson's original
   framing, is knowing what to compare the current state against -
   `write_tracking.py`'s `Store`-backed record of what context.id this
   integration itself last wrote each entity with. The lesson still
   holds where it always mattered: a trigger's one-shot firing is not a
   substitute for a check performed fresh on every tick.

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

16. **A blueprint input with no `default:` is required, regardless of
    "(Optional)" in its own `name:`.** Adding four new entity-selector
    inputs (`morning_scene`/`day_scene`/`evening_scene`/`night_scene`)
    without a `default:` key broke every one of the 15 dependent room
    automations on the very next re-import - none of them set these new
    inputs (they're meant to be optional), so HA's blueprint
    substitution failed to generate any of them at all:
    `Failed to generate automation from blueprint: Missing input
    day_scene, evening_scene, morning_scene, night_scene`, confirmed via
    the live error log (`ha_get_logs(source="error_log")`), not just
    suspected. `ha_get_overview`'s `repair_count` going from 0 to 15 in
    one step was the first signal - all 15 `validation_failed_blueprint`
    repairs, all created within the same second. Every other optional
    input in this blueprint already had an explicit default (`""`,
    `"{{ {} }}"`, `[]`) - these four were the only ones missing it,
    added in the same change as several that did have one, which is
    presumably why it wasn't caught in review. Fixed with `default: null`
    on each; the downstream Jinja already treated an unset value
    correctly (falsy, falls through to a fallback branch) - only the
    blueprint schema itself was wrong. Restored live service by
    importing directly from the fix branch's own commit SHA rather than
    waiting for a PR merge - a pushed commit is immediately fetchable by
    SHA regardless of merge state, and this is a case where minimizing
    outage time mattered more than the normal branch-then-PR-then-merge
    sequencing.

## Current status

Everything below describes how the system works *now*. Per-change
history lives in git; this file only carries what stays true, plus the
decisions and constraints that aren't recoverable from the code.

### Services (`custom_components/adaptive_lighting_helpers/__init__.py`)

Seven, all unit tested and confirmed working live. Full field contracts
in `docs/HELPERS.md` and `services.yaml` - not repeated here.

- `compute_lighting_groups` / `compute_curve` / `compute_scene_coverage`
  - pure planners, no side effects.
- `apply_lighting` - the only side-effecting one; wraps the same
  grouping logic and issues `light.turn_on`/`turn_off`. Takes
  `brightness`/`color_temp_kelvin`/`rgb_color` as **plain values**, not
  a sensor entity_id. It read a sensor internally once; that was
  reverted deliberately, and the reversal is meant to stick - entity
  *selection* happens via the blueprint's `adaptive_sensor` regardless
  of which layer reads the attributes, and voluptuous's own required-
  field validation gives the same hard failure the internal read was
  added for.
- `check_ownership` / `record_ownership` / `clear_ownership` - override
  protection exposed standalone, for callers that want it without any
  curve/brightness logic. `clear_ownership` is the manual escape hatch
  for a light stuck `overridden`.

`rgb_color` on both `apply_lighting` and `compute_lighting_groups`
accepts an explicit `None`, not just an omitted key (`vol.Any(None,
...)`): the blueprint templates it unconditionally from a sensor
attribute that a hand-rolled "bring your own sensor" entity is free not
to publish, so it renders a literal null.

### Override protection

Each tracked entity carries two claims - `confirmed` (a write an earlier
call observed landing) and `pending` (the most recent attempt). The full
model, and why two rather than one, lives in `write_tracking.py`'s module
docstring; the decision table lives in `override_protection.classify()`;
the user-facing contract lives in `docs/HELPERS.md`. Three consumers
share that one table - `grouping.py`'s `externally_set()`, `sensor.py`'s
diagnostic status, and `check_ownership` - deliberately, because they
previously drifted.

Facts worth knowing before touching it, each verified against HA core
rather than assumed:

- `context.id`, not `context.user_id`: every service call in one
  automation run shares that run's context (`helpers/script.py`), so
  user_id can't tell our write from another automation's.
- `Entity._context` expires 5 seconds after the service call that set it
  (`core.py`), so a device whose confirmation takes longer reports back
  under an unrelated context while echoing exactly what was asked for.
  That's why claims also record a `target` and `classify()` falls back
  to comparing values against *either* claim's target.
- Zigbee bulbs speak **mireds**, and HA's Kelvin↔mired conversions are
  both lossy `floor()`. Two Kelvin values flooring to the same mired are
  indistinguishable to the device, so `_color_temp_matches` treats them
  as equal on top of the plain tolerance.
- A bulb's advertised `min/max_color_temp_kelvin` is **not always
  honest** - `light.utility_spot_1` advertises max 4000 and reports
  5813. So `_already_set` accepts the raw target *or* the range-clamped
  one, never only the clamped one.
- `force` and omitting `owner_id` both bypass the check, but differ:
  `force` still claims the write for later calls to recognise, omitting
  `owner_id` leaves it unclaimed.

**Known limitation, not solved.** Once `classify()` returns `overridden`,
`build_groups()` excludes the entity from every group, so nothing ever
records a fresher `pending` for it - and on a ramping curve its recorded
target only gets staler, so the value-rescue can't recover it either.
`clear_ownership` (and the card's Clear button) is the escape hatch.
Revisit properly if it keeps recurring; the underlying rot is that an
excluded entity never gets a refreshed claim.

### Multi-sensor schedule architecture

The config entry registers services only and carries no schedule.
"Add Integration" creates **zero devices and zero entities** - that's
deliberate: HA's own "integration added" dialog
(`step-flow-create-entry.ts`) shows an unconditional device-rename +
area-picker form whenever a flow creates at least one device, with no
way to suppress it, and that dialog is also the only place HA ever
auto-renames entity_ids to match a device name. Auto-seeding a device
therefore popped a rename prompt for something the user hadn't asked
for, and a later rename via Settings → Devices silently didn't
propagate to entity_ids.

Every schedule is a named "sensor" subentry (Settings → Devices &
Services → Add Sensor). `schedule_instances(entry)` in `coordinator.py`
is the single place enumerating them; every other module iterates its
output. Each subentry gets its own device, and every entity uses
`has_entity_name=True`, so renaming the device renames every entity's
displayed name for free.

Per sensor:

- `sensor.<slug>_adaptive_lighting` - state is the phase name;
  `brightness`/`color_temp`/`rgb_color`, today's boundary timestamps,
  and `attributes.points` (289 samples, what the dashboard card reads)
  all live on this one entity. `points` is too large for the recorder,
  which `_unrecorded_attributes = frozenset({"points"})` handles - a
  plain per-attribute-name class field, needing no separate entity.
- `select.<slug>_adaptive_lighting_phase` - manual phase override,
  self-clearing at the next natural phase boundary unless
  `switch.<slug>_sticky_phase_override` is on. Implemented by comparing
  against the phase computed at override time on every refresh, not a
  timer.
- Five `time.*` boundaries and eight `number.*` curve values, as live
  `entity_category: config` entities.

**Config lives in entity state, not `subentry.data`** - `coordinator.py`
reads `hass.states.get(...)` for each. Writing back into `subentry.data`
was rejected: every subentry data change triggers a full entry reload,
recreating every coordinator and entity just to tweak one number. A
genuinely-missing entity falls back to the same default the entity
itself will report moments later, so `phase_at()` never sees a missing
boundary.

### Curve math (`curve.py`)

Every brightness/Kelvin literal is a keyword-only parameter with a named
default (`DEFAULT_CURVE_VALUES`, `DEFAULT_SCHEDULE_HOURS`). Non-obvious
facts:

- The brightness fade's span is **1.6× the nominal evening-to-night
  window**, not 1:1 - kept as a ratio so a custom brightness range
  keeps the same timing shape.
- The Kelvin evening tail is
  `evening_kelvin + (night_kelvin - evening_kelvin) * t`, continuous by
  construction.
- `day_phase` is a **parameter**, not derived from `now_ts` -
  `coordinator.py` passes a manually-overridden phase alongside the real
  time, so phase and instant can legitimately disagree. Every ramp
  clamps its interpolation factor for that reason.
- `kelvin_to_rgb` uses round-half-up (`math.floor(x+0.5)`), matching the
  dashboard card's `Math.round`, not Python's banker's rounding.

**Do not re-add `night_floor_kelvin` or `kelvin_rgb`.** Both were cut
deliberately at the user's direction. RGB is just the Kelvin→RGB
conversion of `kelvin`; there is no separate RGB curve.

### Blueprint (`blueprints/automation/danspencer/adaptive_lighting.yaml`)

**`room_target`** is a single entity/device/area/floor/label `target`
doing double duty: lights within it are controlled, occupancy-class
`binary_sensor`s within it govern occupancy via HA's native `occupancy`
integration (2026.4+). That integration filters strictly by
`device_class: occupancy` - motion-class sensors are never picked up,
even targeted directly. Both `occupancy.*` schemas require `target:` to
be present though every field inside is optional, which is why
`room_target` defaults to `{}` rather than `null`.

`room_target` is resolved **once**, into `target_named_entities` +
`target_expanded_entities`, which the three consumers filter:
`resolved_entities` (lights), `room_occupancy_entities`
(occupancy-class binary_sensors), `scope_entities` (scene scope). The
halves are kept apart because a *directly named* light also pulls in its
device's siblings while the device/area halves already return those -
merging them would silently widen scene scope. Entity/device/area only;
floor/label aren't resolvable in hand-rolled Jinja, a pre-existing gap
shared with the native trigger path.

`room_occupancy_entities` exists only to decide *whether* the room has
an occupancy sensor at all. That matters because
`occupancy.is_detected`'s `any`-across-target semantics are vacuously
**false** over a target matching zero entities - so a light-only room
would otherwise permanently fail the condition.

**Triggers:** `adaptive` (state on the sensor, filtered `to:` the four
phase names so attribute-only ticks don't fire), `adaptive_tick`
(`time_pattern`, `!input update_interval`), `extra`, `motion_on` /
`motion_off` (`occupancy.detected`/`cleared`), `recovered`.

- `adaptive` needs the `to:` filter because a plain `state` trigger with
  no `from`/`to` fires on attribute-only changes; with any of those keys
  set HA rejects events where `old_value == new_value`.
- `adaptive_tick` exists because the curve is **flat** during Morning
  and Night, so the coordinator re-writes identical state and HA emits
  `state_reported`, not `state_changed` - `adaptive` goes silent
  entirely in those phases.
- `recovered` arms on "at least one of our lights is reachable", firing
  as the first bulb returns. The inverse ("none unavailable") is wrong:
  one permanently-unavailable orphan holds it false forever and disables
  recovery for the whole room. Accepted blind spot: one flaky bulb
  recovering beside healthy siblings doesn't move the aggregate;
  `adaptive_tick` mops that up.
- `recovered`'s `value_template` **cannot reference `trigger.*`** - HA
  renders it with only `trigger_variables` in scope, injecting `trigger`
  afterwards for the fired action only. An earlier version referenced
  `trigger.entity_id` there and could never fire at all.

**Jitter:** a `delay:` step, first in `action:`, renders
`range(0, update_jitter+1) | random` for `adaptive`/`adaptive_tick` and
`0` otherwise (HA short-circuits a zero delay synchronously). Spreads
writes so rooms sharing one sensor don't all command at the same instant.
`mode: restart` means a genuine trigger mid-jitter correctly preempts.

**`condition:`** only decides whether the tick is relevant at all. It
does **not** check occupancy: occupancy's only two jobs are turning a
room on (`motion_on` + `allow_turn_on`) and off once empty. Gating
adaptive ticks on it just meant an already-on light got skipped.

**`allow_turn_on` = `manual_run or trigger.id == 'motion_on' or
occupied`. This must never gain a new way to become true without the
user explicitly asking for it in so many words** - not implied, not
inferred as reasonable. The user's position on this is emphatic and
standing. Note `automation.trigger` from another automation counts as a
manual run (`trigger` is defined, `trigger.id` isn't), so anyone wanting
an event to light a room writes their own automation that calls it - no
blueprint input needed.

**Self-heal** shares `adaptive_tick` rather than its own interval. Its
eligibility checks sit in the `choose:` branch's own `conditions:`, so a
tick that doesn't qualify falls through to `default:` and reapplies
lighting normally. It stays **exclusive** of `default:` deliberately:
`entities_still_on`/`adaptive_target_entities` are computed once before
`action:` runs, so reapplying in the same run risks `apply_lighting`
turning a just-turned-off light straight back on. It requires occupancy
continuously clear for the full Wait time, checked with a hand-written
`now() - last_changed` template rather than
`occupancy.is_not_detected`'s native `for:` - that needs the recorder to
prime a duration already satisfied before the condition started
watching, which isn't reliable right after a restart.

**Phase names appear in three inputs** - `rgb_phases`, the four
per-phase scene pickers, and the four per-phase exclude lists. A
deliberate exception to "the blueprint doesn't know phase names",
explicitly chosen. All phase-keyed dict lookups use `.get(key, default)`,
never direct indexing: `states(adaptive_sensor)` can legitimately be
`unknown`/`unavailable`, and direct indexing would crash the whole tick.
`scene_template` wins over the per-phase pick whenever it returns a valid
scene; `brightness_multiplier_template`'s per-entity values likewise win
over the phase exclude lists (`dict(phase_base, **template_result)`).

**`0` and `null` multipliers are not the same thing.** `0` means "turn
this light off"; `null`/`false` means "hands off, something else owns
it" - excluded from the turn-off paths too, not just the adaptive step.
In Jinja as in Python `0 == false`, so membership tests like
`in [none, false]` silently swallow every `0`; the identity form
`multiplier is none or multiplier is sameas false` is required, mirroring
`grouping.py`'s own bucketing.

**`variables:` renders strictly top to bottom**, each key seeing only
those above it, and failures are silent (`x | length` on an undefined
name returns `0` with no log and no trace entry). The brightness-
multiplier chain sits near the top specifically because the turn-off
lists depend on it.

**Sensor reads are guarded before dispatch.** `brightness`/
`color_temp_kelvin` are plain `state_attr()` reads and `apply_lighting`
requires both, so an unavailable or renamed sensor would fail validation
every tick. The guard wraps that one action step rather than
`condition:`, because `motion_off` and self-heal turn lights *off* and
need nothing from the sensor.

**`device_class: occupancy` does not mean the sensor can tell whether
someone is still in the room.** Most sensors here are plain PIR motion
sensors: they report clear the moment motion stops, including while
someone sits still, then flip back on the next movement. Rapid on/off
flapping is normal, not a fault. `no_motion_wait` exists to turn "motion
stopped a moment ago" into a usable proxy for "the room is empty". Only
a couple of sensors here are genuine mmWave presence sensors, and the
blueprint can't tell the two apart - both just present as
`device_class: occupancy` - so Wait time is sized for the PIR case
everywhere.

Nightlight-style overrides need no dedicated mechanism: a template
`binary_sensor` with `device_class: occupancy`, named directly in
`room_target`, gets full native `occupancy.*` support, since the
trigger/condition machinery only looks at entity state, not origin.

### Standing decisions - don't re-propose without new information

- **Extracting target resolution into a service or a Jinja macro.**
  `condition:` can't call services and runs before `action:`; a
  `custom_templates/*.jinja` macro needs a full HA restart to load
  (lesson 2) and would add a manual install step to an otherwise
  self-contained blueprint; registering a global Jinja function means
  monkey-patching the shared template engine. Deduplicating *within*
  `variables:` has none of those blockers and is what's actually done.
- **Condition/action-selector inputs replacing `scene_template` /
  `brightness_multiplier_template`.** Investigated properly against
  `blueprint/models.py` and `annotatedyaml`. A blueprint input's
  `default:` cannot reference another input's value (the `blueprint:`
  key is discarded before substitution). Brightness has no viable
  selector at all - it returns a *value*, which neither `action` nor
  `condition` can produce. Scene handoff is convertible only by giving
  up gap-fill, since a native condition evaluates structurally too late
  to feed the single Jinja pass that computes it.
- **`activating_triggers`** (a second Additional Triggers input allowed
  to turn lights on). Built, shipped, then reverted: "added complexity
  for something that someone can just do via another automation".
- **`night_floor_kelvin` / `kelvin_rgb`** - see Curve math above.
- **An opt-out for the two-step repair** - HA's issue registry already
  provides Ignore, and an ignored issue survives version bumps.
  Documented in `docs/HELPERS.md` rather than reimplemented.

### Parked: scene handling in `apply_lighting`

Not implemented; recorded so it isn't re-derived. Two designs:

1. **Straight port** - `apply_lighting` gains optional
   `scene_entity_id`/`scope_entities`, calls `compute_scene_coverage`
   internally, then `scene.turn_on` plus dispatch on
   `uncovered_entities`. Known cost: loses the `condition:`-level
   suppression that stops the tick running while a scene owns the room.
   Accepted as fine if picked up.
2. **Bigger idea** - read a scene's *stored* per-entity values and feed
   them through the grouping/multiplier pipeline, so a brightness
   multiplier could scale a scene's own brightness (which it explicitly
   cannot today). `ha_config_get_scene` does expose full per-entity
   attributes, but: a scene captures whichever colour mode was active
   when recorded (`xy`/`hs`/`color_temp` can all appear in one scene,
   and `apply_lighting` understands only colour-temp and RGB), reading
   stored scene config is a less-trodden surface than the
   `hass.states`/registry trio used everywhere else, and scenes can
   carry `effect` and non-light domains. Treat as its own decision, not
   a prerequisite for (1).

### Two-step transition detection

`two_step.py` is pure, `two_step_check.py` is the registry adapter,
`repairs.py` is the fix flow. Case-insensitive globs matched against
`"<manufacturer> <model>"`. The options field is **seeded with the
shipped defaults and holds the whole list** - a saved value replaces
them outright, so deleting a shipped pattern takes effect. An
empty/whitespace field falls back to the defaults, so clearing the box
can't silently disable detection.

**Accepted trade-off:** once a user saves the field they own it, and a
later release adding a newly-discovered bulb won't reach them. Chosen
for consistency over reach; PR updates still reach every install that
hasn't customised it.

The fix applies the label to the **device**, not the entity -
`grouping.py` accepts either, but device survives entity renames and
covers every light entity the device exposes. It also *creates* the
label if absent, which is what guarantees the `label_id` is right, since
HA derives the id from the name at creation.

### Deployment / operational notes

- **Integration**: HACS. `update_information` then `download`, confirm
  the deployed file matches the merge with `ha_read_file` before
  restarting (see lesson 12), then restart.
- **Blueprint**: `ha_import_blueprint` with `overwrite=true`, pinned to
  a commit SHA rather than a branch (lesson 12). Note lesson 13 - it
  installs under `danrspencer/`, not `danspencer/`.
- The two halves deploy separately, so a brief window where a restarted
  integration meets a not-yet-reimported blueprint is expected and
  self-resolves.
- **The dashboard cards ship inside the integration**
  (`custom_components/adaptive_lighting_helpers/www/`) and self-register
  via `async_setup` → `async_register_static_paths` +
  `add_extra_js_url`. `cache_headers=False` is deliberate: neither file
  has a versioned URL, so caching would trade a stale-deployed-file bug
  for a stale-browser-cache one. This replaced a separate symlink path
  that silently went stale for over a week.
- `scripts/link_into_ha.sh` was **deleted**, not fixed - once the cards
  travel with the integration there was nothing left for it to do, and
  untested deploy tooling nobody runs is a liability (lesson 7).
- **The integration icon must live inside the integration's own folder**
  (`custom_components/adaptive_lighting_helpers/brand/`, HA 2026.3.0+),
  not the repo root. `home-assistant/brands` no longer accepts custom
  integrations. Root `brand/` is authoring tooling only; the served PNGs
  need re-rendering by hand after a design change. HACS's own store icon
  ignores HA's local brands API ([hacs/integration#5171](https://github.com/hacs/integration/issues/5171));
  HA's native UI is unaffected.
- pyscript is fully gone from both this repo and the live host.
- `dashboard/preview.html` + `generate_preview_data.py` render the real
  cards against synthetic data with no HA instance. Serve the **repo
  root** over HTTP (`.claude/launch.json`'s `dashboard-preview`), not
  `file://` - the card's `fetch()` needs HTTP, and `file://` renders a
  static snapshot with no script execution. Verify by reading the
  rendered shadow DOM directly; screenshot capture has been unreliable
  here.

## Testing

`pip install pytest pytest-homeassistant-custom-component && pytest`
from the repo root (see CONTRIBUTING.md). Python 3.14 - the floor
tracks whatever pytest-homeassistant-custom-component's pinned HA
release requires, since this only ever runs inside a real HA install.
`pip install -e ".[dev]"` does *not* work here and isn't used anywhere:
the flat repo layout isn't set up for setuptools discovery and doesn't
need to be, since nothing is distributed as a Python package. `dev` in
`pyproject.toml` is a versions reference only.

Two layers:

- **Pure** (`test_curve.py`, `test_grouping.py`, `test_scenes.py`,
  `test_override_protection.py`, `test_two_step.py`,
  `test_services_yaml.py`). `tests/conftest.py` puts the component
  directory on `sys.path` so these import `curve`/`grouping` as bare
  modules rather than through the package (which would pull in
  `homeassistant` via `__init__.py`); `tests/fakes.py` provides a fake
  `EntityLookup` so `grouping.py` runs on plain dicts. Note these still
  need `homeassistant` importable - `override_protection.py` imports
  `homeassistant.util.color`, and pytest's own `testpaths` collects
  `tests/integration/conftest.py` regardless of which file you target.
- **Integration** (`tests/integration/`), real HA via
  [pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component).
  `test_services.py` exercises the registered services end-to-end,
  calling `async_setup_entry` directly rather than
  `hass.config_entries.async_setup()` - the latter resolves the
  manifest's `http`/`frontend` dependencies and pulls in the large
  `home-assistant-frontend` package for something these tests never
  touch. `test_blueprint.py` loads the real blueprint into a test
  automation and fires real triggers, mocking only `apply_lighting` /
  `scene.turn_on` / `light.turn_off`.

`test_blueprint.py` is the **only** layer that can catch a bug in the
blueprint's own trigger/condition/action wiring - syntactically fine,
wrong only at runtime, invisible to YAML parsing or an isolated
`ha_eval_template` check. Its classes mirror `docs/BLUEPRINT.md`'s
section headings so it reads as a spec of what the blueprint does.

`tests/integration/conftest.py` overrides the plugin's own
`hass_config_dir` fixture (which otherwise points at its bundled
`testing_config/`) to symlink this repo's `custom_components/` and
`blueprints/` into a throwaway `tmp_path`.

**Practices this repo relies on, worth keeping:**

- **Mutation-verify every behavioural change**: break the fix
  deliberately, confirm *exactly* the intended test(s) fail, restore.
  This has repeatedly caught tests that passed for the wrong reason.
  Commit before mutating - `git checkout <file>` to undo a mutation
  will silently discard uncommitted work in that file.
- Timing tests use the file-wide `frozen_time` fixture
  (`freeze_time(..., real_asyncio=True)`) and call `.tick()`/`.move_to()`
  on it. **Never open a nested `freeze_time`**, and never freeze without
  `real_asyncio=True`: plain `freeze_time` also mocks
  `time.monotonic()`, which is the clock asyncio's event loop uses for
  every timer, and a live loop does not tolerate that - it hangs, or
  fires timers wildly out of order.
- `async_fire_time_changed` fires the event that trips a `time_pattern`
  trigger but does **not** advance `now()`. A template comparing
  `now() - last_changed` needs frozen time ticked forward explicitly.
- Advance time relative to `utcnow()`, never to an absolute
  `.replace(minute=N)` - the latter can target a time in the past
  depending on real wall-clock alignment at run time.
- Two consecutive `hass.states.async_set` calls with identical values
  collapse into one `state_reported` event, and the second call's
  explicit `context=` is silently discarded. Echo a *slightly*
  different value when a test needs a real state change.
- The plugin's `mock_storage` caches a `Store` instance's first load
  and never refreshes it, so reading back a write made through a
  different `Store` for the same key needs a **fresh** instance. Never
  an issue in production, where exactly one tracker is created.
- A real entity-registry change triggers `two_step_check.py`'s
  5s-debounced watcher; flush it or the harness fails on a lingering
  timer.