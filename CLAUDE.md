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
- The blueprint used to carry its own `manual` trigger (context.user_id
  on the *triggering* state change) as a one-shot immediate bail-out -
  removed entirely once it became clear it never actually protected
  anything: its own `condition:` unconditionally backed out of the run
  the instant it fired, so it was pure trace-log noise, not a second
  layer of protection. The only real, sustained protection has always
  lived in `grouping.py`'s `EntityLookup.externally_set()` (see lesson
  5) - and that check itself moved off `context.user_id` onto
  `context.id` equality against a persisted per-entity "what did we
  last write this with" record (`write_tracking.py`), specifically so
  it also catches a light set by *another automation* (e.g. one
  triggered directly by a physical button, which carries no
  context.user_id of its own either - identical to this integration's
  own writes under the old check, and the actual gap that prompted this
  change). User-driven redesign, not a bug fix: "check if the last
  thing that updated a bulb was the automation doing the checking"
  rather than "was it a human" - confirmed against HA core source
  (`helpers/script.py`) that every service call within one automation
  run shares the same context.id, and that a service call given no
  explicit `context=` gets a fresh, unrelated one - which is also why
  `apply_lighting`'s own nested `light.turn_on`/`turn_off` calls now
  explicitly pass `context=call.context` through (including both calls
  inside a two-step transition), where they previously didn't need to.
  `apply_lighting` also takes an optional `owner_id` string identifying
  the caller (the blueprint passes its own `this.entity_id`) so a
  *different* caller's write is recognised as external too, and omitting
  it entirely skips the check altogether as an explicit force/override
  path - see "Current status" for the full mechanism.
- Why any of this stays in Jinja at all: HA `condition:` blocks cannot
  call a service — only `action:` steps can — so anything
  condition:-gating needs to stay template-based. These pieces are also
  relatively compact and get real value from HA's native trace/debug UI
  (`ha_get_automation_traces`), used heavily throughout this project to
  diagnose issues live. Losing that observability isn't worth it for
  logic that isn't actually that bad.
- **The blueprint knows phase names in three places now - RGB, scene
  handoff, and brightness scaling - a deliberate exception to "the
  blueprint doesn't know phase names," explicit user call: "I had been
  avoiding coupling the blueprint to the specific adaptive lighting
  phase names, but I think it's time."** Went through two designs before
  landing here - first a single `prefer_rgb_color_template` (a Jinja
  template, matching `scene_template`/`brightness_multiplier_template`),
  then explicit per-phase selectors once the user flagged editing
  templates in the HA UI as not user-friendly for something this simple:
  - **RGB**: `rgb_phases`, a multi-select of phase names (default
    `[Evening, Night]`) - `{{ states(adaptive_sensor) in rgb_phases }}`.
    No template fallback - list membership is the whole mechanism, and
    needs no `.get()`/crash-guarding against `states(adaptive_sensor)`
    legitimately being `"unknown"`/`"unavailable"` the way a dict lookup
    would (see the scene/brightness bullets below).
  - **Scene handoff**: four optional per-phase scene pickers
    (`morning_scene`/`day_scene`/`evening_scene`/`night_scene`), added
    alongside the kept `scene_template`. **`scene_template` wins
    whenever it returns a valid scene** - the phase pick is the fallback
    for phases it doesn't cover. User-corrected precedence, not the
    initial design: a template handling something like "if the TV is
    on" needs to keep winning over a plain per-phase pick, the opposite
    of "explicit is the friendly default, template is the escape hatch."
  - **Brightness scaling**: four optional per-phase "lights to keep off"
    multi-entity pickers, added alongside the kept
    `brightness_multiplier_template` - each excluded light becomes
    multiplier `0`. Not a per-phase multiplier *number* (user: "I can't
    think of a case where you'd want an entire room dimmer") and not a
    paired light+number control either (no HA selector binds a value to
    a chosen entity). **The template's own per-entity values win over
    the phase exclude list on any collision** (`dict(phase_base,
    **template_result)`, template spread in second) - same
    user-corrected precedence as scenes. Traced against
    `dining_room_lighting` (illuminance-based, phase-independent
    override) and `kitchen_lights` (mixed phase-/illuminance-conditional
    logic in one template) to confirm this merge order leaves both
    provably unaffected even if their owner later also sets a phase
    exclusion for an entity the template already covers.

  All three phase-keyed dict lookups (`phase_scene`, `phase_exclude_lights`)
  use `.get(key, default)`, never direct indexing -
  `states(adaptive_sensor)` can legitimately be `"unknown"` or
  `"unavailable"` (before the coordinator's first refresh, or during a
  failed one), not just the four phase names, and direct indexing would
  crash the whole automation tick on a transient hiccup.

  Also: `adaptive_sensor`'s own selector changed from a plain
  `entity: domain: sensor` to `entity: filter: [{integration:
  adaptive_lighting_helpers, domain: sensor}]` (the `filter:` list form
  is the only documented shape combining `integration:` with `domain:` -
  don't mix a bare top-level `domain:` key with a sibling `filter:`
  list, same class of selector-schema trap as lesson 14). This is only
  possible to do unambiguously because the curve sensor was merged away
  first (see "Multi-sensor schedule architecture" below) - filtering to
  just this integration's sensors while two existed per instance would
  have left the same "which one do I want" ambiguity as before, just
  narrower. **Deliberate trade-off, explicit user call**: this filter
  also hides a hand-written "bring your own sensor" entity (see
  `docs/HELPERS.md`) from the picker UI, even though `apply_lighting`
  itself still accepts one - `docs/BLUEPRINT.md` documents pointing at
  one via the automation's "Edit in YAML" view instead.

  All of these (plus a fourth, `timing`, added shortly after - grouping
  the pre-existing `no_motion_wait`/`reconcile_interval`/
  `motion_on_transition`/`motion_off_transition`/`adaptive_transition`
  inputs that used to sit flat at the bottom of the input list, once the
  user liked the pattern enough to ask for it there too) are grouped via
  blueprint `sections:` (named, collapsible `input:` groups - HA
  2024.6.0+, confirmed via `home-assistant.io`'s docs, no local HA core
  checkout in this repo to verify against directly), which is also why
  this blueprint declares a `homeassistant.min_version` for the first
  time - `2026.4.0` (what the `occupancy.*` triggers already required,
  HA-core-confirmed elsewhere in this file), not `2024.6.0` (sections'
  own lower floor), since the blueprint's real requirement was always
  the higher one and had simply never been declared. Nesting an input
  inside a section doesn't change its name for `!input <key>` purposes -
  purely presentational, confirmed live by every existing `!input`
  reference continuing to resolve unchanged.

  All renames here are breaking, not backward-compatible: removing an
  input key doesn't leave the old value "still working" - HA's
  `variables:` block only template-renders *string* values, but the
  point is moot regardless, since a room automation's stored input
  simply stops matching any input the blueprint still declares. Every
  already-migrated room automation needs the old key removed outright
  (not left blank) as part of deploying a rename - `prefer_rgb_color` →
  `prefer_rgb_color_template` needed this cleanup across all 15 room
  automations; `prefer_rgb_color_template` → `rgb_phases` didn't, since
  no automation had set that input explicitly.

  **Condition/action-selector inputs for scene/brightness overrides -
  investigated properly, not pursued.** HA has `selector: condition: {}`
  and `selector: action: {}` types that surface the real visual
  condition/action-builder UI and produce native condition/action-config
  lists, not free text - real, and initially looked like a promising way
  to give `scene_template`/`brightness_multiplier_template` a
  no-Jinja-required builder. Pulled the actual substitution source
  (`homeassistant/components/blueprint/models.py` +
  `annotatedyaml`'s `input.py`, straight from `home-assistant/core` and
  PyPI, not guessed) to answer two concrete questions and settle this for
  good:
  - **A blueprint input's `default:` cannot reference another input's
    real value.** `inputs_with_default` builds its substitution map with
    one flat pass - `inputs_with_default[inp] = blueprint_input[CONF_DEFAULT]`,
    no substitution run over the default first - and even though the
    later `substitute()` call walks the *entire* blueprint data tree
    (technically including the `blueprint.input.*.default` subtree
    itself), that whole `blueprint:` key gets discarded
    (`combined.pop(CONF_BLUEPRINT)`) before the real automation config is
    assembled. So a literal `!input adaptive_sensor` placed inside
    another input's own default would never resolve to anything real -
    ruling out "pre-populate a condition builder already wired to
    whichever sensor the user picked," the specific idea that prompted
    this investigation.
  - **Brightness has no viable selector-based replacement, full stop.**
    `brightness_multiplier_template` returns a *value* (a dict of
    `entity_id → multiplier`) - neither `action` (a list of service
    calls) nor `condition` (a list of boolean checks) can produce that
    shape. Not a scoping choice, a hard type mismatch.
  - **Scene handoff is technically convertible, but only by giving up
    gap-fill.** A `condition:` selector's value can only be evaluated
    natively (a `condition:`/`if:` position) - no template function
    evaluates a raw condition-config list from Jinja. Today's
    `desired_scene` → `scene_covered_entities` → `scene_valid` →
    `scene_active` → `adaptive_target_entities` chain is one single
    Jinja pass in `variables:`, which is exactly what lets
    `scene_template` "gap-fill" (activate the scene, adaptively light
    whatever it doesn't cover). A native condition check happens
    structurally too late to feed back into that same pass, so a
    condition-selector override could only be an early branch that, when
    triggered, takes over the *entire* tick with no adaptive fallback
    for lights it doesn't cover - a real behavioural regression from
    `scene_template`'s current gap-fill.

  Presented both findings to the user directly; explicit call: not worth
  it. `scene_template`/`brightness_multiplier_template` stay exactly as
  they are. Don't re-propose without new information changing this
  trade-off - same standing this file already gives the earlier
  "Considered and explicitly rejected: extracting target resolution"
  decision.

**Lives in `custom_components/adaptive_lighting_helpers/` (a standalone
HACS integration, four services - see "Current status" for the current
contract of each):**
- Reachability filtering, multiplier bucketing, the tolerance-based
  "already at target" check, externally-set protection, and
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
(`resolved_entities`/`scope_entities`, duplicated twice in the
blueprint - was three times until the `manual` trigger's own copy went
away with the trigger itself, see the architectural-split note above;
still structurally unfixable for the remaining two since triggers and
conditions can't call services either). Two blockers, either of which
alone would kill it: (1) `condition:` reads `resolved_entities`
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
- **Override detection redesigned from `context.user_id` to
  `context.id` equality, unit tested and now confirmed live.**
  User-driven: the old check ("was this light's current state set by a
  real person") missed a real gap - another automation (e.g. one
  triggered directly by a physical button, carrying no
  `context.user_id` of its own either) setting a light looked identical
  to "nothing happened" and got silently overwritten on the next tick.
  The fix reframes the question as "did *we* (adaptive control) make
  the last change" instead of "was it a human": `write_tracking.py`'s
  `LastWriteTracker` (Store-backed, survives restarts) records the
  `context.id` `apply_lighting` actually issued each write with;
  `grouping.py`'s `EntityLookup.externally_set()` (renamed from
  `manually_set()`) compares a light's *current* live `context.id`
  against that record - a mismatch (or no record at all) is the only
  signal, regardless of what caused it. Required threading
  `context=call.context` explicitly through every nested
  `light.turn_on`/`turn_off` call inside `apply_lighting` (including
  both calls of a two-step transition) - confirmed against HA core's
  `helpers/script.py` that a service call given no explicit `context=`
  gets a fresh, unrelated one, which would otherwise have made every
  one of `apply_lighting`'s own writes look externally-set on the very
  next tick. The blueprint's old `manual` trigger (a one-shot,
  context.user_id-based immediate bail-out) was removed entirely as
  part of this - it never actually protected anything on its own (its
  own `condition:` unconditionally backed out the instant it fired),
  so once the real check was fixed there was nothing left for it to do;
  see the architectural-split note above and lesson 5. **Confirmed live
  against `light.bedroom_hall_spot_1`**: a direct `apply_lighting` call
  brought it to the adaptive target (255 brightness / ~5025K); a
  follow-up `light.turn_on` called directly (not through
  `apply_lighting`, simulating an external source - its resulting state
  carried `context.user_id: null`, identical to what the old check
  would have waved through) changed its brightness to 90; a second
  `apply_lighting` call against the same target then left it at 90
  untouched - the exact failure mode this fix targets, confirmed fixed.
  Turning the light off and calling `apply_lighting` once more
  correctly resumed control (back to the adaptive target), confirming
  the existing "turning off ends the protection" behaviour (lesson 5)
  still holds under the new context.id-based check. Restart-survival of
  the persisted `write_tracking.py` record itself wasn't separately
  exercised beyond the restart already required to deploy this change -
  low risk, since it's HA's own standard `Store` helper, the same
  mechanism used throughout HA core.
- **`owner_id` added to `apply_lighting`/`compute_lighting_groups`,
  unit tested and now confirmed live.** User-driven follow-up, surfaced
  by testing the override-detection redesign above: with the context.id
  check in place, a *manual* call to `apply_lighting` (e.g. from
  Developer Tools, after deliberately changing a light some other way)
  got silently skipped too - the check has no notion of intent, only
  "did the last write match mine," so there was no way to say "yes I
  know, take it back anyway" short of turning the light off first.
  `owner_id` (an optional string, any caller can pass anything) fixes
  this two ways at once: omit it entirely and the check is skipped
  altogether - the explicit force/override path; pass it, and
  `write_tracking.py`'s per-entity record grows from just `context_id`
  to `{context_id, owner_id}`, so `externally_set()` now asks two
  questions in order - has *anything* touched the light since the last
  write (context.id, unchanged as the primary signal - this can't be
  owner_id alone, or a human editing the light between two ticks of the
  *same* automation would go undetected), and if not, was that write
  made under *this same* owner_id (a write from a different owner_id -
  a different automation, or an unkeyed force call - counts as external
  too, even though nothing about the light's own context changed).
  Pre-owner_id `Store` entries (a bare context.id string, not a dict)
  are dropped on load rather than migrated - harmless, since "no
  record" was already the accepted safe fallback for a missing entity.
  The blueprint passes its own `this.entity_id` (HA's built-in "this
  automation's own state" template variable, confirmed against
  home-assistant.io's templating docs before relying on it) as
  `owner_id` on its `apply_lighting` call - required as part of this
  same change, not a follow-up, since otherwise every room automation's
  regular tick would itself count as an unkeyed force call and the
  override protection just shipped would go dark immediately.
  **Confirmed live against `light.bedroom_hall_spot_1`**, deployed
  end-to-end (HACS download, full restart since Python changed,
  blueprint re-imported at the merge commit and confirmed to carry
  `owner_id: "{{ this.entity_id }}"`): seeded a write under
  `owner_id: "owner_a"` via `apply_lighting`, then confirmed via the
  read-only `compute_lighting_groups` planner (no risk of an extra real
  write) that a mismatched-target check under `owner_id: "owner_b"`
  excluded the light (`combined: []`) while the identical check under
  `owner_id: "owner_a"` included it (`combined: [...]`) - proving
  context.id-unchanged-but-different-owner is correctly treated as
  external. Separately confirmed with real writes that a context change
  (a direct `light.turn_on`, simulating an external touch) still blocks
  a subsequent `apply_lighting` call even when passed the *same*
  `owner_id` that originally claimed it - context.id remains the
  primary signal, exactly as designed - and that omitting `owner_id`
  entirely (force) then successfully wrote through regardless, bringing
  the light back to the adaptive target.
- RGB colour (`prefer_rgb_color`) is implemented, unit tested, and now
  **fully confirmed live end-to-end** - both the *routing decision*
  (`compute_lighting_groups` correctly bucketed a real bulb into
  `combined_rgb` based on its actual `supported_color_modes`, not just
  test fakes) and `apply_lighting`'s own `rgb_color` dispatch call (a
  direct `apply_lighting` call against `light.bedroom_hall_spot_1` with
  `prefer_rgb_color: true` landed the light in `color_mode: "xy"` with
  `rgb_color` matching `sensor.first_floor_adaptive_lighting`'s own
  `rgb_color` attribute exactly, confirmed via `ha_get_state` before
  turning the light back off to restore its prior state). The blueprint
  side (`rgb_phases`) was already confirmed separately - the boolean
  `prefer_rgb_color` value it computes was checked via automation traces
  - so both halves (which phases prefer RGB, and what actually happens
  when they do) are now live-verified, not just the first.

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

Per sensor, five entities:
- `sensor.<slug>_adaptive_lighting` - state is the phase name; current
  `brightness`/`color_temp`/`rgb_color` plus today's boundary
  timestamps (`morning_start`/`day_start`/`evening_start`/`night_start`/
  `evening_earliest`/`evening_latest`) all live as attributes on this
  one entity - no separate boundary-sensor entities (removed as UI
  noise; a `platform: state, attribute: phase` trigger on this entity
  already covers the automation case those existed for).
  `attributes.points` (the full day as 289 samples, what the dashboard
  card reads) lives here too - **not** on a separate curve sensor as it
  used to. That split existed only because `points` is too large for
  the recorder database (16KB state-attribute size warning); merging it
  in confirmed `_unrecorded_attributes = frozenset({"points"})` (what
  actually solves that) is a plain per-attribute-name class field with
  no dependency on a dedicated entity - the split was never load-bearing,
  just how it happened to be built originally. Removing it was prompted
  directly by the user disliking the second entity's existence, while
  designing the blueprint's `adaptive_sensor` selector (see "Blueprint"
  below) - filtering that selector to just this integration's sensors
  would otherwise still have left two per instance to choose between.
  **Breaking change** to the documented "bring your own sensor"
  contract (`docs/HELPERS.md`) for anyone who built a custom sensor
  mimicking the old two-entity split, or pointed a card's `entities:`
  override at a separate `curve:` entity - the card's default `curve:`
  key is gone, `points` now expected on the same entity as everything
  else.
- `select.<slug>_adaptive_lighting_phase` - manual phase override
  (Auto/Morning/Day/Evening/Night). Self-clears at the next natural
  phase boundary by default; `switch.<slug>_sticky_phase_override`
  disables that and keeps a pinned override until cleared by hand.
  Implemented by comparing against the phase computed at override time
  on every refresh, not a timer - the same "check live state fresh,
  don't invent a persisted expiry" pattern `grouping.py`'s
  `externally_set()` uses.
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
- **`recovered` trigger + scoped force-resync, unit-untestable (pure
  blueprint YAML) - shipped broken, then fixed and confirmed live; see
  the dated incident below for the full story.** User-caught gap, found by
  directly asking "if the zigbee drops off the network then comes back
  again - will that track as someone else having changed the entity?"
  while reviewing the owner_id feature above. Answer: yes, and this
  exposed a real inconsistency in what had just been written - the
  context.id-based check's own `externally_set()` docstring already
  correctly listed "a device regaining power under a fresh context" as
  one of the things *detected* as external, but both `docs/BLUEPRINT.md`
  and that same docstring's closing sentence claimed the opposite (a
  device recovering "isn't treated as an override" / "naturally stops"
  the protection) - true under the old `context.user_id` check (a
  reconnect carries no user_id either), never re-verified against the
  new `context.id` one when it was rewritten, and actually backwards:
  a reconnect's fresh context *is* what makes it look external, and
  nothing at the `grouping.py` layer ever un-marks it - a light stuck
  this way would never resync on its own. Both the doc claim and the
  self-contradicting docstring sentence are corrected as part of this.
  Fixed in the blueprint (the user's explicit call, not the Python
  service) via a new `recovered` trigger - a `platform: template`
  trigger mirroring the removed `manual` trigger's own room_target ->
  light-entity-list resolution boilerplate (confirming `trigger_variables:
  room_target: !input room_target` needed reinstating for it), firing
  when any of the room's lights transitions from `unavailable`/`unknown`
  to a real state. Deliberately **scoped to just `trigger.entity_id`,
  not the whole room** via a second, additional `apply_lighting` call
  (the existing room-wide call still runs normally first, with
  `owner_id` and no force, so it still protects everyone else) - an
  earlier draft of this fix would have force-resynced the *entire* room
  whenever any single light blipped, which would have clobbered a
  genuinely different light's real manual override in the same room
  just because something else nearby recovered from a drop; caught
  before implementing, not after. No native purpose-specific trigger
  covers "entity recovered from unavailable" (checked
  `automation-patterns.md`'s full trigger catalogue first, per the
  best-practices skill's own "check purpose-specific before templating"
  priority) - a plain `state` trigger's own `entity_id:` can't be
  computed from `room_target` (device/area) dynamically either, the
  same constraint that shaped the removed `manual` trigger, so a
  template trigger doing its own resolution was the only path. This
  scoped call originally omitted `owner_id` entirely (force via
  omission, the only mechanism that existed yet) - revised to pass
  `owner_id` *and* `force: true` together once a real gap in that
  approach was found afterward (see the `force` parameter bullet
  below), so the resync is properly attributed rather than left
  orphaned. Not unit-testable (pure blueprint YAML) - can't be exercised
  by unit tests or easily simulated (no service call can force a real
  device to report `unavailable` on demand the way every other case
  tested so far could be), so at the time this shipped, the trigger's
  own firing had only had its surrounding logic (config validity, the
  rest of the tick) confirmed live, not the transition itself - a gap
  that turned out to be hiding a real bug (see below, 2026-08-15).

  **2026-08-15: the `recovered` trigger never actually fired, in any
  room, since it shipped - found and fixed the same day.** User report:
  "I just turned the light on in both the living room and the study and
  they haven't updated at all," testing the exact power-cycle-recovery
  scenario this trigger exists for. Investigated by pulling real state
  history for both rooms' lights first, not assuming - confirmed via
  `ha_get_history` that `light.study_pendant` and
  `light.living_room_pendant_1`/`_2` had genuinely gone
  `unavailable` → `on` minutes earlier (this was a real recovery event,
  not a misunderstanding of what the trigger covers), then pulled both
  rooms' automation traces and found *zero* `recovered`-triggered runs
  in either room's history - only `reconcile`/occupancy-triggered runs,
  despite the light transitions happening well within the trace
  retention window.

  Root-caused by reading HA core's actual template-trigger source
  (`homeassistant/components/template/trigger.py`) rather than
  theorizing from the blueprint YAML alone, confirming a real
  architectural fact: **the `value_template` that a `platform: template`
  trigger uses to decide *whether to fire* is rendered with only that
  automation's `trigger_variables` in scope - `trigger` itself is not
  injected until *after* the template has already gone true, purely
  for the fired action's own variable context, built directly from the
  underlying `state_changed` event rather than from anything the
  value_template computed.** The `recovered` trigger's `value_template`
  referenced `trigger.entity_id`/`trigger.from_state`/`trigger.to_state`
  *inside itself* - `trigger is defined` was therefore always `false`
  there, making the whole boolean expression permanently `false`. The
  trigger could never arm, in any room, from the moment it shipped -
  confirmed directly via `ha_eval_template` reproducing the exact same
  "always false" result outside of any automation context. This is
  also why it was never caught by the "surrounding logic confirmed,
  not the transition itself" caveat above - the parts that *could* be
  tested (config validity, `just_recovered`, the scoped action) all
  genuinely worked; only the trigger's own arming condition was dead on
  arrival, and nothing short of a real recovery event plus trace
  inspection could have surfaced that.

  Fixed by replacing the `trigger.*`-referencing boolean with an
  aggregate condition instead - "none of this room's watched lights are
  currently `unavailable`/`unknown`" - which edge-triggers (false → true)
  exactly when the last outstanding light recovers, without needing
  `trigger` in scope at all.
  `trigger.entity_id`/`from_state`/`to_state` downstream (`just_recovered`,
  and the scoped force-resync action) needed no change - those already
  run in the automation's own `condition:`/`variables:`/`action:`
  context, which *does* get `trigger` populated correctly from the real
  event, independent of what the value_template itself computed.
  Deployed and confirmed: re-imported at the merge commit, deployed
  `value_template` content verified via `ha_get_blueprint` to carry the
  fixed aggregate check, and the fixed logic re-evaluated live via
  `ha_eval_template` against `room_target: {area_id: study}` - correctly
  resolved `light.study_pendant` and returned `true` (not currently
  unavailable), confirming the template itself is now sound. The next
  genuine power-cycle of either room will be the first real end-to-end
  firing - still not something a service call can force on demand (see
  above), so this remains the one piece of this feature that only a
  real recovery event, not a live tool call, can fully exercise.
- **`force` parameter added to `apply_lighting`/`compute_lighting_groups`,
  fixing a real bug in the two "omit `owner_id`" force paths shipped
  just before this - unit tested, live-deployment pending.** User-caught,
  by asking a verification question before trusting the design: "if we
  do one run without an owner id, will subsequent runs with one continue
  to work?" Traced through and the answer was no. `write_tracking.py`
  records whatever `owner_id` a write was made under, including `None`
  when it's omitted - so a forced write (no `owner_id`) got recorded as
  `{context_id, owner_id: None}`, and the *next* regular tick, calling
  with a real `owner_id`, compared `None != "automation.xxx"` and found
  a mismatch - `externally_set()` treated its own just-forced write as
  someone else's, indefinitely, since nothing ever corrected the
  orphaned record. This affected the `recovered` trigger's scoped call
  directly (it force-resyncs by omitting `owner_id`, so the very next
  regular tick would immediately re-flag that same light as external
  again) and would have affected the manual-run case below the same way
  had it shipped with the same "just omit `owner_id`" pattern.

  Fixed with two changes, not one - either alone leaves a hole:
  1. `force: bool = False` added to `EntityLookup.externally_set()`
     (checked right after the `is_state` guard, before `owner_id` is
     even inspected) and threaded through `build_groups()` - an
     explicit bypass that still accepts `owner_id` alongside it, so the
     write gets attributed properly instead of orphaned. `force` and
     "omit `owner_id`" are no longer the same mechanism: force lets a
     caller claim identity *and* skip the check in the same call;
     omitting `owner_id` is now the fully-anonymous variant, unclaimed
     by design.
  2. `externally_set()`'s owner-comparison step now treats a recorded
     `owner_id` of `None` as "doesn't count against anyone," not "counts
     against everyone" - `last_owner is None: return False` before the
     final `last_owner != owner_id` comparison. Needed regardless of (1)
     for full backward compatibility, since "omit `owner_id` entirely"
     remains a valid, already-shipped, already-tested force path in its
     own right (e.g. a one-off Developer Tools call with no interest in
     claiming anything) - without this, *that* path would still orphan
     records that a later `owner_id`'d caller could trip over.

  The blueprint was updated to use `force` properly instead of ever
  omitting `owner_id` to force something: the `recovered` trigger's
  scoped call now passes `owner_id: "{{ this.entity_id }}"` *and*
  `force: true` together (see the `recovered` bullet above, revised as
  part of this same change). A second, new case: **running the
  automation manually** (hitting "Run" in the UI, or calling
  `automation.trigger` directly) now also passes `force: "{{
  manual_run }}"` alongside its normal `owner_id` on the main room-wide
  call - the user's own follow-on ask right after owner_id shipped: "if
  it's ran manually it should also not pass in an owner id so it'll
  always update," which the verification question above caught before
  it could ship with the same bug the `recovered` trigger already had.
  **`manual_run`'s own detection needed a live-caught fix before this
  actually worked**: it first read `trigger is not defined`, on the
  assumption that a manual run leaves `trigger` itself undefined - live
  testing showed the trace still carrying `force: false` on a manual
  `automation.trigger` call, proving `trigger` *is* defined even then,
  just `trigger.id` isn't. Corrected to `not (trigger is defined and
  trigger.id is defined)` - the same two-part guard `just_recovered`
  already used, for the same reason - and reconfirmed live afterward:
  triggering `automation.harrison_s_pendant_lighting` manually
  (`automation.trigger`, no `skip_condition`) now produces a trace whose
  `apply_lighting` call carries `"force": true` (previously `false`),
  and the light ended the run at its correct adaptive target. A
  follow-up attempt to also re-confirm the fuller "resynced past a
  deliberately-mismatched external write" scenario on this same light
  was inconclusive, not failed - `light.harrisons_room_pendant` stopped
  accepting new `light.turn_on` writes entirely partway through testing
  (three calls with different explicit brightness/color_temp values all
  left `last_updated` frozen, confirmed via `ha_eval_template` reading
  live state directly, bypassing any tool-level caching) - consistent
  with this exact bulb's own history of `unavailable`/`on` flapping
  earlier the same day, i.e. real device/Zigbee flakiness unrelated to
  this fix, not evidence against it. The core claim - `force: true` now
  reaches `apply_lighting` on a manual run - is confirmed by the trace
  alone regardless.
- **A scene could never be activated by the plain periodic `adaptive`
  tick, in any room, from the moment scene handoff shipped - found and
  fixed 2026-08-15, same session as the `test_blueprint.py` expansion
  above.** Surfaced by the user directly questioning the doc-coverage
  audit's finding #5 ("activating a scene should be in the same flow as
  'apply adaptive lighting'... this bug should not exist"), not
  discovered independently - the audit itself only characterised it as
  a documentation gap, not recognising it as a real bug until pushed on
  it. Root cause: `condition:`'s adaptive/extra branch had a clause
  suppressing the *entire* tick - not just the scene-activation step,
  the adaptive-lighting dispatch too - whenever `trigger.id == 'adaptive'`
  and `scene_active` was already true, meant to avoid reactivating the
  same scene every single minute. But `scene_active` is computed fresh
  from *this* tick's own state, not "was it active last time" - so the
  very first tick where a phase-picked scene becomes eligible (the
  phase just changed) is *also* an adaptive-id tick, and got suppressed
  identically to the 500th. A room with continuous occupancy across a
  phase boundary (nobody leaving and re-triggering `motion_on`, nothing
  in Additional Triggers to fall back on) would never switch over to a
  configured phase scene at all - it would just keep receiving adaptive
  lighting indefinitely.

  Fixed by moving the concern out of `condition:` entirely, matching
  the user's own explicitly-stated mental model ("should we try to
  update the lighting? if yes: do we have scenes to apply? if yes apply
  them. now we apply adaptive lighting to the remaining lights") -
  `condition:` now only ever decides *whether this tick is relevant at
  all* (occupancy), never *what kind of relevant*. Scene handoff and
  adaptive dispatch both live entirely in `action:`, already coexisting
  safely there since `adaptive_target_entities` has always excluded
  scene-covered lights regardless of trigger type - the top-level
  suppression was solving a problem the downstream logic didn't
  actually have.

  Removing the suppression outright would have reintroduced the
  "reactivate every minute" concern it existed for in the first place -
  scenes carry none of `apply_lighting`'s own tolerance/override
  protections, so re-issuing `scene.turn_on` on every attribute-only
  tick (brightness ticking, same phase, roughly once a minute) would
  silently stomp a manual change to any of the scene's own lights
  within a minute. **User's own explicit call, after being shown this
  trade-off directly via `AskUserQuestion`**: still needed a guard, but
  "keep it as linear as possible" - resolved with a single self-contained
  variable, `scene_recheck_due`, consulted only at the point of the
  "Activate matching scene" action step itself (not threaded back into
  `condition:`): true for every trigger except a same-phase adaptive
  tick (`trigger.id == 'adaptive'` and `trigger.from_state.state ==
  trigger.to_state.state`) - extra/motion_on/manual/recovered are all
  comparatively rare, meaningful events already and always recheck,
  matching the precedent the blueprint already set for `extra` being
  exempt from the old (now-removed) suppression.

  Known, deliberately out-of-scope limitation this doesn't solve: a
  `scene_template` that returns a *constant* scene (no phase dependency,
  nothing wired to Additional Triggers) still can't activate via a pure
  adaptive attribute tick - `scene_recheck_due`'s phase-comparison only
  helps because phase-picked scenes and phase-reading templates are
  inherently tied to the sensor's own state string. This is the same
  "wire your template's real dependency to Additional Triggers"
  requirement the docs already describe, not a new gap - a
  no-dependency `scene_template` had exactly the same first-activation
  problem *before* this fix too (the old suppression blocked every
  adaptive tick unconditionally, not just phase-changing ones).

  Regression-tested directly: `test_valid_scene_activates_via_a_phase_change_and_adaptive_lighting_only_covers_uncovered_entities`
  triggers via a genuine phase transition (not a manual run, which
  would have masked this bug entirely) with the room continuously
  occupied throughout, confirming both the scene activates and adaptive
  lighting still covers the uncovered light in the same tick;
  `test_scene_recheck_is_skipped_on_a_same_phase_attribute_only_tick`
  confirms a subsequent same-phase brightness-only tick does not
  re-call `scene.turn_on`. `pytest` 27/27 in `test_blueprint.py` (83/83
  full suite), stable across repeated local runs.
- **Recovery-from-unavailable's own dedicated scoped-resync step
  removed entirely, and occupancy stopped gating already-on-light
  updates at all - 2026-08-15, same day as the scene-activation fix
  above.** Prompted by the user reading real automation traces and
  asking a basic architectural question: "why is recovery from
  unavailable its own step? this should just happen naturally right?"
  Went through several rounds before landing on a materially simpler
  design than what shipped that morning (the `recovered` trigger +
  `just_recovered` + scoped force-resync mechanism documented
  immediately above this entry).

  **A proposed intermediate design was explicitly and emphatically
  rejected by the user, and the rejection is a standing constraint, not
  just feedback on that one idea.** Investigating "why doesn't recovery
  just happen naturally," the blocker turned out to be real: a
  reconnecting device's own state report carries a fresh `context.id`
  (confirmed directly against `homeassistant/core.py` before relying on
  it - `async_set_internal` does `if context is None: context =
  Context(id=ulid_at_time(timestamp))`, and `Entity._context` is only
  set by `async_set_context()` during a service call, expiring after
  `CONTEXT_RECENT_TIME_SECONDS = 5`s - so a bare reconnect, not preceded
  by a recent service call, always gets a brand-new context with no
  owner_id attached). That fresh context makes `externally_set()` treat
  the reconnect as an override, same as any other change from outside
  this integration - which is *why* the scoped-resync mechanism existed
  at all. Proposed fix at the time: clear the write-tracking record on
  unavailable (uncontroversial - see below) *and* treat "whole room just
  came back from unavailable" as a new event equivalent to motion, i.e.
  eligible to turn lights on via `allow_turn_on`. **User's response, in
  full, verbatim, because this is a hard boundary going forward:**
  *"whow whow whow, did you just say that an entire room becoming
  available can now turn on? bevcause that is EXPLICTLY WRONG, we NEVER
  TRIGGER unless motion / occupied / manual run NEVER EVER THIS WILL NOT
  CHANGE UNLESS EXPLICITLY ASKED FOR!!"* `allow_turn_on`
  (`manual_run or trigger.id == 'motion_on' or occupied`) must never
  gain a new way to become true without the user explicitly asking for
  it in so many words - not implied, not inferred as "obviously
  reasonable," asked for.

  The user's own corrected framing, immediately after, is what actually
  shipped: *"if we trigger on a bulb leaving unavailable then we just
  hit our existing turn on guards, if its off then we don't do
  anything, if its on then we can update."* No new turn-on permission
  needed at all - `allow_turn_on` already handles "was the room
  occupied," and a light that reconnects already-on only needs its
  attributes *updated*, which was never gated by `allow_turn_on` in the
  first place (that variable only guards `light.turn_on` calls to
  currently-off lights).

  **A second, independent simplification followed from the user
  questioning the pre-existing occupancy-gating design itself**, not
  just the new recovery mechanism: *"why do we care if a room is
  occupied before deciding to update or not - we don't, we care if the
  bulbs are off or not, the only thing we DO care about reconciling on
  with regards to occupancy is when we're responsible for turning off,
  so if the room is empty AND we're past the time it should've turned
  off AND a bulb is still on, then turn it off."* Confirmed correct on
  review: occupancy's only two real jobs were always turning a room on
  (via `motion_on` + `allow_turn_on`) and turning it off once empty (via
  `motion_off`/`reconcile`) - gating the *adaptive/extra* ticks on
  occupancy never protected anything, it just meant an already-on light
  in a room the room's own occupancy sensor briefly disagreed with (or a
  light-only room with no occupancy sensor at all, see the
  `room_target` merge note above) got skipped for a tick it should have
  received. User confirmed both changes together: *"I think yes thats
  right... do this."*

  **Final three-part implementation, all shipped together:**
  1. `write_tracking.py`'s `LastWriteTracker` gained
     `async_start_listening(hass)` - a single hass-wide `state_changed`
     listener (not a per-entity subscription needing to track
     `apply_lighting`'s own entity set over time) that deletes an
     entity's `{context_id, owner_id}` record the instant it's observed
     going `unavailable`/`unknown`. Wired up in `__init__.py` via
     `entry.async_on_unload(write_tracker.async_start_listening(hass))`,
     right after the tracker loads. This is what actually closes the
     "reconnect looks external" gap - by the time the device reports
     its recovered state, there's no stale record left for the fresh
     context to mismatch against, so `externally_set()`'s existing "no
     record → free" branch (already there, see lesson 5's
     `EntityLookup`) picks it up with no new logic of its own.
  2. The blueprint's `condition:` block collapsed from three branches
     (occupancy-gated adaptive/extra, a `motion_on` efficiency check, a
     catch-all) down to two: `motion_on` still only proceeds if
     something in scope is actually off (unchanged, an efficiency
     check, not a permission check - `allow_turn_on` is what actually
     permits the turn-on); every other trigger (`adaptive`, `extra`,
     `manual`, `recovered`) now proceeds unconditionally. Occupancy is
     no longer read anywhere in `condition:` at all.
  3. The `recovered` trigger, `just_recovered` variable, and the scoped
     "force-resync just this light" action step (documented in detail
     immediately above this entry) were removed outright - not
     deprecated, deleted. The `recovered` trigger itself (a light
     leaving `unavailable`/`unknown`) is kept, but purely as a
     promptness signal - it just causes the *ordinary*, unscoped,
     room-wide `apply_lighting` call (unchanged: `owner_id: "{{
     this.entity_id }}"`, `force: "{{ manual_run }}"`, no per-light
     scoping) to run sooner than the next `reconcile` tick would have,
     rather than waiting up to 5 minutes. `script_transition`'s
     adaptive-transition bucket gained `'recovered'` alongside
     `'adaptive'`/`'extra'` so a recovered light fades in smoothly
     rather than snapping. The trigger's own aggregate arming template
     ("none of this room's lights are currently unavailable/unknown")
     needed no change - already correctly handles a single flaky bulb
     among reliable siblings, confirmed by trace-through, not just
     assumed.

  **Test-layer consequence, not an oversight**: with no more
  blueprint-level per-light scoping, the "a sibling under a genuine
  external override stays protected while the recovered light gets
  freed" guarantee no longer lives in the blueprint at all - it lives
  entirely in `write_tracking.py`/`grouping.py`. Coverage moved
  accordingly: `test_blueprint.py`'s old
  `test_only_resyncs_the_light_that_recovered_not_the_whole_room` was
  replaced with
  `test_recovery_joins_the_ordinary_room_wide_tick_not_a_separate_call`
  (asserts exactly one unscoped, unforced `apply_lighting` call covering
  the whole room), and two new tests were added to `test_services.py`
  instead, exercising the real service rather than a mocked one:
  `test_write_tracking_record_is_cleared_when_light_goes_unavailable`
  and
  `test_recovered_light_is_freed_while_an_unrelated_override_stays_protected`
  (two lights written under one context; one goes unavailable and
  reconnects under a fresh context and is confirmed freed; the other is
  externally changed without ever going unavailable and is confirmed
  still protected, via `compute_lighting_groups`'s `combined` list).
  `TestOccupancyDrivenOnOff` also gained
  `test_real_occupancy_sensor_reporting_unoccupied_still_updates_an_already_on_light`,
  directly regression-testing the second simplification. Full suite:
  `test_blueprint.py` 28/28, `test_services.py` 9/9.

  Docs updated to match: `docs/BLUEPRINT.md`'s "Occupancy-driven on/off"
  section now opens by stating occupancy's exactly-two-jobs scope
  directly; its "Override detection" section's "device regaining power"
  paragraph now describes the automatic record-clearing as the real
  mechanism (previously described the now-deleted scoped-force
  mechanism); the transition-duration table moved "a light recovering
  from a dropped connection" from the Motion On row to the Adaptive
  Transition row. `docs/HELPERS.md`'s `force: true` bullet dropped the
  now-inaccurate "resyncing a light that dropped off the network"
  example, and its closing paragraph now states the integration handles
  device-recovery automatically for *any* caller (not just the
  blueprint), since the fix lives in `write_tracking.py` itself.
- **The clear-on-unavailable listener above shipped with a real bug the
  same day it deployed: it wiped override protection for almost every
  managed light in the house on every plain HA restart, not just ones
  that had actually dropped off the network - found live, 2026-08-16,
  from a user report** ("I just turned off one of the kitchen pendants
  and dimmed the other, next time I looked they're both back on full
  brightness"). Investigated with real evidence before concluding
  anything - automation traces, state history, and logbook for both
  kitchen pendants, not assumption. Two genuinely different things were
  going on, and only one was a bug:
  - The fully-off pendant coming back on was **correctly explained by
    existing, intentional design, confirmed via live traces**: the user
    turned it off via the app (a real `context.user_id`-bearing
    `light.turn_off`), then ~90 seconds later the kitchen's own
    occupancy sensor genuinely transitioned off→on (a real `motion_on`
    trigger, confirmed via the automation trace), which is allowed to
    turn a currently-off light back on regardless of who turned it off
    - lesson 5's "turning off ends protection" is deliberate and
    long-standing, not something this session's redesign touched. The
    kitchen has two occupancy sensors covering one open-plan area
    (`binary_sensor.kitchen_sensor_occupancy` and
    `binary_sensor.extension_right_occupancy`), so real motion retriggers
    happen every few minutes there - an inherent, foreseeable
    consequence of the existing design in a busy/twin-sensor room, not
    a new regression.
  - The dimmed-but-still-on pendant resetting to full brightness had no
    such explanation, and this is the part that turned out to be a real
    bug. Root-caused by reading `write_tracking.py`'s
    `async_start_listening()` (shipped hours earlier the same day, see
    above): it deletes an entity's write-tracking record on *any*
    observed transition into `unavailable`/`unknown`, with no check on
    what the entity was *before* that. A HA restart routinely puts
    almost every entity through `unavailable`/`unknown` as its platform
    reconnects (confirmed directly: `light.kitchen_pendant_2`'s own
    logbook showed exactly this at the timestamp of that day's earlier
    restart) - indistinguishable, from the listener's point of view,
    from a genuine network drop. Once cleared, a record only comes back
    the next time that light actually *needs* a real write (brightness/
    colour outside tolerance) - a light that happened to already be
    correctly positioned right after the restart could go the rest of
    the day with no record at all, meaning the very next manual touch
    on it - context.id genuinely different, but with nothing to compare
    against - was waved through as "free to manage" (the same "no
    record → free" fallback that correctly handles a brand-new entity,
    firing here for the wrong reason) and silently overwritten on the
    next tick. This fully explains the observed timing: the pendant's
    last *real* write was well before the restart; nothing after the
    restart needed to touch it again until the user's manual dim, by
    which point its record was long gone.

  Fixed by requiring the transition to start from a genuine on/off
  state, not just end at `unavailable`/`unknown`: `old_state is None`
  (a fresh process's state machine has no history for the entity yet -
  its first-ever event) or `old_state.state in ("unavailable",
  "unknown")` (already mid-transition, e.g. `unavailable` → `unknown`
  before its first real value) now both leave an existing record
  alone. Only `old_state.state in ("on", "off")` transitioning to
  `unavailable`/`unknown` - a light that really *was* being tracked,
  now genuinely gone dark - clears anything, matching the mechanism's
  actual intent. The two existing tests (`light` going `off` →
  `unavailable` → `on`, and the sibling-protection test) were
  unaffected, since `off` is a real prior state and still correctly
  triggers clearing; a new
  `test_a_restart_style_unavailable_blip_does_not_clear_an_existing_record`
  reproduces the actual failure shape - `hass.states.async_remove()`
  before setting `unavailable`, matching a fresh process's blank state
  machine (`old_state=None`) rather than a real drop - and confirms a
  manual change made afterward is still correctly protected. Full
  suite: 87/87.
- **`activating_triggers` was added and then removed the same day
  (2026-08-16) - do not re-propose it without a concrete new reason.**
  A second Additional Triggers input whose entities were also allowed to
  turn currently-off lights on (a one-shot permission alongside
  `motion_on`), asked for to drive "some lights turn on at dusk". Built,
  tested, shipped and deployed - then the user reconsidered on reflection:
  *"its added complexity for something that someone can just do via
  another automation"*. Reverted in full: the input, the `activating`
  trigger id, its entries in `allow_turn_on` and the `condition:`
  efficiency check, four tests, and the docs.

  Worth keeping from that round, because it stays true and is the reason
  the feature isn't needed: **`automation.trigger` from another
  automation counts as a manual run here** - `manual_run` is `not
  (trigger is defined and trigger.id is defined)`, and a service-invoked
  run has `trigger` defined but no `trigger.id`. So a manual run gets
  `allow_turn_on` *and* `force: true`. Anyone wanting an event to light a
  room writes their own automation that calls `automation.trigger` on the
  room's automation - no blueprint input required. Confirmed live during
  that round: `automation.jacobs_room_switch` does exactly this, and the
  logbook shows its `automation.trigger` call turning `light.jacobs_pendant`
  from off to on.

  Also worth keeping: a plain `platform: state` trigger with no `to:`
  fires on **attribute-only** changes, so `sun.sun` is a trap for
  anything event-like - confirmed live, `last_changed` 15:00:34 vs
  `last_reported` 20:14:21, five hours of elevation/azimuth updates with
  no state change - and its state flips twice daily (dusk *and* dawn).

  The standing rule this briefly tested is unchanged and was never
  weakened: `allow_turn_on` must not gain a new way to become true
  without the user explicitly asking. It gained one, then gave it back.
- **A `null` brightness multiplier now means hands-off for *turning off*
  too, not just for the adaptive step - a real asymmetry that had been
  in place since multipliers existed, found live 2026-08-16.** Surfaced
  while investigating a user report that the kitchen lightstrip wouldn't
  come back on. That report turned out to have no bug in it at all (see
  below), but reading the strip's logbook to prove it exposed **257
  state changes in 14 hours** on that one entity, with
  `automation.kitchen_lights` repeatedly switching it off.

  Root cause: `_bucket_by_multiplier` in `grouping.py` correctly skips a
  `None`/`False` multiplier, so `apply_lighting` never *dimmed* a
  handed-off light - but the blueprint's two turn-off paths
  (`motion_off`'s `reachable_entities` and `reconcile`'s
  `entities_still_on`) were built straight off `resolved_entities` and
  never consulted the multipliers at all. So a light explicitly handed
  to another automation was still being switched off on every
  motion-clear and every reconcile tick. Confirmed the skip half works
  via a read-only `compute_lighting_groups` probe against the real
  kitchen during Evening - `light.kitchen_strip_back` appeared in no
  group whatsoever, while the turn-off logbook entries were attributed
  to the same automation.

  **User's call, unambiguous**: *"if the template says none then that
  light is just hands off leave it"*. Fixed with a new
  `hands_off_entities` variable, `reject`ed from both turn-off lists.

  **Two non-obvious things this required:**
  1. **`0 == false` in Jinja, exactly as in Python** - so the natural
     `selectattr(1, 'in', [none, false])` membership test *silently
     swallows every `0` multiplier as handed-off*, which would have
     turned "turn this light off" into "never touch this light". Verified
     live before writing the fix: that expression returns
     `['false', 'null', 'zero']` for a dict containing `0`. The correct
     form is an identity test - `multiplier is none or multiplier is
     sameas false` - which returns `['null', 'false']` and mirrors
     `grouping.py`'s own `m is None or m is False` exactly. `sameas` is
     the Jinja identity test; there is no `is false` test.
  2. **The whole brightness-multiplier variable chain had to move
     earlier in the `variables:` block.** HA renders `variables:` strictly
     top to bottom, each key seeing only the ones above it (documented in
     the best-practices skill's `automation-patterns.md#variables`, and
     the silent-failure modes there are nasty - `x | length` on an
     undefined name returns `0` with no log and no trace entry).
     `brightness_multipliers` was declared near the *end*, while
     `reachable_entities`/`entities_still_on` are near the start, so
     `hands_off_entities` and its dependencies
     (`template_brightness_multipliers`, the four `*_exclude_lights`,
     `phase_exclude_lights`, `brightness_multipliers`) were relocated to
     just after `manual_run`. A comment there records why they can't move
     back down.

  Three tests added to `TestBrightnessScaling`, all **mutation-verified**:
  removing the `reject` from both turn-off lists fails exactly
  `test_a_null_multiplier_light_is_not_turned_off_when_occupancy_clears`
  and `test_reconcile_does_not_retry_turning_off_a_null_multiplier_light`;
  swapping the identity test for the naive `in [none, false]` fails
  exactly `test_a_zero_multiplier_light_is_still_turned_off_when_occupancy_clears`
  - i.e. the `0`-vs-`null` trap is directly covered, not just reasoned
  about. Full suite: 94/94.

  `docs/BLUEPRINT.md`'s multiplier table previously stated the old
  behaviour outright ("skips the light entirely on power-on ... **but
  still includes it when the room turns off**") - now corrected, with an
  explicit "`0` and `null` are not the same thing" paragraph, plus notes
  in the occupancy and self-healing sections.

  **The original report itself was not a bug**, and is worth recording
  so it isn't re-investigated: `automation.kitchen_strip_gradient` fired
  correctly at 20:00:01.838 (`last_triggered`), and the device confirmed
  it with an attribute-only update at 20:00:03.568 (`last_changed`
  unchanged at 19:53:59, `last_updated` moved) - proving the MQTT topic
  and payload both land. The user then turned the strip off at 20:08:30
  via a **bare context** (no `context_user_id`, no automation - i.e.
  from outside HA's service layer entirely, such as the Hue app). Nothing
  re-asserted it because adaptive excludes it during Evening/Night by
  that room's own template, and the gradient automation is a one-shot on
  the `to: "Evening"` edge which had already passed.
- **Two-step-label drift detection added as a fixable repair
  (2026-08-16).** Prompted by the user asking whether the
  `no_combined_transition` list was maintainable at all: *"have we
  exposed a way to keep that list up to date as part of our
  integration?"* Answer was no - it's pure HA label-registry data that
  `grouping.py` only ever reads, applied by hand, and getting it wrong
  fails completely silently (exact `label_id` string match; a typo, or a
  new bulb nobody remembered to label, just quietly goes back to
  combined transitions). User asked for detection **plus** a
  configurable model list **plus** auto-fix: *"the list of bulbs types
  it should detect should be configurable once the integration is
  installed (and easily updatable with a PR into the repo) - can we make
  the repair diagnostic autorepair?"*

  Audited the live instance first, before building anything: 27 TRADFRI
  GU10s, 25 labelled, and the 2 unlabelled ones both decommissioned
  ("Ex ..." names, parked in dead areas). So the list was *correct* -
  this is drift-prevention, not a bug fix.

  **Shape**: `two_step.py` is pure (no HA imports, matching
  curve/grouping/scenes), `two_step_check.py` is the registry adapter,
  `repairs.py` is the fix flow. Case-insensitive globs against
  `"<manufacturer> <model>"`.

  **The model list started as two layers and was flattened the same day,
  at the user's direction** - originally `DEFAULT_TWO_STEP_MODEL_PATTERNS`
  shipped in the repo and an options field added *extra* patterns on top,
  additive only. User's objection was consistency: *"you've let people
  add new entries to the list of lights to manage, but you've hard coded
  the ones we currently do - we should treat both the same, just
  pre-populate the field with lights we'll handle by default."* Now the
  options field is seeded with the shipped defaults and holds the whole
  list; a saved value replaces them outright, so deleting a shipped
  pattern actually takes effect. An empty/whitespace field falls back to
  the defaults rather than disabling detection, so clearing the box by
  accident can't silently switch the check off.

  **Known, accepted trade-off**: once a user saves the field they own it,
  and a later release adding a newly discovered bulb to
  `DEFAULT_TWO_STEP_MODEL_PATTERNS` will not reach them. That partially
  undercuts the original "easily updatable with a PR" goal - flagged to
  the user, who chose consistency anyway. PR updates still reach every
  install that hasn't customised the field.

  **The fix applies the label to the *device*, not the entity** -
  `grouping.py` accepts either, but device survives entity renames and
  covers every light entity the device exposes. It also *creates* the
  label if absent, which is what actually guarantees the `label_id` is
  right: HA derives the id from the name at creation, so creating it
  here removes the one genuinely error-prone manual step.

  **Two real bugs the test harness caught, both dormant-until-invoked:**
  1. **`Debouncer(hass, None, ...)` - lesson 10 repeating verbatim.**
     Debouncer needs a real `logging.Logger` *and* a genuinely awaitable
     `function`; a sync lambda type-checks fine, then fails only when
     the timer actually fires - and the resulting "log the exception"
     path itself dies on the `None` logger, burying the original error.
     Same shape as the `DataUpdateCoordinator(__name__)` bug. Worth
     re-reading lesson 10 before passing anything to an HA helper
     constructor.
  2. **A lingering timer after every run.** `Debouncer` schedules a
     further cooldown timer *after* each execution, so it outlives a
     reload unless explicitly shut down. Replaced with a single tracked
     `async_call_later` handle cancelled on unload - less machinery, and
     nothing pending once it has fired. `pytest-homeassistant-custom-component`
     fails the test on a lingering timer, which is the only reason
     either of these surfaced.

  Verified against HA source before building, not recalled:
  `ir.async_create_issue`'s required args, that `repairs.py` +
  `async_create_fix_flow` is the (registration-free) mechanism, that
  `ConfirmRepairFlow`/`RepairsFlow` come from
  `homeassistant.components.repairs`, and that
  `LabelRegistry.async_get_label(label_id)` exists alongside
  `async_get_label_by_name`. Note `demo`'s manifest does *not* list
  `repairs` in `dependencies`; this integration does, for explicitness.

  **Dismissal needed no code at all.** User asked for a way to dismiss
  the repair and how to get it back; HA's issue registry already
  provides Ignore, and an ignored issue stays in the registry (verified
  live on this instance - a `playstation_network` repair showing
  `ignored: true, dismissed_version: "2026.8.1"` and still listable, and
  still ignored after the 2026.8.2 upgrade, so dismissal survives
  version bumps). Recovery is Settings -> Repairs -> overflow menu ->
  "Show ignored issues". Documented in `docs/HELPERS.md` rather than
  reimplemented - don't build an opt-out for a repair, the platform has
  one.

  Tests: 20 pure (`tests/test_two_step.py`) + 10 end-to-end
  (`tests/integration/test_two_step_repair.py`, real entity/device/
  label/issue registries) - including that a device-level label counts
  the same as an entity-level one, that a Hue bulb isn't swept up by
  pressing Fix, that a disabled entity is never flagged, that a saved
  list replaces rather than extends the defaults, that an empty field
  falls back to them, and that the issue *self-clears* after the fix via
  the registry-change watcher rather than a manual re-check. Full suite:
  119/119.

  **Verified live end-to-end, twice.** First with 2 bulbs
  (`light.utility_spot_1`/`_2`): labels removed -> repair appeared within
  the coalescing window naming exactly those two -> Fix pressed in the UI
  -> labels written to the *devices* -> issue self-cleared -> a
  read-only `compute_lighting_groups` probe put both in `two_step` while
  an unlabelled Hue pendant stayed in `combined`, proving device-level
  labels really do drive the routing (their entity labels were empty).
  Then with all 25 at once, which is the current live state - all
  detected, sorted, truncated at 8 with "and 17 more".

- **Brightness multipliers are now clamped at 255, not just floored at
  1 (2026-08-17).** User's ask was ergonomic - *"it should clamp the top
  end at 255 so templates don't have to do complicated maths to figure
  out the correct brightness"* - but checking what actually happened
  first turned it into a real bug fix.

  `light.turn_on` validates brightness with
  `VALID_BRIGHTNESS = vol.All(vol.Coerce(int), vol.Clamp(min=0, max=255))`
  - confirmed by grepping the pinned HA in `.venv-integration`, not
  recalled. **`vol.Clamp`, not `vol.Range`**: HA silently writes the
  clamped value rather than rejecting the call. So a multiplier of 1.5
  against a curve at 200 computed a target of 300, HA wrote 255, and
  then `_already_set` compared the light's reported 255 against the
  un-clamped 300 target, never found it within the 2-point tolerance,
  and re-commanded that light **on every single tick, indefinitely** -
  the same silent churn shape as the kitchen strip's 257 state changes
  in 14 hours. Clamping in `grouping.py` keeps our notion of "at target"
  reachable by a real bulb.

  `MAX_BRIGHTNESS = 255` is a named constant in `grouping.py` with the
  reasoning attached, since the number alone doesn't explain why the cap
  is load-bearing rather than cosmetic. Two tests, both
  mutation-verified (removing the cap fails exactly
  `test_multiplier_caps_at_max_brightness` and
  `test_a_light_already_at_max_is_not_recommanded_when_the_multiplier_overshoots`).
  Docs updated in `docs/BLUEPRINT.md`'s multiplier table and both copies
  of the `brightness_multipliers` description in `services.yaml`. Full
  suite: 121/121.

- **Two independent faults found from one user report, 2026-08-19:
  ensuite lights came on and never updated until a manual run.** Both
  found by diagnosis-before-fix at the user's explicit instruction
  ("Diagnose rather than fix right now"), and both are general rather
  than ensuite-specific.

  **Fault 1 - the `adaptive` trigger goes silent whenever the curve is
  flat, in every room.** Morning and Night are constant stretches
  (verified against the sensor's own `points`: Morning is
  `brightness [255] kelvin [6667]` across all 24 samples, Night is
  `[50]/[2700]`), so the coordinator re-writes identical state *and*
  identical attributes every minute. Home Assistant treats that as a
  `state_reported`, not a `state_changed`, and `platform: state` only
  sees the latter. Proven live without reading any source: the sensor's
  `last_updated` sat unmoved at 06:00:41 while `last_reported` advanced
  to 06:56:41, and *no* room had a single `adaptive`-triggered run in
  its trace history over that window - Jacob's pendant, on the same
  sensor, showed only reconciles too. Earlier the same day it had been
  ticking every minute, because that was during Day when the Kelvin
  ramp genuinely changes.

  Fixed with a `time_pattern` floor tick (`minutes: /1`) under its own
  id, `adaptive_tick`. Deliberately **not** reusing the `adaptive` id:
  `scene_recheck_due` reads `trigger.from_state`/`to_state`, which a
  time_pattern trigger has no equivalent of, and attribute access on an
  undefined name raises and aborts the whole run (the silent-failure
  table in the best-practices skill's `automation-patterns.md#variables`).
  `scene_recheck_due` returns false for `adaptive_tick` outright - it
  carries no phase to compare, and treating every minute as "recheck
  due" is precisely the re-activate-the-scene-every-minute behaviour
  that variable exists to prevent. `script_transition` gained the new id
  alongside adaptive/extra/recovered.

  **Fault 2 - the `recovered` trigger's aggregate was the wrong way
  round, and one orphaned entity disabled it permanently.** It asked
  "none of our lights are unavailable", which a single entity that is
  *always* unavailable holds false forever. The Ensuite had exactly
  that: `light.ensuite_spots`, a Zigbee group that no longer exists,
  still in the area and permanently unavailable - so recovery had been
  silently dead for that whole room. **User's own correction, and the
  right one**: *"this trigger is WRONG - the trigger should be ALL of
  the lights in this room were unavailable, but now at least one
  isn't."* Now `{{ ids | reject(unavailable) | reject(unknown) | length > 0 }}`,
  which arms while the room is entirely dark and fires as the first bulb
  returns, making a dead entity just one more member of the dark set.

  **Accepted blind spot, asserted in a test rather than left to be
  discovered**: one flaky bulb recovering beside available siblings no
  longer moves the aggregate, so `recovered` doesn't fire for it. That
  is only tolerable *because* of fault 1's fix - the periodic floor tick
  is what mops it up. The two changes are complementary, not
  independent; shipping the trigger change without the tick would have
  traded one gap for another.

  Neither fault alone explains the report - the ensuite has no occupancy
  sensor, so with `adaptive` silent (flat Morning curve) and `recovered`
  vetoed (orphan), and `reconcile` only ever turning lights *off* and
  gated on occupancy entities existing, there was genuinely no path left
  that could update those lights. 17 minutes elapsed between the lights
  coming on (06:29:58) and the manual run (06:47:07).

  Four tests added, all mutation-verified: reverting the aggregate fails
  exactly `test_an_orphaned_permanently_unavailable_entity_does_not_veto_recovery`
  and `test_one_bulb_recovering_beside_available_siblings_does_not_fire`;
  removing the floor tick fails exactly
  `test_a_flat_curve_still_gets_a_tick_from_the_time_pattern`; letting
  the floor tick recheck scenes fails exactly
  `test_the_periodic_tick_does_not_reactivate_a_scene`.
  `test_recovery_joins_the_ordinary_room_wide_tick_not_a_separate_call`
  was rewritten to black out the whole room, since that is what the new
  aggregate actually arms on. Full suite: 125/125.

- **Override protection redesigned a second time, from a single write
  record to two named claims (`confirmed`/`pending`) - 2026-08-19,
  prompted by a real live incident and a genuinely collaborative design
  process, not a self-driven refactor.** User report: "the living room
  lights are definately whiter than usual at the moment." Diagnosed via
  `compute_lighting_groups` with and without `force` against the real
  instance: the living room pendants were permanently excluded from
  every tick, and forcing brought them to the correct target - override
  protection was stuck locked on, not a curve/sensor bug.

  Root cause, confirmed by rereading `write_tracking.py`'s own design:
  `apply_lighting` records the `context.id` it *issued* right after
  dispatching a write, without ever confirming the physical bulb
  adopted it - the two are asynchronous. A single dropped write (the
  device silently ignoring or failing a command) leaves the recorded
  context permanently mismatched against the light's real, unchanged
  one. Nothing that happens afterward can retroactively make them equal
  again, so the single-record design (in place since `owner_id`
  shipped) had a real, previously-undiscussed failure mode: **any one
  dropped write locks a light out forever**, not just a genuine
  external change.

  **User explicitly rejected the first fix proposed** (extending the
  existing clear-on-unavailable `state_changed` listener to also do
  write-confirmation) as over-engineered, verbatim: *"stop, wait wait
  wait - I think you've over complicated things here, this is easy...
  I'm not totally convinced we even need any sort of additional
  internal state tracking... Don't make any changes yet I want to
  understand this problem thoroughly."* What followed was a genuine,
  multi-round design conversation, not a single correction:
  - Verified against the actual pinned HA source in `.venv-integration`
    (not recalled) in response to the user's own question about
    `context.id`'s constraints: `Context.__init__`'s `self.id = id or
    ulid_now()` accepts any string with zero validation, but the
    recorder's `ulid_to_bytes_or_none()` requires exactly 26 characters
    or silently stores `NULL` for that row - ruling out a "salted,
    still-unique context.id" scheme without breaking logbook/recorder
    attribution for the write it's used on.
  - **User proposed the mechanism that shipped**, verbatim: *"we store
    the last N writes from an apply_lighting, when we do another run we
    check if the current context.id is in our list... I *think* this
    means we only need to store 2 context ids but possibly we might
    want to store more?"* Validated by tracing a naive FIFO-of-2 against
    a concrete counterexample: two consecutive dropped writes evict the
    still-valid earlier entry under an unconditional shift, silently
    reopening the exact bug being fixed. The correct mechanism isn't a
    FIFO at all - it's two **named** slots, `confirmed` (a write an
    *earlier* call actually observed landing) and `pending` (the most
    recent attempt, unverified), with promotion (`confirmed <-
    pending`) happening only when a later call *observes* the live
    context matching the old `pending` - never on a schedule, never by
    assumption. This is provably bounded at exactly two stored values
    regardless of how many consecutive writes fail, since `confirmed`
    is only ever replaced by an observed match. User's go-ahead,
    verbatim: *"nope, that sounds good, make it so!"*

  **A second gap surfaced only after implementing and running the real
  test suite, not from further design discussion up front.** Three
  `tests/integration/test_services.py` tests failed -
  `test_override_protection_survives_a_real_write_tracking_round_trip`,
  `test_recovered_light_is_freed_while_an_unrelated_override_stays_protected`,
  `test_a_restart_style_unavailable_blip_does_not_clear_an_existing_record`
  - all for the identical reason: each does exactly one `apply_lighting`
  call before checking protection, so `confirmed` never gets promoted
  from `None`. Traced this to a real, structural ambiguity rather than
  a test artifact: a light's **very first-ever write** (or first write
  after a genuine unavailable-recovery, which clears the whole record)
  has no `confirmed` baseline - if an external change lands before the
  next confirming tick, there's no way to distinguish "our first write
  silently dropped" from "someone changed it right after," using
  context.id alone. Being lenient there (what shipped first) self-heals
  a dropped first write but leaves that narrow window's *genuine*
  external changes unprotected; being strict resurrects the exact
  permanent-lockout bug this whole redesign exists to fix, just scoped
  to first writes. Presented both trade-offs directly via
  `AskUserQuestion` rather than picking one and rewriting the tests to
  match, given how much rigor this project already puts specifically
  into override-protection correctness.

  **User's resolution, verbatim, and the one actually shipped**: *"if
  it's the first right [sic - write] then we treat the last context.id as the
  confirmed ID. yes it wasn't ours but the intent is the same. If our
  first write fails then the context.id will stay as the 'confirmed'
  one and subsequent writes know they can continue."* On an entity's
  first-ever write (no prior record at all), `async_record` now records
  the context.id that was live *before* that write - almost certainly
  not this integration's own - as the `confirmed` baseline, with
  `owner_id: null` (so it never itself blocks a different owner's
  claim, matching the existing "a claim with no owner doesn't count
  against anyone" precedent). This isn't claiming that pre-existing
  state as this caller's write; it's reusing the same "the light hasn't
  changed" retry signal every later dropped write already relies on -
  if the first write drops, the light's context stays at exactly that
  pre-write value, so the very next call recognises the match and
  retries instead of reading "no record -> free" and losing the
  attempt. All 3 originally-failing tests pass unmodified once this
  landed - confirmed by retracing each by hand before rerunning, not
  just observed after the fact.

  **Mutation-verified**: reverting the first-write baseline back to
  `confirmed = None` fails exactly those same 3 tests (129/132) and no
  others - confirmed by actually applying the mutation and rerunning,
  not assumed from the trace alone. A fourth, new test,
  `test_a_dropped_first_write_self_heals_on_the_next_tick_with_no_interference`,
  documents the complementary half of the story (a genuinely dropped
  first write, no external interference, self-heals on retry) even
  though it doesn't discriminate this specific mutation on its own -
  the original lenient-when-`None` design also passed it, since a fully
  lenient design was never wrong about the *self-healing* half, only
  the *protection* half. Full suite: 132/132.

  `docs/HELPERS.md`'s "Override protection" section was rewritten from
  the old single-record 7-step description to the confirmed/pending
  model, including the first-write baseline; `docs/BLUEPRINT.md`'s
  "Override detection" section's opening paragraph was updated to match
  and now links through to `HELPERS.md` for the full mechanism rather
  than restating it. `write_tracking.py`'s own module docstring and
  `async_record`'s docstring carry the full reasoning for both the
  confirmed/pending promotion rule and the first-write baseline, not
  just the two-line summary here.

- **`sensor.adaptive_lighting_write_tracking` added, 2026-08-20 - the
  confirmed/pending mechanism above stopped being a black box.** User's
  own framing, right after the redesign shipped: "can we do something
  like add a sensor to the integration which surfaces the owner_id
  tracking so that a user can see whats going on? I don't like it being
  a black box." Exposes `write_tracking.py`'s raw `confirmed`/`pending`
  claims per light, plus a computed `status` (`confirmed`/`pending`/
  `mismatched`/`unavailable`) so a user doesn't have to mentally re-run
  `externally_set()`'s own comparison logic themselves.

  **Verified against home-assistant/frontend source before deciding
  where this entity lives, not guessed - directly informed by the
  "Auto-seeded Default sensor" incident earlier in this file.** That
  incident's fix was "Add Integration creates zero devices" specifically
  to avoid HA's device-rename/area-picker dialog firing unwanted. Before
  adding a NEW device for this sensor, fetched
  `step-flow-create-entry.ts` directly and confirmed the dialog's own
  logic: it renders whenever *any* device exists for the config entry
  at render time (a live registry read against
  `device.primary_config_entry`, not something scoped to just the
  entities the flow itself returned) - true for every flow type
  (config, subentry, options), and with zero devices it renders nothing
  but a plain confirmation instead. Conclusion: **the sensor gets no
  `device_info` at all** - an entity attached to no device can never
  appear in `this.devices`, so it can't trigger that dialog regardless
  of when or how it's created, without needing to gate it behind an
  explicit "Add X" step the way schedule instances are.

  **A real, previously-latent production bug surfaced as a direct
  result of wiring this up, unrelated to the sensor's own logic.**
  Making this entity entry-scoped (existing even with zero "Add Sensor"
  schedule instances configured) required removing the `if instances:`
  guard around `hass.config_entries.async_forward_entry_setups(entry,
  SCHEDULE_PLATFORMS)` in `__init__.py` - previously, `sensor`/`select`/
  `number`/`time`/`switch` were only ever forwarded when at least one
  schedule instance existed. The very first test exercising this path
  crashed with `AttributeError: module
  'custom_components.adaptive_lighting_helpers.time' has no attribute
  'time'` - `__init__.py` had `import time` (stdlib) at module scope,
  and this package also ships its own `time.py` (the HA `time` platform
  module, part of `SCHEDULE_PLATFORMS`). Importing a submodule
  unconditionally rebinds it as an attribute of its parent package -
  which *is* `__init__.py`'s own global namespace - so the moment
  anything imports `adaptive_lighting_helpers.time`, the name `time` in
  `__init__.py` silently flips from the stdlib module to this
  submodule, permanently, for the rest of the process. `compute_curve`'s
  `call.data.get("at", time.time())` (the only usage) would have raised
  this exact error in **any real production install with at least one
  schedule instance configured** the moment `compute_curve` was called
  without an explicit `at` - the normal case - which is every real
  install, since `async_forward_entry_setups` already ran unconditionally
  there whenever `instances` was non-empty, well before this change. It
  was never caught before because no existing test configures a real
  schedule subentry (grepped for `subentry_type`/`ConfigSubentry` across
  `tests/` - zero matches), so this forwarding path had literally never
  been exercised by pytest, only confirmed live for the entities that
  path creates (never for `compute_curve`'s own "at" defaulting in the
  same process). Fixed by aliasing the import (`import time as
  time_module`), which cannot collide with any submodule name.

  **Two `MockConfigEntry`-based integration test files needed a
  `mock_state(hass, ConfigEntryState.LOADED)` call added** (in
  `tests/integration/test_services.py`'s `_setup_entry` and
  `tests/integration/test_two_step_repair.py`'s `_setup`) -
  `async_forward_entry_setups` now always runs, and it requires the
  entry to already be in `LOADED` state, normally something
  `hass.config_entries.async_setup()` does around calling into a
  component - both files deliberately call `async_setup_entry` directly
  instead (see `test_services.py`'s own docstring: avoids resolving the
  `http`/`frontend` manifest dependencies, needed only for the dashboard
  card, which would pull in the separate, large `home-assistant-frontend`
  package these tests never touch). `mock_state()` is a real, sanctioned
  pytest-homeassistant-custom-component helper for exactly this "can't
  get here via normal config entry methods" situation.

  **The new sensor's own tests deliberately don't go through
  `async_setup_entry`/full platform forwarding at all**
  (`tests/integration/test_write_tracking_sensor.py`) - `frontend`
  being a genuinely unresolvable manifest dependency in this test venv
  (no `home-assistant-frontend` package installed, deliberately, same
  reasoning as above) means that path can't be exercised here without
  reintroducing the exact "large, untouched dependency" `test_services.py`
  already avoids. Instead the entity is constructed directly (`hass`/
  `entity_id` set by hand) and its `native_value`/`extra_state_attributes`
  properties read as plain Python - this tests the actual thing that
  matters (status classification, live push-updates via
  `SIGNAL_WRITE_TRACKING_UPDATED`) without needing HA's platform
  machinery at all, matching how `tests/test_grouping.py` already
  exercises `EntityLookup` through plain fakes rather than real
  entities. The two claims that specifically depend on that machinery -
  auto-registration with zero schedule instances, and genuinely having
  no device in the real registry - were confirmed live against the real
  instance instead, not left untested (see below).

  Mutation-verified: swapping the `pending`/`confirmed` branch order in
  the status classification fails exactly
  `test_status_pending_when_live_context_matches_pending_only`.
  Full suite: 138/138.

  **Confirmed live end-to-end** after HACS update + restart:
  `sensor.adaptive_lighting_write_tracking` exists with `device_id:
  null` in the entity registry (no device, as designed), its state
  reflects the real count of tracked lights, and its `entities`
  attribute shows real `confirmed`/`pending` claims with the expected
  `status` for lights under active override protection.

- **`sensor.adaptive_lighting_write_tracking` shipped push-only, and
  went stale within minutes of its own first deploy - found and fixed
  the same day, 2026-08-20.** Live-checking the sensor right after
  deploying it (the very validation the entry above describes) showed
  every one of 58 tracked lights reading `status: "unavailable"` -
  including `light.living_room_pendant_1`/`_2`, confirmed separately
  via a direct `ha_get_state` to actually be `"off"`, not unavailable at
  all. Root cause: the sensor was wired to update only on
  `SIGNAL_WRITE_TRACKING_UPDATED` (fired from `write_tracking.py`'s
  `async_record`/clear-on-unavailable listener), but `status` is
  computed by comparing `confirmed`/`pending` against each light's
  *live* state - which can change with nothing ever calling
  `apply_lighting` in between. A restart is exactly that case: most
  entities briefly report `unavailable` while their own integration
  reconnects, entirely independent of write-tracking. The sensor's one
  and only push, fired when `async_add_entities` first registered it
  moments after restart, froze that transient window into the state
  machine - and with no light needing a real write since (nothing had
  drifted from target), nothing ever pushed again to correct it.

  Fixed by also polling (`should_poll` defaults to `True` when left
  unset, HA's own `DEFAULT_SCAN_INTERVAL` of 15s) alongside the
  existing push - push still makes a real write feel instant, polling
  is what keeps everything else honest. `docs/HELPERS.md` corrected to
  match (previously claimed "not on a poll").

  Two things worth keeping in mind for anything built on this same
  push-only pattern again: (1) a sensor whose `state`/attributes only
  read data *this integration itself* wrote can safely be push-only,
  but the moment it also compares against something *external* (here,
  live entity state, set by physical devices/other automations), a
  push tied only to *this integration's* own write events cannot stay
  correct - the external half needs its own refresh path. (2) a test
  that reads an entity's properties directly (`sensor_entity.native_value`,
  as this feature's own initial tests did) can never catch this class
  of bug, since properties are always freshly computed on access - only
  a test reading the *state machine's* cached copy
  (`hass.states.get(...).attributes`) exercises the actual staleness
  the real symptom lived in.
  `test_a_poll_refreshes_status_even_when_write_tracking_itself_has_not_changed`
  added, doing exactly that; mutation-verified (reverting to
  `should_poll = False` fails it, and only it). Full suite: 139/139.

- **`adaptive-lighting-write-tracking-card` added, 2026-08-20 - a UI
  for the sensor above, with trace-back to what actually happened.**
  User's own follow-on ask, right after seeing the sensor's raw
  attribute dump: *"can we also have a UI element that tracks this? it
  should probably have each bulb we're tracking, then the actual action
  in the confirmed slot (so trace the context id back) and same for
  pending/unconfirmed."*

  **"Trace the context id back" has a real, low-effort answer: HA's own
  logbook, not anything this integration needs to reimplement.**
  Confirmed against HA core source before building anything -
  `logbook/get_events`, a public WebSocket command
  (`homeassistant/components/logbook/websocket_api.py`), accepts a
  `context_id` filter directly and returns resolved, human-readable
  entries (who, what, when) - HA already walks a context back through
  whatever chain produced it (an automation run, a service call, the
  resulting state change) for its own Logbook UI; a card can call the
  exact same command client-side with zero new backend logic. The one
  gap: that command requires a `start_time` (no way to search "any
  time"), and nothing recorded *when* each claim was made - so a card
  querying it would have to guess a search window or scan unbounded
  history.

  **Fixed by adding `recorded_at` (ISO 8601) to `_ContextClaim` in
  write_tracking.py** - stamped at `async_record` time for a real write;
  left `None` for the synthetic first-write baseline (see that entry
  above), since that context was only ever *observed*, not recorded,
  and a timestamp there would claim precision that doesn't exist. This
  is useful independent of the card too - the sensor's raw attribute
  dump now answers "how long has this been pending" at a glance. Old
  persisted claims (pre-`recorded_at`) migrate with `recorded_at: None`
  on load, same "don't reopen a protection gap on upgrade" precedent
  `async_load`'s other migration branch already established.

  **Card placement decided via `AskUserQuestion` before building**: a
  new standalone card (what shipped) vs. folding into the existing
  curve card as a second tab. User picked standalone - the curve card
  is schedule-scoped (one per named sensor instance); write-tracking is
  global across every light in the house, an awkward fit for a
  per-room card.

  **Design**: one row per tracked light (entity, live status badge,
  confirmed claim, pending claim), sorted "most interesting first"
  (mismatched, pending, unavailable, confirmed) rather than
  alphabetically, plus a text filter (by entity_id or owner_id - useful
  given a real house showed 58 tracked lights in one flat list).
  Clicking a light opens its more-info dialog (a standard
  `hass-more-info` custom event, same mechanism any Lovelace card
  uses). Tracing is **lazy, not eager** - resolving every claim's
  context.id on every render would mean dozens of logbook queries per
  card update for information nobody's looking at yet; a "Trace" button
  per claim fires exactly one WebSocket call, only when clicked,
  narrowed to ±a few seconds around that claim's own `recorded_at`.

  **Two real bugs caught before shipping, both found through direct
  interactive testing of the rendered card, not just reading the code
  back:**
  1. The filter input lost focus and cursor position on every
     keystroke - each keystroke triggers a full re-render (the same
     wholesale `innerHTML` replacement pattern the curve card already
     uses), which necessarily creates a brand-new `<input>` element each
     time. The first draft tried to restore focus by comparing the *old*
     element reference *after* the DOM had already been replaced -
     always false, since that reference was already stale. Fixed by
     capturing focus/caret state from the *previous* input at the very
     start of `_render()`, before the replacement happens, then
     re-applying it to the *new* input afterward.
  2. `document.querySelector`-based verification of the trace flow
     initially appeared to hang forever on "Tracing…" - traced to the
     same class of stale-reference bug in the *test*, not the card: a
     `<tr>` reference captured before the trace resolved becomes
     detached from the live DOM the moment the async resolution's own
     `_render()` call replaces `innerHTML` again: re-reading
     `.textContent` off a detached node just returns what it was at
     detach time, not live content. Re-querying the row fresh after the
     wait showed the trace had in fact resolved correctly
     (`"Kitchen Pendant 1 turned on"`) - not a card bug, but the same
     underlying lesson as bug 1: this card's live-rebuild rendering
     model means *any* reference into its shadow DOM is only valid
     until the next `_render()` call, whether held by the card's own
     code or by something inspecting it from outside.

  **Verified functionally through direct DOM/event interaction in a
  real browser** (`dashboard/preview.html`, extended with synthetic
  data covering all four statuses and a mocked `callWS`), not just a
  visual screenshot - the session's screenshot capture was stuck
  returning stale frames throughout this session (confirmed via
  `window.scrollY` changing correctly while the returned image never
  updated - a tool-level issue, not a rendering one). Confirmed instead
  via direct shadow-DOM inspection and simulated events: correct
  row count and status-priority sort order, correct owner-name/relative-
  time display, the full async trace flow (loading -> resolved text),
  the filter narrowing rows while the input kept focus and caret
  position, all four status badges computing the right background
  colour, and a light-cell click dispatching `hass-more-info` with the
  right `entityId`.

  `dashboard/write-tracking-card.yaml` added (the copy-paste snippet,
  matching `house-settings-card.yaml`'s pattern) - simpler than the
  curve card's, since the sensor it reads is a single entry-scoped
  entity with no per-room name to substitute. `docs/HELPERS.md`'s
  "Inspecting write-tracking state" section and `CONTRIBUTING.md`'s
  file listing updated to match.

- **A plain HA restart, on its own, could permanently exclude every
  already-tracked ON light from adaptive control - found and fixed the
  same day the write-tracking sensor made it observable, 2026-08-20.**
  Not found by symptom report this time - found by *looking*, directly,
  the moment the new sensor gave enough visibility to notice it: right
  after a routine restart (deploying the write-tracking dashboard card),
  the sensor showed all 57 tracked lights as `status: "mismatched"`.
  Confirmed via a direct `compute_lighting_groups` probe (the same
  diagnostic technique used throughout this session) that
  `light.kitchen_1` - genuinely on, never dropped off the network - was
  really excluded, not just cosmetically mislabelled: `combined: []`
  with a real owner_id.

  Root cause: a HA restart recreates every entity's state object from
  scratch, so the very first state report after restart always carries
  a brand-new context.id - even when the reported value hasn't changed
  and the underlying device never actually went offline. That's
  indistinguishable from a genuine external change to
  `externally_set()`'s comparison. The existing clear-on-unavailable
  listener (see its own dated entry above) doesn't cover this: it only
  fires on an *observed* unavailable transition, and a light that stays
  continuously "on" through a restart never produces one - by the time
  the listener is attached again post-restart, the light has already
  reported its new context with nothing here ever seeing the "before"
  state to compare against. Since an externally-set light is never
  written, nothing ever gets the chance to refresh its stale record
  either - the exact permanent-lockout shape the confirmed/pending
  redesign (earlier the same day) exists to prevent, just triggered by
  *any* restart instead of a dropped write, and probably live on every
  restart this entire session has been performing.

  **Presented to the user directly via `AskUserQuestion` before fixing**,
  given how central this exact class of override-protection judgment
  call has been all session - proposed snapshotting each tracked
  entity's live context as its new `confirmed` baseline at startup
  (`LastWriteTracker.async_resync_to_live_state`, called once right
  after `async_load`), reusing the same synthetic-baseline mechanism
  `async_record` already uses for a first-ever write, with the same
  accepted trade-off: a manual override standing in the exact instant of
  a restart is forgotten, same as it already is when a light briefly
  goes unavailable. **User's call: proceed** - matches this session's
  standing precedent (self-heal over lockout when genuinely ambiguous).

  `pending` is deliberately left untouched by the resync - a
  pre-restart `pending` claim can never match a fresh post-restart
  context either way, so there's nothing to fix there; it just sits
  inert until the next real write overwrites it. An entity still
  genuinely unavailable at resync time (a real drop the restart itself
  doesn't fix) is left alone entirely - nothing live to snapshot yet,
  and the clear-on-unavailable listener already handles it correctly
  once it does report back in.

  Three tests added to `test_services.py`, mutation-verified (disabling
  the resync fails exactly
  `test_a_restart_resyncs_confirmed_to_live_context_so_an_on_light_stays_manageable`
  and no others): the main fix, proven end-to-end via `build_groups`
  built directly against the resynced tracker (going through the
  *service* here would prove the wrong tracker's state, since the
  service still reads whichever tracker got registered when the test's
  own `setup_integration` fixture set up its entry, before the test's
  write even happened); that a genuinely-still-unavailable light is left
  untouched (using a bare, unconnected `LastWriteTracker` rather than
  `setup_integration`'s own, whose live clear-on-unavailable listener
  would otherwise interfere and mean the test was only proving *that*
  mechanism works, not this one); and that `pending` survives the
  resync unchanged. One test-writing gotcha caught along the way: two
  consecutive `hass.states.async_set` calls with identical values are
  collapsed into a single "state_reported" event by HA, and the
  *second* call's explicit `context=` is silently discarded - already
  documented in this file's own `test_force_bypasses_protection_and_reclaims_ownership`,
  re-learned the hard way while writing the first draft of this fix's
  own test. Full suite: 144/144.

  `docs/HELPERS.md`'s "Override protection" section extended with a new
  paragraph on this specific gap, alongside the existing device-recovery
  one it's easy to conflate with but is mechanically distinct from.

- **The restart-resync fix above shipped incomplete - found live within
  minutes of its own deploy, fixed the same day, 2026-08-20.** The
  restart that deployed it was also the first real test of it, per the
  user's own explicit instruction to verify thoroughly rather than
  declare success from a first glance. Most lights looked fine
  afterward, but `light.kitchen_2` - genuinely on, same room, same
  automation, same regular tick as several siblings that *had*
  recovered - stayed excluded through multiple ticks. Confirmed
  directly, twice, with fresh `compute_lighting_groups` probes (the
  first read turned out to be comparing against a stale sensor
  snapshot from a few minutes earlier, caught by re-fetching before
  concluding anything - several other lights that looked stuck in that
  stale read had already self-healed via a real tick by the time they
  were re-checked).

  Root cause: `async_resync_to_live_state` runs once, early in startup,
  and correctly skips anything still `unavailable`/`unknown` at that
  exact moment (so it doesn't manufacture a claim from a state that
  isn't really there yet). But a real restart puts *nearly every*
  entity through `unavailable`/`unknown` first - confirmed via
  `ha_get_history` that `light.kitchen_1` and `light.kitchen_2` both
  went `on -> unavailable -> unknown -> on` within the same few seconds
  of each other during the same restart. Whether a given light lands in
  "already reporting by the time resync ran" (fixed) or "still
  reconnecting" (stuck) is a race with no relationship to which lights
  matter - `kitchen_1`'s own apparent fix turned out to be unrelated to
  resync at all once traced through history: it happened to cycle off
  and back on two minutes later for an unrelated reason, which
  "turning off ends protection" (a much older, unrelated mechanism)
  freed on its own.

  Reported to the user in full before touching any code, per their
  explicit ask - what was found, why, and the proposed fix - rather
  than silently patching further. **User's reply: "alright, implement
  it."**

  Fix: `async_start_listening`'s existing clear-on-unavailable listener
  now watches *both* directions of the unavailable/unknown boundary,
  not just the drop. Recovery (unavailable/unknown, or no prior state
  at all, to a real state) gets the identical snapshot
  `async_resync_to_live_state` performs at startup - factored into a
  shared `_snapshot_confirmed` helper - closing the timing race
  entirely rather than depending on startup ordering. The two
  directions are **not symmetric**, and getting this wrong broke a
  previously-passing test immediately: drop only fires when the new
  state explicitly *reports* `unavailable`/`unknown` as a string - not
  merely when `new_state` is absent (an entity fully removed from the
  state machine, e.g. `hass.states.async_remove`, or a fresh process's
  own state machine having no entry for it yet). A first draft treated
  "absent" and "unavailable" as equivalent for the drop direction too,
  which immediately broke
  `test_a_restart_style_unavailable_blip_does_not_clear_an_existing_record`
  - reopening the exact "wiped on every restart" incident that test
  guards, since an entity's in-memory state always vanishes before it
  re-registers on every restart. Recovery has no equivalent asymmetry:
  "no prior state at all" is exactly the case it's meant to catch too
  (a fresh process's first-ever report for an entity, functionally
  identical to recovering from a drop from this listener's point of
  view).

  **A real, unrelated bug caught in passing while refactoring this
  method**: `async_resync_to_live_state` never actually fired
  `SIGNAL_WRITE_TRACKING_UPDATED` after a successful pass - present
  since the method's first draft, never caught because no existing test
  asserted on the signal firing specifically. Fixed in the same change,
  since it was touched anyway.

  **The first version of the new test didn't actually test the new
  code at all - caught by running the mutation check, not by reading
  the test back.** Two consecutive test-design mistakes, both worth
  keeping in mind for anything touching this listener again:
  1. The first draft reached "unavailable" via a live off->unavailable
     transition. That transition trips the *existing*, already-correct
     drop branch, which clears the record and makes the light "free to
     manage" via the pre-existing "no record -> free" fallback -
     entirely independent of whatever the new recovery branch does.
     Disabling the new branch left the test passing regardless. Fixed
     by setting "unavailable" as the entity's *first-ever* state in the
     test's own hass instance (`old_state=None`) instead - not a
     transition the listener's drop branch could ever have fired on,
     matching what light.kitchen_2 actually looked like from a fresh
     process's point of view: its real drop happened in the *previous*
     process, before this one's listener ever existed to observe it.
  2. Even after that fix, the seeded record used a single
     `async_record` call with `live_context_before_write=None`, which
     produces `confirmed=None` (the synthetic-baseline path explicitly
     declines to fabricate one without a real prior context - see
     `async_record`'s own docstring). A record with `confirmed=None` is
     *always* lenient regardless of what recovers, by
     `externally_set()`'s own final fallback - so this also passed with
     the new branch disabled. Fixed by seeding two real writes instead,
     so `confirmed` promotes to an actual claim before the
     unavailable/recovery sequence begins.

  Both mistakes were caught the same way: applying the mutation (delete
  the new branch) and confirming the test still passed when it should
  have failed - not just reading the test and assuming it was testing
  what its name said. The final version mutation-fails correctly, and
  only that test.

  `docs/HELPERS.md`'s restart paragraph rewritten to describe both
  directions together, rather than presenting the startup pass as if it
  were the whole story. Full suite: 145/145.

- **`reconcile`'s occupancy check had no debounce of its own, and could
  turn a light off on a single noisy-sensor blip - found live,
  2026-08-21, user report: dining room spots stuck off, force-run
  needed to bring them back.** User pushed back hard on an initial
  "restart timing race, already fixed" conclusion (the bug class the
  five entries directly above this one cover) - "not sure we've fixed
  everything... there have been no restarts" - and, once a first wrong
  hypothesis (an overlapping/racing `apply_lighting` two-step
  transition) was raised and the user was unconvinced, redirected
  investigation again after a real illuminance/multiplier bug on the
  *extension* lights was found in passing but confirmed by the user to
  be unrelated to the actual report ("that only affexts the extension
  lights though... it was the main dining room lights that were the
  problem"). The `apply_lighting` concurrency hypothesis was disproven
  directly against HA core source (`helpers/script.py`, `core.py`):
  `mode: restart` already cancels an in-flight run's nested service
  calls correctly, including through `asyncio.sleep`, via
  `async_interrupt.interrupt` wrapping every `service:` step - ruling
  out a race as the cause.

  Root cause, confirmed by reading the deployed automation's actual
  condition structure: `reconcile`'s branch checked only `not
  occupancy.is_detected` at the instant its `time_pattern` trigger
  fired - no debounce of its own, unlike `motion_off`'s trigger (which
  already has `options: {for: {seconds: !input no_motion_wait}}}`).
  User's own diagnosis, which is exactly what shipped: "the sensor is
  correct, the lights should only go off once the sensor has registered
  as empty for the given interval though." The dining room's occupancy
  sensor is a plain PIR motion sensor, not a presence sensor (see the
  "device_class: occupancy doesn't mean..." note above) - it reports
  clear the moment motion pauses and flips straight back to detected on
  the next movement, repeatedly, all day, confirmed via its own history
  during this investigation. That's not a faulty sensor, it's how PIR
  detection normally behaves with someone present but not constantly
  moving - which is exactly why `no_motion_wait` exists at all. A
  `reconcile` tick landing on one of those brief clear windows could
  turn the light off immediately, bypassing Wait time entirely instead
  of respecting the debounce Wait time is there to provide.

  Two designs tried before landing on what shipped:
  1. **Native `occupancy.is_not_detected` + `for:`** - the more
     idiomatic fit on paper (`EntityConditionBase` in HA core's
     `helpers/condition.py` supports a duration option via the same
     mechanism trigger-level `for:` already uses). Broke the pre-existing
     `test_reconcile_retries_turning_off_a_light_left_on_with_no_occupancy`
     test outright, even after reordering the test to clear occupancy
     only after automation setup (an attempt at giving the condition a
     transition to observe rather than a pre-satisfied duration).
  2. **Same, plus `options: {behavior: all}`** - `is_not_detected`'s
     default `behavior` is `"any"` (mirroring `is_detected`'s own "any
     entity on" semantics), which for *this* condition means "at least
     one entity is off" - not the negation of `is_detected` once a room
     has more than one occupancy sensor (the dining room's actual
     configuration). Caught and fixed before shipping, but the same
     test still failed regardless - the deeper problem was structural,
     not this bug.

  Both failed for the same underlying reason, confirmed by tracing
  `for:`'s actual mechanism: a native duration condition needs the
  recorder to "prime" a duration that was already satisfied *before*
  the condition itself started watching (HA core's own
  `_HistoryPrimingManager` exists specifically for this) - not reliably
  available in this project's test environment (no real recorder
  configured), and a real concern right after a genuine restart too,
  where `reconcile`'s condition starts watching fresh with no in-memory
  history of its own. This is the same class of restart-timing
  unpredictability this project has been bitten by repeatedly (the five
  entries directly above this one, and lessons 2/10 in this file).

  **Shipped instead: a hand-written Jinja template**, comparing
  `now() - states[e].last_changed` against `no_motion_wait` for every
  occupancy-class entity in `room_occupancy_entities`, all must be
  `off` *and* past Wait time for reconcile to proceed. `last_changed` is
  a plain property every state object already carries, answered
  instantly from memory - no recorder, no priming, no restart-timing
  dependency at all. Required exposing `no_motion_wait` as a named
  blueprint `variables:` entry (`no_motion_wait: !input no_motion_wait`)
  for the first time - previously only ever consumed via direct
  `!input` YAML-node substitution (e.g. in the `motion_off` trigger's
  own `for:` clause), never as a name a Jinja template could reference;
  `!input`'s own substitution is a YAML-node-value mechanism, not
  something usable bare inside a template string. `motion_off`'s own
  trigger-level `for:` was deliberately left unchanged - it already
  debounces correctly since a real *trigger* firing after a duration is
  exactly what `for:` is built for, this gap was specific to
  `reconcile`'s bare instantaneous condition check.

  **The existing test itself needed a real fix, not just a reorder,
  once the new template was in place - caught by actually running it,
  not assumed from the design change alone.** `async_fire_time_changed`
  only fires the event that trips a `time_pattern` trigger; it does not
  advance `now()` itself (confirmed by reading its own implementation in
  `pytest_homeassistant_custom_component.common` - no global time patch
  at all). Since the new condition's `now()` reads real wall-clock time,
  the pre-existing test (real elapsed time between clearing occupancy
  and firing the trigger: milliseconds, not six minutes) failed
  correctly against the *new* logic - proving the debounce genuinely
  works, not a test bug. Fixed with `freezegun.freeze_time`, ticking
  frozen time forward by the intended gap before firing the trigger, so
  `now()` inside the template genuinely reflects elapsed Wait time.
  **The new momentary-blip test
  (`test_reconcile_ignores_a_momentary_occupancy_blip_shorter_than_wait_time`)
  had the identical, easy-to-miss problem**: its first version used
  real (non-frozen) `async_fire_time_changed` calls, so it passed - but
  for the wrong reason, since real elapsed time between its two calls
  is always near-zero regardless of what the debounce logic actually
  does. Mutation-verified this was a real gap before fixing it: removing
  the debounce clause entirely left that test passing. Rewritten with
  the same `freeze_time`-and-tick pattern, after which the identical
  mutation correctly fails exactly that one test and no others.

  Full suite: 146/146.

- **`TestRecoveredTrigger::test_a_plain_off_to_on_transition_does_not_fire_it`
  was genuinely flaky, at roughly 5% (1 failure in 20 runs) - found and
  fixed while shipping the reconcile-debounce PR directly above this
  entry, 2026-08-21. Went through two designs, and the first one shipped
  broken - caught live in CI, not locally.** Root cause: `adaptive_tick`,
  the real `time_pattern: minutes: /1` trigger added for the flat-curve
  fix (2026-08-19, documented above), is a genuine, live trigger on
  every test automation this file sets up - not mocked, not disabled.
  This test's whole premise is `assert apply_lighting_calls == []` after
  a plain off->on transition that nothing in the blueprint should react
  to - but nothing in the test controls wall-clock time, and
  `_setup_room_automation` genuinely schedules `adaptive_tick` against
  real UTC time the moment the automation is set up. If real time
  crosses a minute boundary between that setup call and the test's
  final assertion, `adaptive_tick` fires for real, calls
  `apply_lighting`, and the "nothing fired" assertion fails for a reason
  that has nothing to do with what the test claims to be checking.

  **First attempt (shipped, then reverted the same day)**: a file-wide
  `autouse=True` fixture doing a bare `with freeze_time(dt_util.utcnow())
  as frozen: yield frozen`, on the theory that freezing wall-clock time
  stops a real-time-scheduled trigger from firing on its own. Passed
  every local check at the time - the exact repro loop that found the
  flake, 60 consecutive runs, zero failures - and was pushed. **CI
  failed on the very first run**, with two different tests each showing
  an extra, unexplained `apply_lighting` call carrying `transition: 30`
  (the blueprint's own default, not anything either failing test
  configured) - including
  `test_a_plain_off_to_on_transition_does_not_fire_it` itself, the exact
  test this fix existed to protect. The fix hadn't just failed to help,
  it had made the underlying real-firing problem *more* likely to
  surface, not less.

  Root-caused by direct experimentation, not guesswork: a scratch repro
  automation with a single `time_pattern: minutes: /1` trigger, set up
  inside `with freeze_time(dt_util.utcnow()): ... await
  asyncio.sleep(0.5)`, **hung indefinitely** - `asyncio.sleep()` never
  returned. A second repro (many iterations of state-writes +
  `async_block_till_done()`, no explicit sleep, mirroring this file's
  real usage pattern) logged asyncio's own internal slow-callback
  warning, `Executing <Task ...> took 1785461732.472 seconds` - roughly
  *56 years* of apparent elapsed time for a single callback. Both point
  at the same mechanism: freezegun's plain `freeze_time()`, with no
  further configuration, also mocks `time.monotonic()` - the clock
  `asyncio`'s own event loop uses for all of its internal timer
  bookkeeping (`loop.time()`, and every `TimerHandle.when()` a
  `time_pattern` trigger schedules through it). A real, live event loop
  does not tolerate having its own notion of elapsed time frozen or
  desynced out from under it - pending timers can end up scheduled
  against a `when` that, compared against the loop's now-inconsistent
  clock, reads as either infinitely far in the future (the hang) or
  already long overdue (the phantom `transition: 30` calls, and
  presumably why the *specific* tests CI happened to hit varied
  run to run - whichever test's `async_block_till_done()` happened to
  be the next one to process the event loop's ready-timer queue after
  things fell out of sync).

  **Second attempt (what actually shipped)**: freezegun has a
  documented, purpose-built answer to exactly this - `real_asyncio=True`,
  which freezes `datetime.now()`/`time.time()` (everything the
  blueprint's own templates and `dt_util.utcnow()` calls actually read)
  while leaving the event loop's own clock and timer scheduling on the
  real, unmodified system clock. With the loop's timing restored to
  real and predictable, a `time_pattern` trigger's real firing is back
  to depending on genuine elapsed wall-clock seconds between
  registration and the next matching boundary - which reopens the
  *original*, narrower risk this fixture exists to close (a trigger
  registered very close to its own boundary could still fire for real
  within a test's brief execution window). Closed by freezing at a
  fixed anchor a couple of seconds *past* the current minute
  (`dt_util.utcnow().replace(second=2, microsecond=0)`) rather than at
  `dt_util.utcnow()` verbatim, which could itself land arbitrarily close
  to a boundary - guaranteeing at least ~58 real seconds before
  `adaptive_tick` could next fire, comfortably longer than this file's
  entire real run time (a few seconds, full suite included).

  `TestSelfHealing`'s two pre-existing tests (documented immediately
  above), which already used their own bare `freeze_time()` blocks
  *without* `real_asyncio` before this session touched anything, were
  refactored to take the shared `frozen_time` fixture and call
  `.tick()`/`.move_to()` on it directly instead of opening their own
  nested freeze - both to avoid relying on freezegun's freeze-nesting
  support where it isn't needed, and because their own pre-existing bare
  freeze was itself a plausible source of exactly this class of bug.

  Verified thoroughly before pushing this time, specifically targeting
  what CI had actually caught (not just the original single-test
  repro): the single-test repro loop (30 runs, zero failures), a
  20-run loop scoped to `TestSelfHealing` alone (the tests most likely
  to interact badly with `real_asyncio` given their own explicit
  `tick`/`move_to`/`async_fire_time_changed` usage - zero failures), and
  **100 consecutive full-suite runs (60 then a further 40), zero
  failures** - full-suite runs specifically, since that's the shape CI's
  own failure took and single-test reruns alone hadn't caught it the
  first time. Full suite: 146/146.

  **The "second, unrelated flake" originally flagged as a separate,
  pre-existing issue after the first (broken) attempt is almost
  certainly the same bug, not a different one, and appears fixed as a
  side effect of this same change - not confirmed with certainty, but
  strong statistical evidence either way.** That flake (occasional
  full-suite runs failing 2-4 unrelated tests together, at roughly
  10-14% of full-suite runs on the pre-this-session baseline commit)
  was reproduced on a baseline that still had `TestSelfHealing`'s own
  bare, non-`real_asyncio` `freeze_time()` usage - the same mechanism
  just root-caused above. 100 consecutive clean full-suite runs after
  switching every `freeze_time()` usage in the file to `real_asyncio=True`
  would be a roughly 1-in-40,000 outcome if the true underlying failure
  rate were still ~10%. Not re-flagged as a separate task on that
  basis, but worth revisiting if full-suite flakiness resurfaces.

- **Every room automation was firing twice a minute during any ramping
  phase, and a light near the tolerance boundary could get double-written
  across those two near-simultaneous ticks - found live, 2026-08-21,
  user report: "apparently my adaptive lighting automations are all
  firing twice a minute."** Confirmed directly via
  `automation.dining_room_lighting`'s traces: 5 stored traces spanning
  2.5 minutes, alternating `adaptive` (a `platform: state` trigger on
  the room's sensor, no `to:`/`from:` filter - fires on *any* change
  including attribute-only ticks) and `adaptive_tick` (`platform:
  time_pattern, minutes: /1`, added 2026-08-19 specifically for the
  *opposite* case - Morning/Night's flat curve, where `adaptive` goes
  silent entirely), roughly 2 seconds apart, every minute. Root cause:
  the coordinator polls every 60s (`coordinator.py:75`), and during any
  phase where the curve is actively ramping (Day, and the evening/night
  transition - i.e. everything except the flat stretches `adaptive_tick`
  was built for), every poll produces genuinely different attribute
  values, so `adaptive` now fires almost every poll too - both triggers
  end up covering the same underlying event. `grouping.py`'s tolerance
  check (brightness ±2, color_temp ±10K) suppresses most of the
  resulting redundant runs from producing a real `light.turn_on`, but
  not all: confirmed live via `sensor.adaptive_lighting_write_tracking`
  that `light.dining_room_1` got two separate real writes 57 seconds
  apart, right at a tolerance-boundary moment - a light sitting close
  to the edge, with the target still drifting, can flip in and out of
  "needs update" across both ticks in the same minute.

  User's explicit direction, given three initial options, chose a
  combined fourth: stop `adaptive` from firing on attribute-only
  changes (only a genuine phase transition), keep the periodic tick but
  make its interval configurable, and add jitter so many rooms don't
  all hit Home Assistant/Zigbee in the same wall-clock second.

  **Design correction, mid-session, user-caught.** A first draft (via a
  Plan subagent) proposed gating `adaptive` with a new `condition:`
  entry and a `scene_recheck_due`-style template variable comparing
  `trigger.from_state.state != trigger.to_state.state`. The user asked
  instead: "can't we just add the sensor as a real trigger... HA
  normally only triggers on state change not attribute change?" -
  verified directly against the pinned HA core source
  (`homeassistant/components/homeassistant/triggers/state.py`,
  `async_attach_trigger`): `match_all` is `True` only when *none* of
  `from`/`not_from`/`to`/`not_to` are present in the trigger config;
  with any of those keys set, the listener explicitly rejects an event
  where `old_value == new_value` - i.e. it requires an actual value
  change once filtered. The `adaptive` trigger had none of those keys,
  which is *why* it fired on every attribute tick. Adding a `to:`
  filter listing the sensor's four real phase values is enough on its
  own - no new `condition:` entry, no new template variable - and is
  strictly better than the condition-based draft: HA rejects the
  spurious runs at the event-listener level, before even reaching this
  automation's `condition:`/`action:`, rather than paying for a full
  condition evaluation on every rejected tick. `from:` stays
  unrestricted, so a transition into a real phase from `unknown`/
  `unavailable` (a restart, or the sensor's first-ever value) still
  correctly fires - `old_value` is `None` there, which never equals a
  real phase name.

  **Jitter scope was also corrected mid-session, user-caught.** First
  scoped to `adaptive_tick` only. User pushed back: "if all the
  automations have to fire at the same time... I'm not totally sure
  we're gaining much" - correctly identifying that the *bigger*
  simultaneous-cascade risk is a genuine phase transition (`adaptive`),
  not the periodic tick. Once `adaptive` is gated to real transitions,
  every room sharing one Adaptive Lighting Sensor instance (confirmed
  live - dining room/kitchen/entrance hall all point at
  `sensor.ground_floor_adaptive_lighting`) fires at literally the same
  instant, ~4 times a day - `adaptive_tick`'s own cascade risk is
  smaller by comparison, since most of its ticks are already no-ops via
  the tolerance check. Jitter now covers `trigger.id in ['adaptive',
  'adaptive_tick']`. `extra`/`recovered` are deliberately left out -
  both are inherently per-room events, not something that fires
  simultaneously across multiple rooms the way a shared sensor's phase
  transition or a synchronized `time_pattern` boundary does.

  **What shipped**: `adaptive`'s trigger gained `to: [Morning, Day,
  Evening, Night]` (a native filter, not a coupling to the phase
  names' meaning - they're already hardcoded in several other inputs
  in this blueprint, e.g. `rgb_phases`' selector options).
  `adaptive_tick`'s `minutes:` is now `!input adaptive_tick_interval`
  (new input, default `"/1"` - preserves today's cadence exactly, now
  that this tick is the sole driver of ordinary tracking rather than
  just the flat-curve fallback). A new `adaptive_tick_jitter` input
  (default `15` seconds, user's explicit choice - non-zero by default
  so the thundering-herd problem is actually addressed out of the box,
  not just for rooms where someone discovers the input) feeds a `delay:`
  step, first in `action:`, rendering `range(0, N+1) | random` when
  `trigger.id` is `adaptive`/`adaptive_tick` and `0` otherwise - a `0`
  delay short-circuits synchronously in HA core's own
  `_async_step_delay` (`if not delay: return`), so non-jittered
  triggers pay no real overhead. `mode: restart` (already set on this
  automation) means a genuine trigger arriving mid-jitter correctly
  preempts the delayed run - confirmed against `helpers/script.py`: a
  new run's `Script._async_run` calls `async_stop()` on any in-flight
  run before starting, resolving `_async_step_delay`'s awaited stop
  future, so the delayed run's next loop iteration sees `self._stop.done()`
  and aborts before ever reaching `action:`'s `choose:` block.
  `scene_recheck_due`'s own `trigger.id == 'adaptive'` branch is now
  provably unreachable-when-false (any run reaching it already has
  `from_state.state != to_state.state` by construction, enforced at the
  trigger level) - left as-is with a comment noting why, rather than
  simplified in the same change; treat that as a separate, lower-risk
  cleanup.

  **`reconcile_interval`'s existing description got the same syntax fix
  while this area was already being touched.** The user's first idea
  was linking `time_pattern` syntax to crontab.guru; verified live
  against HA's own docs (`home-assistant.io/docs/automation/trigger/#time-pattern-trigger`)
  that `time_pattern` is deliberately simpler than standard cron -
  separate `hours:`/`minutes:`/`seconds:` keys, each only a fixed
  number, `*`, or a `/N` step, no day-of-month/month/weekday fields, no
  comma lists or ranges - and HA's own docs never call it cron.
  crontab.guru expects a single 5-field cron string; pointing users
  there risks them typing that whole string into a field that only
  accepts the bare `/5` fragment, which fails `TimePattern.__call__`'s
  validation (`homeassistant/components/homeassistant/triggers/time_pattern.py`,
  read directly, not guessed). Both `reconcile_interval` and the new
  `adaptive_tick_interval` link to HA's own docs page instead.

  **Test impact was much bigger than the initial scan suggested - found
  while planning, not while implementing.** The autouse `_sensor`
  fixture in `test_blueprint.py` primes `sensor.test_adaptive = "Day"`
  for *every* test before the test body runs - so a test's own
  `hass.states.async_set("sensor.test_adaptive", "Day", {...different
  attrs...})` has a real prior "Day" `from_state`, not `None`: a
  genuine same-phase, attribute-only write, exactly the case now
  filtered out. 11 existing tests relied on this as their sole trigger
  mechanism and needed a `async_fire_time_changed` advance added
  (firing via `adaptive_tick` instead) to keep passing - two of them
  (`test_periodic_adaptive_tick_updates_an_already_on_light`,
  `test_adaptive_tick_uses_the_adaptive_transition_duration`) had
  always tested the `adaptive` attribute-fire behavior just removed,
  not a real periodic tick, despite their names.

  **`_setup_room_automation`'s own baseline `input_` dict needed
  `"adaptive_tick_jitter": 0` added, not optional.** The blueprint's own
  jitter default is `15`; without this, every test firing `adaptive`/
  `adaptive_tick` and checking `apply_lighting_calls` synchronously
  would need up to a real 15s wait or flake outright - confirmed by
  actually hitting exactly this hang before adding the fix.

  **The jitter test went through four real designs, not one, each
  caught by actually running it - repeatedly, not just once - rather
  than assumed correct after a single green run.** Three of the four
  rounds only surfaced after this same PR picked up a file-wide
  `frozen_time` fixture (`freeze_time(..., real_asyncio=True)`,
  autouse) that landed from a different session mid-flight (see its own
  dated entry directly above this one) - each fix looked complete in
  isolation (a single passing run, sometimes several) until run enough
  times, or as part of the full suite, to expose the next layer.

  1. **First draft**, before the fixture existed: `hass.async_block_till_done()`
     does not "process what's ready and return" - it genuinely blocks
     until every task HA is tracking finishes, however long that takes,
     so it can't observe "not yet, still delayed" state. Fixed with a
     short bounded real sleep (`asyncio.sleep(0.05)`) to let the
     automation's task reach the delay step, then a longer real sleep
     past the delay to observe the resumed call - passed cleanly in
     isolation, at the time.
  2. **The file-wide `frozen_time` fixture landed, and the exact same
     test hung indefinitely** - not slow, genuinely stuck, confirmed via
     `pytest-timeout` catching it mid-`select()` inside a nonzero
     `asyncio.sleep()`. Root-caused with a minimal standalone repro (a
     bare `async def test(...): await asyncio.sleep(0.05)` under the
     identical fixture) before touching the real test at all - a
     genuine, general incompatibility in this harness: any
     nonzero-timeout `asyncio.sleep()` called directly in a *test's
     own* coroutine (as opposed to an HA-tracked background task) hangs
     forever under `real_asyncio=True` in this specific combination of
     pytest-homeassistant-custom-component's own event loop and
     freezegun - `await asyncio.sleep(0)` (zero-length) does not hang
     under the same repro, consistent with asyncio routing a zero sleep
     through `call_soon` rather than `call_later` + a `select()` wakeup.
  3. **Fixed by force-firing the delay's real timer instead of waiting
     it out** - `_async_futures_with_timeout` in HA core's
     `helpers/script.py` confirms the delay's timeout is an ordinary
     `hass.loop.call_later(...)`-registered `asyncio.TimerHandle`,
     exactly what `async_fire_time_changed` already force-fires for
     every `time_pattern` trigger in this file, confirmed to be a
     *generic* mechanism (scans every scheduled `TimerHandle` on the
     loop, not ones belonging to a specific trigger). Passed repeatedly
     for the `adaptive` (state-trigger) case. For `adaptive_tick`,
     it *looked* fine too at first, then flaked under a 15-run repeat
     loop with a genuine spurious second `mode: restart` visible in the
     trace log: once the mock clock has been pushed past one minute
     boundary to fire the tick, a later `async_fire_time_changed` call
     can *also* match the freshly-rescheduled next boundary (its real
     future_seconds, bounded by ~60s of real loop time, can be smaller
     than the mock offset already claimed) - re-triggering the
     automation and cancelling the in-flight delayed run before it ever
     reached `apply_lighting`.
  4. **Final design: dropped the "and it eventually resolves" half
     entirely, kept only "does it delay at all."** Swapping `adaptive_tick`'s
     own resolution to a plain `await hass.async_block_till_done()`
     (reasoning it would simply wait out the real ~1s, same as before
     the fixture existed) *also* turned out unreliable under this exact
     combination - intermittent early returns with the call never
     landed, confirmed by re-running that version of the test 15 times
     in a row and seeing it fail from run 5 onward, not a one-off.
     Concluded that "the delayed call eventually lands" is HA core's own
     `delay:` mechanism, already reliable in production (confirmed
     live) and not something specific to this blueprint's own logic -
     what actually needs proving here is *which* triggers get delayed
     at all, which a `asyncio.sleep(0)`-then-check "not immediate"
     assertion already establishes cleanly, with zero real waiting and
     zero flakiness across 20 repeated runs. The now-permanently-pending
     delayed runs (never resolved) needed an explicit cleanup step
     before the test ends, or teardown fails on a lingering task (a
     genuinely in-flight delay, not just an armed-but-not-due
     `time_pattern` timer, which this file's own teardown fixture
     already tolerates separately) - a final manual `automation.trigger`
     call's own `mode: restart` cancels both pending delayed runs for
     free, the same mechanism the whole feature depends on in
     production.

  Also confirmed HA's `random` template filter is its own
  `_random_every_time` (`homeassistant/helpers/template/extensions/functional.py`,
  wrapping the exact same `random.choice` as Jinja's own default filter,
  just re-implemented `@pass_context` to avoid template-compile-time
  caching) - `monkeypatch.setattr("random.choice", lambda seq: seq[-1])`
  makes the jitter delay deterministic for the test regardless of which
  implementation is actually in play. Stability re-confirmed after the
  final design: 20/20 for this test alone, 10/10 full-suite runs.

  **A time_pattern-interval test went through two rounds of fixes, not
  one - the first for a boundary-math bug, the second for a real bug in
  its own use of `freeze_time`, only exposed once the file-wide
  `frozen_time` fixture above landed mid-flight.** First draft froze
  time to `.replace(minute=0, second=0, microsecond=0)` and ticked
  forward in 1-minute steps - failed under mutation-testing scrutiny (a
  call was present at a point that shouldn't have matched a `/2`
  pattern) because whether "1 minute after now" lands on an odd/even
  minute boundary depends on the real wall-clock minute at test-run
  time, the same class of flakiness lesson already documented elsewhere
  in this file for `reconcile`'s own tests. Fixed by reusing the *exact*
  proven `next_boundary`/`move_to()` pattern `TestSelfHealing`'s
  reconcile tests already established - freeze at the real starting
  instant (not an artificial reset), compute the next matching boundary
  from that same reference point, and always move forward in frozen
  time relative to it.

  That fix opened its own `with freeze_time(dt_util.utcnow()) as frozen:`
  block - correct in isolation, but became a *nested* freeze the moment
  the file gained its own autouse `frozen_time` fixture (`real_asyncio=True`)
  from a different session mid-flight. Confirmed live by actually
  running the merged file, not assumed from reading the diff: the
  combination hung (the fixture's own docstring, written by that other
  session after root-causing the identical mechanism for a different
  test, warns explicitly against exactly this). Fixed by taking the
  shared `frozen_time` fixture as a parameter and calling `.move_to()`
  on it directly, matching every other timing test in this file - no
  nested freeze at all.

  All three mutation checks confirmed correct and isolated: removing
  the `to:` filter fails exactly `test_attribute_only_same_phase_update_does_not_call_apply_lighting`
  (plus expected collateral in `test_adaptive_tick_interval_actually_changes_cadence`,
  since removing the filter lets `adaptive` fire during that test's own
  same-phase write too - not a discrimination failure, a genuine
  side-effect of that specific mutation); narrowing jitter's scope back
  to `adaptive_tick`-only fails exactly `test_jitter_delays_adaptive_and_adaptive_tick_but_nothing_else`;
  reverting `adaptive_tick`'s `minutes:` to a hardcoded `/1` fails
  exactly `test_adaptive_tick_interval_actually_changes_cadence`. Full
  suite: 151/151.

- **`reconcile` (a second, independent periodic tick just for the
  self-healing turn-off retry) merged into `adaptive_tick`, removing
  `reconcile_interval` entirely - 2026-08-21, user's own direct
  question right after the jitter fix shipped: "why do we have TWO
  reconcile intervals?"** No good answer existed - `reconcile` predates
  `adaptive_tick` by a long way (an early, narrow safety net for dropped
  turn-off commands) and `adaptive_tick` was bolted on separately
  (2026-08-19, for the unrelated flat-curve gap); nobody had revisited
  whether they still needed separate cadences once both existed. The
  self-heal check's own Wait-time debounce (shipped hours earlier the
  same day) already makes checking-more-often harmless - it just
  re-evaluates "has it been long enough," never fires early - so there
  was no remaining reason to keep two independent `time_pattern`
  triggers per room. The user's question also surfaced a real gap in
  the jitter fix that shipped moments before it: `reconcile` was not
  jittered at all, so every room sharing a schedule still fired its
  self-heal check at the exact same 5-minute wall-clock boundary,
  unjittered - the identical cascade risk `adaptive`/`adaptive_tick`
  had just been fixed for, just missed because attention was on the
  "twice a minute" symptom specifically.

  **The merge is not a bare trigger-id swap - that would have broken
  ordinary tick-driven reapplication entirely, caught while designing,
  not after shipping it.** `choose:`'s branches are mutually exclusive
  of `default:` - matching only on `trigger.id == 'adaptive_tick'` (the
  reconcile branch's old condition, naively renamed) would have made
  *every* `adaptive_tick` fire take that branch, meaning `default:`
  (scene handling + `apply_lighting`) would never run for it again,
  silently breaking the periodic tick's whole original purpose. Fixed
  by moving the self-heal eligibility checks (an occupancy sensor
  exists in Room, it's been continuously clear past Wait time, and
  something's still on) from the branch's own inline `if:` up to the
  `choose:` branch's `conditions:` list itself, alongside `trigger.id
  == 'adaptive_tick'` - the same shape the `motion_off` branch right
  above it already uses. Now: a tick where all of those hold takes the
  self-heal branch exclusively (unchanged from today - see below for
  why); a tick where they don't (occupied, nothing on, wait time not
  yet up, or no occupancy sensor at all) falls through to `default:`
  and reapplies lighting exactly as any other `adaptive_tick` does.

  **Deliberately still exclusive, not "do both in the same run" - a
  real, considered trade-off, not an oversight.** `entities_still_on`
  and `adaptive_target_entities` are `variables:`, computed once before
  `action:` runs at all; they don't reflect the self-heal branch's own
  `light.turn_off` yet. Reapplying lighting in the *same* run off that
  stale variable set would risk `apply_lighting` immediately turning
  the light it was just told to turn off back on. Confirmed as a real,
  not hypothetical, risk by deliberately building and running the
  unsafe version: moving the self-heal turn-off into a plain,
  non-exclusive `if:` at the top of `default:` (instead of its own
  exclusive `choose:` branch) reproduces exactly this - a light
  correctly turned off by self-heal gets an `apply_lighting` call for
  it moments later, in the same trace, confirmed by actually running
  this mutation and reading the resulting `ServiceCall`, not reasoned
  about in the abstract. Staying exclusive means a self-heal tick waits
  one more tick interval before resuming normal tracking - the same
  one-tick lag `reconcile` already had today, not a new limitation.

  `reconcile_interval` removed outright, not deprecated - confirmed via
  the real (not mocked) blueprint substitution path
  (`async_setup_component` in the integration test suite) that HA's
  blueprint substitution silently ignores a `use_blueprint.input:` key
  the blueprint no longer declares, rather than erroring the way a
  *missing required* input does (lesson 16 in this file) - so any
  live room automation with `reconcile_interval` explicitly set is
  unaffected by this change, the key just becomes inert. Four existing
  tests that passed `reconcile_interval="/5"` were updated to pass
  `adaptive_tick_interval="/5"` instead, preserving their actual
  timing intent rather than leaving a now-meaningless no-op kwarg.

  Two tests strengthened with the assertion that actually matters here:
  `test_reconcile_retries_turning_off_a_light_left_on_with_no_occupancy`
  now also asserts `apply_lighting_calls == []` (mutation-verified
  against the unsafe non-exclusive design described above - fails
  correctly, and only that test); `test_reconcile_does_nothing_while_occupied`
  now also asserts `apply_lighting_calls` is non-empty, confirming the
  merge didn't accidentally suppress the ordinary tick path when
  self-healing doesn't apply. Full suite: 151/151.

- **Three inputs renamed to stop overloading "Adaptive" as a generic
  prefix, plus the Brightness Scaling section - 2026-08-21, user's own
  direct pushback right after the reconcile merge shipped**: "we've got
  some weird field naming going on... Adaptive Tick Interval, Adaptive
  Tick Jitter, Adaptive Transition - none of these thing are actually
  adaptive - you've just stuck the word adaptive on everything!" Also
  flagged that Brightness Scaling no longer accurately describes its
  own contents, now that it holds the per-phase "lights off" pickers
  alongside the multiplier template - those are a binary exclude, not a
  scaling operation. Both fair - `adaptive_tick_interval`/
  `adaptive_tick_jitter` describe a *schedule* concern (self-healing,
  routine reapplication) that isn't specifically about the adaptive
  curve, and `adaptive_transition` is really about *who's* waiting on
  the update (nobody, vs. Motion On/Off's "someone's here right now"),
  not adaptiveness either.

  Renamed via `AskUserQuestion` against the user's own instinct (all
  four "Recommended" options were picked as-is): `adaptive_tick_interval`
  → `update_interval` ("Update Interval"), `adaptive_tick_jitter` →
  `update_jitter` ("Update Jitter"), `adaptive_transition` →
  `background_transition` ("Background Transition" - contrasts
  directly with Motion On/Off Transition), and the `brightness_scaling`
  section → `brightness_exclusions` ("Brightness & Exclusions").

  **A genuine breaking rename, not just cosmetic - user's own explicit
  call: "right now I'm the only person using this so don't worry about
  backwards compatability - you can fix my instance."** Renamed the
  actual `input:`/`!input` keys throughout (trigger `minutes:`,
  `variables:`, the jitter delay template, `script_transition`), not
  just the display `name:` labels - confirmed via `ha_config_get_automation`
  that `automation.dining_room_lighting` had `adaptive_transition: 0`
  explicitly set (the only one of the three with a real live override -
  `update_interval`/`update_jitter` were introduced only hours earlier
  the same day, in the two PRs directly above this entry, so nothing
  could have customised those yet). The internal trigger id
  (`id: adaptive_tick`) and its own template references
  (`trigger.id == 'adaptive_tick'`, used throughout `script_transition`/
  `scene_recheck_due`/the merged self-heal branch) were deliberately
  left unchanged - not user-visible in the UI editor the way the input
  labels are, and renaming it would have meant touching many more
  template conditions for no real benefit to the actual complaint.

  Full suite: 151/151, no mutation testing needed (a pure rename, no
  new logic). Live automations with the old key(s) set need fixing by
  hand after deploying - see the live-verification note for which ones
  and how, matching the user's own "fix my instance" instruction rather
  than leaving compatibility shims.

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

**`device_class: occupancy` doesn't mean the underlying sensor can
actually tell whether someone is still in the room - most of the
sensors carrying that device_class in this house are plain PIR motion
sensors, not true presence sensors, and that distinction matters for
understanding this blueprint's whole occupancy-timing design, not just
one incident.** A PIR sensor only detects *movement* - it reports clear
the moment motion stops being detected, including while someone is
sitting still right in front of it, then flips back to detected the
instant they move again. Rapid on/off flapping (see the dining room's
own history in the 2026-08-21 `reconcile` debounce entry below) isn't a
faulty or noisy sensor - it's the normal, expected behaviour of a PIR
sensor in a room where someone is present but not constantly moving.
`no_motion_wait` (Wait time) exists specifically to compensate for
this: it's not a cosmetic delay before turning lights off, it's the
mechanism that turns "motion stopped being reported a moment ago" into
a reasonable proxy for "the room is actually empty now." Only a
handful of sensors in this house are genuine presence sensors - real
Aqara FP2-style mmWave radar units, which can detect a still, silent
person by their breathing/micro-movement and correctly stay
"occupied" the whole time someone is in the room without needing to
move. Those don't need `no_motion_wait` to compensate for anything (a
real presence sensor going clear already means empty), but the
blueprint has no way to tell the two sensor types apart - both just
show up as `device_class: occupancy` - so Wait time is sized for the
PIR case everywhere, room by room, rather than assuming presence-grade
sensing by default.

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

- **A device's own delayed write confirmation could permanently lock a
  light out of adaptive control while it stayed continuously on - a
  genuinely new failure mode, distinct from every dropped-write/restart/
  recovery scenario already fixed, because it never touches unavailable/
  unknown at all. Found live, 2026-08-22, from a direct user report that
  the write-tracking sensor "doesn't seem right" for kitchen lights that
  were neither manually overridden nor failing to update.** Confirmed
  with live forensics before touching any code, per the user's own three
  hypotheses: `light.kitchen_3`/`light.kitchen_5` were genuinely,
  correctly lit at the Morning target (255 brightness, 6535K - their own
  device max, clamped below the nominal 6667K) the entire time, yet
  their write-tracking record hadn't moved in over an hour while every
  other light on the same automation, same tick, kept getting fresh
  writes every few minutes - and a real automation trace confirmed
  `apply_lighting`'s own `entities` list included both lights on every
  call, proving the exclusion was internal to `externally_set()`, not a
  blueprint-level omission.

  Root-caused with a standalone reproduction script against the real
  `grouping.py`/`write_tracking.py` algorithms (a plain-dict mirror of
  `async_record`, no HA needed) rather than continued guessing from live
  JSON snapshots - live forensics alone had produced two successive
  wrong theories (a hardware-model correlation, disproven by checking
  `kitchen_4`/`kitchen_7` - identical "Hue white ambiance GU10 with
  Bluetooth" model, behaving perfectly normally; and a drop/recovery-
  listener guard bug, which didn't match the observed data once traced
  through by hand) before the repro nailed the real mechanism directly:
  a light that stays "on" continuously, whose live `context.id` changes
  even once to something `write_tracking.py` never recorded - with
  *nothing* else about its reported value actually different - flips
  `externally_set()` permanently `True`, with no path back, because
  nothing here ever un-marks it short of the light turning off (the
  existing self-heal, `is_state(entity_id, "on")` returning `False`) or
  going through `unavailable`/`unknown` (the existing clear-on-drop
  listener). A light that just sits there, correctly lit, hits neither.

  The trigger for that first stray context is entirely mundane, not a
  hardware defect: confirmed against the pinned HA core source
  (`homeassistant/core.py`) that `Entity._context` expires exactly 5
  seconds after the service call that set it (the same fact this
  project already cited for the *restart*/device-recovery case, just
  not previously connected to an *ordinary*, healthy device). A real
  Zigbee/MQTT round-trip confirmation arriving after that window - mesh
  congestion, an extra hop, nothing wrong with the bulb - gets processed
  as a context-less write and lands under a brand-new, unrelated
  context, even though it's echoing exactly the value that was asked
  for. `kitchen_4`/`kitchen_7` not being stuck too isn't evidence
  against this - it's a per-tick timing coincidence, not a hardware
  property, so it doesn't reproduce on demand and doesn't need to.

  **Fixed by teaching `externally_set()` a value-based fallback, not by
  touching the context-comparison mechanism itself.** Each stored claim
  (`_ContextClaim` in `write_tracking.py`) now also records `target` -
  what that specific write actually asked for
  (`{"brightness": ..., "color_temp_kelvin": ...}` or `{"brightness":
  ..., "rgb_color": [...]}`), populated by `apply_lighting`'s own
  dispatch loop in `__init__.py` per group as it already builds
  `written_entities`. `grouping.py`'s `externally_set()` gains one more
  step before concluding "genuinely external": if the light's *current*
  reported values still match `pending`'s own recorded `target` within
  the same tolerance `_already_set`/`_already_set_rgb` already use, this
  reads as our own write's delayed echo, not a real touch - rescued back
  to "not externally set," not just to a one-off allowance, so the
  *next* tick treats it completely normally, including writing a
  genuinely different value once the curve moves on. The comparison
  itself (`target_matches_values`) is a small, pure function taking
  plain values, not an `EntityLookup`/entity_id - deliberately factored
  out so `sensor.py`'s diagnostic status can reuse the *exact* same
  logic (see below), rather than the two drifting apart over time.

  **Deliberately checked against `pending` (the most recent attempt),
  not `confirmed`** - `pending` is what a given echo would actually be
  confirming; comparing against the older, possibly long-stale
  `confirmed` value instead would rescue a light whose target has
  already moved on, which is exactly the outcome this fix needs to
  avoid.

  A first attempt at a test for this shipped with a real gap, caught
  before landing by checking the assertion actually discriminates
  the bug (this project's standing mutation-testing habit) rather than
  assuming a "before/after" pair means it does: the initial version's
  "rescued" case asserted the light stayed excluded from `combined`
  both with and without the fix - true either way, since a light
  already at its (unchanged) target isn't written regardless of whether
  it's "excluded" or merely "already correct." The real, only-with-a-
  broken-fix-visible case needs the target to have moved on since the
  echo - `test_context_mismatch_with_stale_target_still_updates_once_the_curve_moves`
  in `tests/test_grouping.py` is the one that actually fails under
  mutation (reverting the fallback back to a bare `return True`), and is
  the only one of the five new `test_grouping.py` cases that does - the
  other four (forgiven-with-matching-values, still-external-with-
  different-values, no-recorded-target-stays-external, the RGB variant)
  remain valid regression/documentation coverage but don't discriminate
  *this specific* change on their own, which is expected and fine, not
  a gap. The same lesson applied to
  `tests/integration/test_services.py`'s end-to-end version
  (`test_a_devices_own_delayed_echo_does_not_permanently_lock_the_light_out`):
  its first draft's simulated "device echo" reused the identical
  brightness/color_temp as the light's current state, which HA collapses
  into a no-context-change `state_reported` event rather than a real
  state change (the identical gotcha this file's own
  `test_force_bypasses_protection_and_reclaims_ownership` already
  documents) - so the echo silently never happened at all, and the test
  passed under mutation for the wrong reason. Fixed by echoing a value
  within tolerance but not identical (201/3005 against a 200/3000
  target), which HA does treat as a real change. Both fixes confirmed by
  actually applying the mutation and rerunning, not reasoned about in
  the abstract. Full suite: 158/158.

  `write_tracking.py`'s module docstring and `EntityLookup.externally_set()`'s
  own docstring both carry the full reasoning (the 5-second window, why
  `pending` and not `confirmed`, the live incident) - `docs/HELPERS.md`'s
  "Override protection" section gained a matching paragraph and a new
  step 7 exception, rather than restating it from scratch.

- **`sensor.adaptive_lighting_write_tracking`'s `status` values and the
  write-tracking dashboard card's terminology renamed the same day,
  prompted directly by the incident above - user's own follow-on
  feedback: "confirmed should be 'controlled', 'pending' is fine, then
  we need a word that means 'been controlled by something else', ideally
  with hover text on each to explain what it means."** `confirmed` →
  `controlled` (now also covers the value-match rescue above - that
  light genuinely isn't excluded from the next tick either, so folding
  it into the same word is accurate, not a new fifth status invented for
  a narrow case), `mismatched` → `overridden`, `pending` unchanged. The
  raw claim *names* in the data model (`confirmed`/`pending` - a write
  earlier observed landing vs. the most recent attempt) are deliberately
  left alone; only the computed `status` word and the card's own labels
  changed - the claim names are a different concept (which stored slot)
  from the status verdict (what it means right now), and conflating them
  would have been a regression, not a simplification.

  `www/adaptive-lighting-write-tracking-card.js` gained a `STATUS_TOOLTIP`
  map wired to each badge's native `title` attribute (`cursor: help`
  added to `.status-badge` as the visual affordance), plus tooltips on
  the Status/Confirmed/Pending column headers themselves - the latter
  explain what the raw claim *slots* mean, which needed saying somewhere
  since renaming `status` alone doesn't explain why a "Controlled" light
  can still show a `Pending` claim next to it. Verified via the Browser
  pane against `dashboard/preview.html` (served over HTTP via
  `.claude/launch.json`'s `dashboard-preview` config, not `file://` -
  the latter renders as a static snapshot with no script execution, per
  this project's own established preview workflow) - confirmed all four
  tooltip strings and both new CSS class names
  (`status-controlled`/`status-overridden`) are wired correctly by
  reading the rendered shadow DOM directly, not just visually (a native
  `title` tooltip doesn't reliably appear in a headless screenshot
  either way). `dashboard/preview.html`'s synthetic seed data updated to
  the new status strings, plus a new `light.kitchen_3` entry
  demonstrating the value-match-rescued "Controlled" case specifically
  (a `context.id` that matches neither claim, `target`s that do) rather
  than only the plain context-match case the existing entries already
  covered. `tests/integration/test_write_tracking_sensor.py`'s two
  renamed tests plus one new one
  (`test_status_controlled_when_context_mismatches_but_value_matches_pendings_target`)
  mutation-verified the same way as above. Full suite: 158/158.

- **Override protection extracted into its own standalone module and
  two new services (`check_ownership`/`record_ownership`) - and, as a
  direct structural consequence, a real off-light misclassification bug
  fixed for good rather than patched. 2026-08-22, same day, prompted by
  the user reviewing the just-shipped terminology rename and reporting
  "loads of lights are marked as overridden in the UI but shouldn't be,
  and seem to be working correctly."**

  **The bug, confirmed live before writing any code**: 6/6 sampled
  lights reading `status: "overridden"` were all genuinely, harmlessly
  `state: "off"` (the whole `dining_room_1..8`/`extension_left_1..6`
  batch, switched off together under one shared `motion_off` context -
  a code path outside `apply_lighting` whose context `write_tracking.py`
  never records). Root cause: `grouping.py`'s `EntityLookup.externally_set()`
  - the check `apply_lighting` actually uses - has always started with
  `if not self.is_state(entity_id, "on"): return False`, but
  `sensor.py`'s separately-maintained status classification never
  mirrored that precondition, so an off light with no matching claim
  fell straight into the same final `else` branch a *genuinely*
  overridden light does. This is exactly the class of bug this
  session's earlier `target`/rescue fix was itself trying to prevent
  (two copies of "what does this claim mean" drifting apart) - just a
  second instance of it, in the other direction.

  **Rather than patch `sensor.py`'s branch list a second time, the user
  asked a bigger question first**: "currently I think all this owner
  tracking is part of the adaptive lighting right? can we pull it into
  its own thing? ... make that its own service, that just happens to
  get natively used by the adaptive lighting service ... might make it
  easier to test and debug." Confirmed via `AskUserQuestion`: stays in
  this same integration (no new HACS install step - matches how
  `compute_scene_coverage` was already built as standalone-useful
  inside this same package); ships as two services mirroring the
  existing `compute_lighting_groups`/`apply_lighting` read/write split;
  the light-specific `target` shape (`{brightness, color_temp_kelvin,
  rgb_color}`) stays as-is rather than generalising to arbitrary entity
  attributes - a relocation, not a redesign.

  **New module: `override_protection.py`** (pure, no `hass` import,
  same pattern as `curve.py`/`scenes.py`). Holds `_ContextClaim`/
  `_WriteRecord` (moved from `write_tracking.py`, which now imports
  them back), `target_matches_values()` (moved from `grouping.py`
  verbatim), and two new functions extracted from
  `EntityLookup.externally_set()`'s own body:
  - `classify(is_on, confirmed, pending, current_context,
    current_brightness, current_color_temp_kelvin, current_rgb_color,
    ...) -> (status, claim_owner_id)` - the actual decision table,
    returning one of `"off"` (checked first, before any claim - the
    fix), `"unclaimed"` (no claim at all, or only an unconfirmed first
    attempt - today's existing leniency), `"pending"`, `"controlled"`
    (context match *or* the delayed-echo rescue), `"overridden"`.
  - `is_blocked(status, claim_owner_id, owner_id, force) -> bool` - the
    remaining "is this blocked *for this specific caller*" step
    (force/owner_id-None bypasses, then the owner-conflict check),
    factored out separately from `classify()` so the raw classification
    stays reusable by something that only wants to *display* a claim
    (the sensor, which shows every owner regardless of who's asking)
    without also needing a specific caller's owner_id.

  **Bundled correctness fix, found while moving this logic, not
  designed in from the start**: the delayed-echo rescue used to return
  "not externally set" completely unconditionally, with zero owner
  check - unlike the two context-match branches, which both always
  checked ownership first. A light whose live value happened to match
  a *different* owner's `pending` target would have incorrectly read
  as free-to-manage for anyone asking. `classify()`'s rescue branch now
  returns `pending`'s own owner instead of `None`, so all three matched
  cases go through the identical `is_blocked()` owner check uniformly.
  Mutation-verified at both layers: a pure `classify()` test and a new
  `build_groups()`-level test
  (`test_context_mismatch_rescue_still_checks_owner_id`, deliberately
  asking for a target the light *isn't* already at, so `_already_set()`
  can't mask the difference the way an identical-target version of this
  test would have) both fail correctly when the fix is reverted, and
  only those two.

  **`grouping.py`'s `EntityLookup.externally_set()` becomes a thin
  adapter** - gathers plain values via its own existing injected
  closures, calls `classify()`/`is_blocked()`, done. Deliberately kept
  its exact signature, and `EntityLookup`'s dataclass fields and
  `tests/fakes.py`'s `make_lookup()` signature untouched - this is what
  let all 20 tracking-related `test_grouping.py` tests (of 38 total)
  keep passing with zero edits, proof the refactor genuinely didn't
  change `build_groups()`'s observable behaviour (beyond the one
  deliberate owner-check fix above). Bare-module import gotcha: since
  `tests/conftest.py` puts `custom_components/adaptive_lighting_helpers/`
  directly on `sys.path` so `test_grouping.py` can import `grouping`
  without pulling in `homeassistant` (see that file's own comment), a
  plain `from .override_protection import ...` inside `grouping.py`
  would fail in that bare-module context (no parent package to resolve
  the dot against) while working fine in production/`tests/integration/`
  - fixed with a `try: from .override_protection import ... except
  ImportError: from override_protection import ...` fallback, the
  standard pattern for a module that needs to work both ways.
  `write_tracking.py` needed no such handling - it's never imported
  bare (it needs a real `hass` regardless), so a plain relative import
  is fine there.

  **`sensor.py`'s status property now also calls `classify()`** instead
  of its own separately-maintained comparison chain - this is what
  fixes the off-light bug structurally rather than as a one-off patch:
  the sensor and `externally_set()` are now provably looking at the
  same table, so they can't drift apart silently again the way they
  already had once. `classify()`'s `"unclaimed"` status displays as
  `"controlled"` (unchanged from today's behaviour - a claim-free
  entity was never a concern), `"off"` is new. Two tests added
  (`test_status_off_when_the_light_is_legitimately_off`,
  `test_status_off_takes_precedence_over_a_stale_context_match` - the
  latter locks in branch *order*, not just presence, by deliberately
  reusing a stale claim's own `context_id` for the off-transition).
  Mutation-verified by removing `classify()`'s `is_on` guard entirely:
  failed exactly those two tests, the equivalent new pure
  `test_override_protection.py` test, *and* the pre-existing
  `test_grouping.py::test_turning_the_light_off_ends_the_protection` -
  confirming the shared logic genuinely protects both consumers now,
  not just the one being tested at the time.

  **Two new services, `check_ownership` (read-only,
  `supports_response: ONLY`) and `record_ownership`** (side-effecting,
  context read from `call.context.id` automatically, matching
  `apply_lighting`'s own convention) - registered in `__init__.py`,
  documented in `services.yaml` following the existing
  `compute_lighting_groups`/`apply_lighting` field conventions exactly.
  Deliberately built directly against `write_tracker`/`hass.states`
  rather than constructing a full `EntityLookup` first - `check_ownership`
  has no need for `EntityLookup`'s brightness-bucketing concerns
  (`reachable`/`tags`/`supports_rgb`), only the five tracking-related
  values `classify()` itself needs. `apply_lighting`/`build_groups()`
  keep calling the underlying Python functions directly (not through
  `hass.services.async_call`) - per the user's own framing, these are a
  parallel, independently-callable surface onto the same functions, not
  a new indirection layer `apply_lighting` has to pay for.

  Four new integration tests in `test_services.py` cover: a brand-new
  entity reading `"unclaimed"`; a full `check_ownership`/
  `record_ownership` round trip - our own write read back as not
  blocked, then a genuine external change reading as blocked, then a
  *different* `owner_id` correctly still seeing that same matched claim
  as blocked, all in one sequence (had to be corrected mid-write - a
  first draft's assertions expected `"controlled"` after the first ever
  write to an entity, but that write's `live_context_before_write` and
  the recording call's own `context.id` happened to be identical in the
  test's own setup, so the synthetic first-write baseline recorded
  under `confirmed` and the real `pending` claim ended up sharing one
  context.id - `classify()` correctly reports `"pending"` there, checked
  first per its own precedence; not a bug, a wrong test expectation,
  caught by actually running it rather than assumed correct); `force`
  bypassing regardless of claims; and the off-light case at the service
  layer specifically (mutation-verified separately from the
  sensor/pure-function versions, by forcing `is_on=True` inside the
  service handler itself - confirms the handler's own plumbing, not
  just `classify()`'s logic underneath it, which was already proven).

  Docs: `docs/HELPERS.md` gained a new "Using override protection
  standalone" section (with a real YAML example for both services)
  between "A plain Home Assistant restart..." and "Inspecting
  write-tracking state", plus the `off` status bullet; "Four services"
  in the file's own intro became "Six". `dashboard/preview.html` gained
  a `light.dining_room_1` entry demonstrating `status: 'off'`
  (mirroring the real incident); the dashboard card's `STATUS_LABEL`/
  `STATUS_TOOLTIP`/`STATUS_ORDER`/CSS all gained the new status, `off`
  placed last in `STATUS_ORDER` (more inert even than `controlled` -
  nothing is being actively managed at all). Verified in-browser via
  `dashboard/preview.html` (served over HTTP, not `file://`) by reading
  the rendered shadow DOM directly for the new badge's label/class/
  tooltip and correct sort position - same method established earlier
  the same day, since this environment's own screenshot capture had
  already been found unreliable for this kind of check.

  Full suite: 181/181 (158 baseline + 16 new pure `override_protection`
  tests (11 for `classify()`/`target_matches_values`, 5 for
  `is_blocked()`) + 2 sensor off-light tests + 1 `build_groups`
  owner-check-on-rescue test + 4 new `check_ownership`/
  `record_ownership` integration tests, all mutation-verified where the
  change was behavioural rather than a pure relocation).

- **A genuinely new, self-reinforcing lockout found live the same day
  PR #79 shipped, 2026-08-22 - "overridden" is a dead end once
  `build_groups()` starts excluding an entity, not just a transient
  misclassification.** User report, right after confirming PR #79 fixed
  the off-light bug: "there's still some uncontrolled lights though
  that I think should be." Diagnosed via the same live-forensics
  method used throughout this project (read `sensor.adaptive_lighting_write_tracking`,
  cross-reference against real light state, don't guess) - seven
  kitchen lights (`light.kitchen_1`/`_3`/`_4`/`_6`, `light.kitchen_pendant_2`,
  `light.kitchen_strip_back`, `light.extension_right_3`) all showed
  `overridden` while genuinely, correctly lit (brightness 255,
  color_temp_kelvin 5347 - matching every sibling light in the same
  room, same tick). Their `pending` claims all shared one target
  (255/5336, recorded at a single timestamp) while siblings' `pending`
  claims kept advancing every tick - the tell that these seven had
  simply stopped receiving writes altogether, not that they were being
  written and rejected.

  Root cause, confirmed by reading `grouping.py`'s `build_groups()`
  directly rather than assumed: every one of `needing_off`/`combined`/
  `combined_rgb`/`two_step`/`two_step_rgb` filters through `not
  lookup.externally_set(...)` - once `classify()` returns `overridden`
  for an entity, it's excluded from every group `apply_lighting`
  dispatches, which means `write_tracker.async_record()` (only ever
  called for entities actually written - see `__init__.py`'s
  `apply_lighting`) never runs for it again either. The value-rescue
  mechanism PR #78 added (comparing current values against `pending`'s
  own recorded `target`) depends on that target staying roughly current
  - but once excluded, `pending` is frozen at whatever it was the
  instant exclusion began, while a ramping curve (Day's Kelvin ramp, in
  this case) keeps moving the real target further away every tick. The
  seven kitchen lights' shared `pending.target` (5336K, recorded at
  12:59:40) drifted a single Kelvin outside `color_temp_tolerance`
  (default 10) by the time they were checked (live 5347K, 11K off) -
  once that happened, exclusion became permanent: nothing left could
  ever record a fresher target for them again, on a curve that only
  moves in one direction for the rest of Day. Genuinely worse than the
  PR #78 bug it built on top of: that one was a transient window (fixed
  by the rescue); this one is the rescue's own precondition rotting the
  instant the light it's meant to protect gets excluded, with no
  self-healing path except the light turning off, going through a
  genuine unavailable/recovery cycle, or a `force` call - none of which
  happen on their own during continuous, correct, uneventful operation.

  **Not fixed structurally this session** - flagged to the user
  alongside the two features requested in the same message, which
  together happen to be the practical mitigation: a manual escape
  hatch rather than a deeper redesign of when `pending` gets refreshed
  for an excluded entity. Revisit if this keeps recurring - the
  underlying rot (excluded entities never get a fresh `pending`) is
  still there and will keep producing new lockouts on every ramping
  phase, just currently patched over by making the lockout *recoverable*
  rather than *permanent*.

- **`clear_ownership` service + a "Clear" button/column on the
  write-tracking dashboard card, plus a top-level `owner_id` field on
  the sensor's per-entity dict - 2026-08-22, the user's own explicit
  ask, landing in the same turn as the lockout diagnosis above** ("maybe
  we should add a 'clear ownership' to the dashboard - oh and can we
  see the owner id that owns it too?").

  `write_tracking.py`'s `LastWriteTracker` gained `async_clear(entity_ids)`
  - pops each entity's record entirely (a no-op for one with no record),
  persists, and fires `SIGNAL_WRITE_TRACKING_UPDATED` the same way every
  other mutating method does. `__init__.py` wraps it as
  `adaptive_lighting_helpers.clear_ownership` (`entities` required, no
  other fields - there's nothing to configure about discarding a
  record), following the same registration/schema pattern as
  `check_ownership`/`record_ownership`. **Caught and fixed in passing
  while touching this area**: `async_unload_entry` never removed
  `check_ownership`/`record_ownership` on unload (a gap present since
  PR #79 shipped, never exercised since nothing in the test suite
  reloads the entry after adding them) - fixed alongside registering
  `clear_ownership`'s own removal, all three added to the existing
  `hass.services.async_remove(...)` block.

  `sensor.py`'s `_WriteTrackingSensor.extra_state_attributes` now
  captures `classify()`'s second return value (previously discarded as
  `_claim_owner`) and surfaces it as a top-level `owner_id` per entity -
  whichever claim's owner actually produced the current `status`
  (`null` for `off`/`unavailable`/`overridden`/an unclaimed
  `controlled`, matching `check_ownership`'s own `owner_id` semantics
  for the identical entity). This was a real, if minor, gap: previously
  a viewer had to manually check which of `confirmed`/`pending` matched
  the live context to answer "who owns this light right now" - now it's
  answered directly.

  `www/adaptive-lighting-write-tracking-card.js` gained an "Owner"
  column (between Status and Confirmed, using the existing
  `friendlyOwner()` helper) and an "Actions" column with a per-row
  "Clear" button - `window.confirm()`-gated (the action briefly removes
  override protection for that light, unlike the existing zero-friction
  "Trace" button, which is read-only), calling `clear_ownership` via
  `hass.callService` and relying on the resulting `SIGNAL_WRITE_TRACKING_UPDATED`
  push to refresh the row rather than a manual re-render on success. A
  failed call surfaces inline in that row (`_clearErrors`, a
  `Map<entity_id, message>`) rather than through the existing
  `_renderError` path, which replaces the *entire* card - appropriate
  for "the one entity this sensor represents is missing" (its only
  existing caller), wrong for "one row's service call failed" (would
  wipe every other row's live data along with it). The filter box's
  existing haystack search gained `record.owner_id` alongside the two
  claims' own owner fields, so filtering by automation name still finds
  a light via its currently-winning owner even when that happens to be
  the rescue case (context mismatch, value match) rather than a direct
  claim match.

  `dashboard/preview.html`'s six synthetic entities all gained an
  `owner_id` field matching what `classify()` would actually compute
  for their given `status`/claims, plus a mocked `callService` for
  `clear_ownership` (deletes the entity from the synthetic
  `entities` object and pushes a fresh state, mirroring the real
  sensor's own push-on-clear behaviour). Verified in-browser via
  `dashboard/preview.html` (served over HTTP - `.claude/launch.json`'s
  `dashboard-preview` config - not `file://`, same established
  requirement): confirmed the Owner column renders the right friendly
  name per row (or `—` when `null`) via direct shadow-DOM inspection,
  and exercised the full clear flow end-to-end by scripting a click
  with `window.confirm` stubbed to auto-accept - button read
  "Clearing…" and was disabled mid-call, and `light.study_pendant`
  (the `overridden` row) was gone from the rendered table after the
  mocked service call resolved, matching what a real `clear_ownership`
  round trip does.

  Two new integration tests
  (`test_clear_ownership_frees_a_light_stuck_overridden`,
  `test_clear_ownership_is_a_noop_for_an_untracked_entity`) plus two
  sensor tests (`test_owner_id_surfaces_whichever_claim_currently_matches`,
  `test_owner_id_is_none_when_nothing_currently_matches`), both new
  behavioural pieces mutation-verified: a no-op `async_clear` fails
  exactly the "frees a light" test; discarding `classify()`'s owner
  return value in `sensor.py` fails exactly the "surfaces" test, not
  the "is none" one (expected - a mutation that always returns `None`
  is indistinguishable from correct behaviour on a case that's already
  `None`). Full suite: 185/185.

- **Root cause of the recurring "overridden but genuinely fine" reports
  finally found - Kelvin/mired unit-conversion rounding, not curve
  drift - 2026-08-22, same day as `clear_ownership` shipped.** User
  spotted `light.dining_room_1` showing `overridden` minutes after
  restart and pushed back hard, correctly, on the first two
  explanations offered: a "the curve moved on since the frozen
  `pending` target was recorded" theory didn't survive scrutiny once
  the user asked the obvious question - a light that isn't being
  rewritten can't be tracking a moving curve, its value should stay
  frozen at exactly what it was last told, not drift. That pushback is
  what led to the real answer, not the first two attempts.

  Traced `light.kitchen_2` through HA's actual logbook
  (`logbook/get_events` by context_id, the same mechanism the
  write-tracking card's own "Trace" button uses) rather than continuing
  to theorise: its `pending` write was a real, logbook-attributed
  `automation.kitchen_lights` command (target `4373K`); the light's own
  live context appeared 7 seconds later with **zero logbook
  attribution** - no automation, no service call, nothing - the
  signature of the device's own asynchronous confirmation, not a human
  touch. Root-caused precisely by reading HA core's actual, pinned
  source (`.venv-integration`), not guessed:

  ```python
  # homeassistant/util/color.py
  def color_temperature_kelvin_to_mired(kelvin_temperature): return math.floor(1000000 / kelvin_temperature)
  def color_temperature_mired_to_kelvin(mired_temperature): return math.floor(1000000 / mired_temperature)
  ```

  Zigbee bulbs communicate colour temperature in **mireds**
  (`1,000,000/kelvin`, always a whole number), not Kelvin. HA converts
  a Kelvin ask to mireds before sending it to the device (lossy
  `floor`), and converts the device's own reported mireds back to
  Kelvin for display (lossy again). Ran the real numbers:
  `floor(1000000/4373) = 228` mireds → `floor(1000000/228) = 4385K` -
  **exactly** the value observed live, not close, not a coincidence.
  The bulb did precisely what it was told; `4373` and `4385` are two
  different Kelvin-space labels for the identical underlying mired
  value, the way "12 inches" and "1 foot" are the same length. The
  existing flat `abs(current - target) <= color_temp_tolerance`
  comparison (10K default) has no notion of this seam, and in this
  colour range a single mired step is worth ~20K+ - comfortably enough
  to blow through the tolerance from device rounding alone, with zero
  drift and zero external touch involved. (`light.dining_room_1`'s own
  exact numbers didn't cleanly reconstruct against the specific writes
  visible in its history - plausibly an earlier write outside that
  visibility window - but the *mechanism*, proven exactly for
  `kitchen_2`, is more than large enough to explain gaps of this size
  on its own.)

  Confirmed via direct follow-up questions from the user before any
  code changed, not assumed: `confirmed`'s own claim already carries
  `target` (brightness/colour) from whenever it was written -
  `write_tracking.py`'s promotion logic (`confirmed = old_pending`)
  carries the whole claim object forward, target included, nothing to
  fix there. RGB is unaffected by this specific bug - RGB values are
  sent directly with no lossy Kelvin-style conversion in the middle.
  The blueprint already passes a fully-qualified `owner_id` (`{{
  this.entity_id }}`, e.g. `automation.dining_room_lighting`) -
  confirmed by reading the blueprint file directly, no blueprint change
  needed for the clickable-owner card feature below.

  Also surfaced, independently, while investigating: `sensor.py`'s
  status-across-the-house snapshot showed `controlled: 0` out of 57
  tracked lights at one point, which read as alarming until worked
  through with the user - `pending` status is *by definition* "the
  light's live context currently matches exactly what we last wrote",
  so every `pending` light is already direct proof the context-id match
  is working correctly; during a continuously-ramping Day phase nearly
  every light gets rewritten nearly every tick, so there's rarely a
  quiet window long enough to observe the "independently reconfirmed"
  `controlled` label before the next write happens. `pending` and
  `controlled` are both "not blocked" functionally - only `overridden`
  actually means something went wrong. Not a bug, just a genuinely
  confusing number to see without this context - worth remembering
  before treating a low `controlled` count as a symptom on its own.

  **Fix, in two places that both compare a live Kelvin value against a
  target with a flat tolerance** - `override_protection.target_matches_values()`
  (the delayed-echo rescue) and `grouping._already_set()` (the "does
  this light even need a new command this tick" check, run for *every*
  light, *every* tick - the one that matters more day-to-day, since a
  light whose rounded value doesn't happen to land within tolerance of
  a freshly-computed target would get needlessly re-commanded
  indefinitely, plausibly contributing to the "too many firings"/Zigbee
  traffic concerns raised earlier this session, not just the
  override-protection false-positive specifically):

  ```python
  def _color_temp_matches(current_kelvin, target_kelvin, tolerance_kelvin):
      if abs(current_kelvin - target_kelvin) <= tolerance_kelvin:
          return True
      return _kelvin_to_mired(current_kelvin) == _kelvin_to_mired(target_kelvin)
  ```

  Deliberately additive, not a replacement - `color_temp_tolerance`
  keeps its existing meaning and default (10K) unchanged; this just
  also recognises the device-rounding-identical case as a match, which
  a bigger flat tolerance couldn't reliably do on its own (a single
  mired step is ~5K near 2700K but ~20K+ near 4500K - wildly
  inconsistent across the range this project's curve actually spans).

  **Real design correction, mid-implementation, user-caught**: the
  first draft *reimplemented* the two-line mired conversion inline
  rather than importing `homeassistant.util.color`, matching
  `curve.py`'s own precedent (`kelvin_to_rgb`, independently
  implemented) and the stated "grouping.py/override_protection.py have
  no Home Assistant dependency" principle. User pushed back directly:
  "this code is designed to live in HA, we should use their libraries
  if available" - and offered the fallback of mocking the function for
  tests, or just accepting HA as a test dependency, if that's what was
  blocking it. Verified before deciding, not assumed either way:
  `homeassistant.util.color` alone is cheap to import (144 modules,
  ~38ms, nothing from `core`/`config_entries`/`aiohttp` - none of the
  event-loop/component-loading machinery), and the "these tests run
  without Home Assistant installed" property the reimplementation was
  nominally protecting turns out to already be false today regardless -
  confirmed live by running `pytest tests/test_grouping.py
  tests/test_override_protection.py` directly and watching it still
  fail with `No module named 'pytest_homeassistant_custom_component'`,
  because `pytest`'s own `testpaths = ["tests"]` config collects every
  `conftest.py` under `tests/` - including `tests/integration/conftest.py`,
  which requires the real HA test plugin - regardless of which specific
  file was targeted. So the "no HA dependency" bare-module design for
  these two files was already not buying the thing it was believed to
  buy; importing `homeassistant.util.color` directly costs nothing
  practical in exchange for using the exact, single-source-of-truth
  conversion HA itself relies on (impossible to drift from real device
  behaviour if HA's own rounding ever changes). `curve.py`'s
  `kelvin_to_rgb` precedent doesn't actually generalise here either -
  it likely has no direct HA equivalent to import in the first place,
  so it isn't evidence this codebase has a blanket "avoid HA imports"
  rule.

  `classify()` also gained a third return value, `matched_via` (`"context"`
  when the live context.id matched a claim directly, `"value"` when the
  delayed-echo/mired rescue is what actually produced a `pending`/
  `controlled` status, `null` for every other status) - genuinely new
  information `status` alone didn't carry, both matched cases reported
  `"controlled"` identically otherwise. Threaded through every caller:
  `grouping.py`'s `externally_set()` (unpacks the 3-tuple, doesn't
  change its own `bool` return), `check_ownership`'s response (`{...,
  "matched_via": ...}`), and `sensor.py`'s per-entity dict. The
  **Adaptive Lighting Write Tracking** dashboard card shows this as a
  small annotation under each status badge ("context matched" /
  "colour/brightness matched"), with its own tooltip explaining what
  each means - the user's own ask: "can we update the card so that it
  shows why we think it's in that state." The Owner column also became
  a clickable link (reusing the same `hass-more-info` dispatch the
  light-cell already used) whenever `owner_id` resolves to a real, live
  entity - falls back to today's plain text otherwise, since
  `check_ownership`/`record_ownership` accept an arbitrary free-text
  `owner_id` from callers other than the blueprint.

  Mutation-verified both new behavioural pieces: reverting
  `_already_set`'s mired-aware comparison back to the plain `abs(...)
  <= tolerance` check fails exactly `test_a_mired_equivalent_color_temp_is_treated_as_already_set`
  and no other `test_grouping.py` test; swapping `classify()`'s
  rescue-branch `matched_via` literal from `"value"` to `"context"`
  fails exactly the two tests asserting that specific value
  (`test_context_matches_neither_but_value_matches_pendings_target`,
  `test_status_controlled_when_context_mismatches_but_value_matches_pendings_target`)
  and nothing else. Verified the new card behaviour directly in-browser
  against `dashboard/preview.html` (served over HTTP): the matched-via
  annotation text/tooltip render correctly per status, the owner cell
  correctly distinguishes a resolvable entity (clickable, dispatches
  `hass-more-info` with exactly that entity_id, no double-firing
  alongside the row's own light-cell click) from an unresolvable
  free-text owner (plain text, both cases deliberately present in the
  same preview render). Full suite: 190/190.

- **The write-tracking card's "Clear" button lost its confirmation
  prompt, 2026-08-22, the same day it shipped.** User's own call,
  verbatim: "we're not performing some horrible unrecoverable action
  we're letting a light get updated." Correct reframe of what clearing
  actually is - it discards a diagnostic bookkeeping record, not the
  light itself, and a fresh claim re-establishes automatically the
  moment anything next writes to that entity (the very next regular
  tick, in practice). Unlike this project's other confirmation-worthy
  actions (none currently exist on this card), there's no real
  "oops" scenario clearing creates that regular operation wouldn't
  already fix on its own. Docs updated to match
  (`docs/HELPERS.md`'s "Using override protection standalone"
  section).

- **`write_tracking.py` gained a genuine expiry mechanism -
  `async_prune_stale()` - 2026-08-22, prompted directly by cleaning up
  real leftover Zigbee2MQTT groups in the live house.** After removing
  `light.ensuite_spots` (a deleted Zigbee2MQTT group device, still
  lingering in HA's registry - a real Zigbee/HA-registry cleanup, not
  this integration's concern) the user separately flagged
  `light.extension_spots_left`, which turned out to be a *different*
  kind of leftover entirely: not a stray HA registry entry at all (it
  had already been fully deleted, `ha_get_entity` returned "not found"),
  but a stale record in *this integration's own* `write_tracking.py`
  Store, for an entity Home Assistant had otherwise completely
  forgotten. Root cause: every existing cleanup path
  (`async_start_listening`'s drop-detection,
  `async_resync_to_live_state`'s startup pass) depends on
  `hass.states.get(entity_id)` returning *something* to act on - even
  just `unavailable`. An entity deleted outright returns `None`
  forever, silently skipped by both paths, with nothing left in Home
  Assistant that could ever prompt the record's removal.

  User's own framing, directly: "we need a timeout in our state
  tracking - we don't wanna hold onto random state forever." First
  draft shipped internally (never merged) with a 30-day cutoff,
  reasoned from "how long could a legitimately-idle light plausibly go
  without a real write" - **user corrected this immediately, and the
  correction is the more important part**: "last seen makes me think
  you're overthinking it... if we've got a record that's not been
  updated in a day then I doubt we care about it." Re-derived properly
  in response: pruning a record is **never actually risky**, regardless
  of how soon it happens - `classify()` treats "no record at all"
  identically to `"unclaimed"` (see its own docstring), never as
  blocked, so a record pruned too early for a still-real light just
  makes it look brand-new again, re-established normally on its very
  next write. There's no lockout to weigh against being aggressive
  here, unlike nearly every other decision in this module - the 30-day
  reasoning was solving for a risk that doesn't exist. Shipped with
  `STALE_RECORD_MAX_AGE_DAYS = 1` instead.

  A one-day cutoff only means something if it's actually checked more
  than once a restart, though - a startup-only pass would let a record
  sit stale for as long as HA happens to stay up between restarts.
  Added `PRUNE_CHECK_INTERVAL` (1 hour) via `async_track_time_interval`
  in `__init__.py`, alongside the existing startup call (which still
  matters on its own - it catches anything that went stale while HA
  was down). **Real test failure caught and fixed, not assumed
  correct**: the periodic timer registration
  (`entry.async_on_unload(async_track_time_interval(...))`) initially
  failed nearly every integration test in the suite with "Lingering
  timer after job ... _periodic_prune" - `tests/integration/test_services.py`'s
  own `setup_integration` fixture calls `async_setup_entry` directly
  and never unloads the entry afterward (deliberately, per its own
  docstring, to avoid resolving the `frontend`/`http` manifest
  dependencies), so `entry.async_on_unload`'s callbacks never fire in
  these tests, and pytest-homeassistant-custom-component's own teardown
  fixture asserts against exactly this: a real `asyncio.TimerHandle`
  still scheduled on the event loop after a test ends. Root-caused
  against HA core's own `_TrackTimeInterval` source rather than
  guessed: `cancel_on_shutdown=True` on the job is checked explicitly
  by that same lingering-timer assertion (`if job.cancel_on_shutdown:
  continue`) - and is also the semantically correct production setting
  regardless of the test, not just a workaround: a periodic maintenance
  timer has no reason to keep rescheduling itself once Home Assistant
  itself is shutting down, restart or not.

  New `last_seen` field added to the shared `_WriteRecord` shape
  (`override_protection.py`, alongside `confirmed`/`pending` - unused
  by `classify()`, pure `write_tracking.py` bookkeeping), stamped by
  every place that already writes or observes a record
  (`async_record()`, `_snapshot_confirmed()` - both its startup-resync
  and live-recovery callers). Pre-existing persisted records (from
  before this field existed) are backfilled to *now*, not left missing
  or backdated, on `async_load()` - deliberately conservative on this
  one specific point even though pruning-too-early is otherwise
  harmless, since immediately mass-pruning every already-tracked
  light's diagnostic history the very first time this loads on an
  upgraded install would be a needlessly disruptive way to roll this
  out, however harmless the underlying effect actually is.

  Four new tests in `tests/integration/test_services.py`
  (`test_async_load_backfills_last_seen_for_legacy_records_without_it`,
  `test_prune_stale_removes_a_record_untouched_past_the_cutoff`,
  `test_prune_stale_leaves_a_recent_record_alone`,
  `test_prune_stale_leaves_a_record_with_no_last_seen_alone`), all
  mutation-verified individually: always-prune-regardless-of-cutoff
  fails exactly the "leaves a recent record alone" test; treating a
  missing `last_seen` as immediately stale fails exactly the "leaves a
  record with no last_seen alone" test; removing the `async_load()`
  backfill fails exactly the "backfills" test - each mutation caught
  precisely one test, confirming they're not accidentally redundant
  with each other. Full suite: 194/194.

- **`apply_lighting` stopped reading a sensor entity internally -
  reverted to raw `brightness`/`color_temp_kelvin`/`rgb_color` fields,
  matching `compute_lighting_groups` exactly - 2026-08-22, prompted by
  the user reading through the services docs and questioning the
  design directly: "giving apply_lighting a sensor rather than just raw
  brightness, temp and rgb... feels like it makes it harder to use for
  different tasks for no real benefit."**

  Checked before agreeing, not taken at face value: `apply_lighting`
  was a genuine outlier. Its sibling pure planner
  `compute_lighting_groups` already took `sensor_brightness`/
  `sensor_color_temp_kelvin`/`rgb_color` as raw values with no entity
  reference at all - every other field between the two schemas was
  already identical. This is also a considered *reversal*, not a fresh
  direction, and said so rather than glossed over: the blueprint used
  to pre-compute these values itself, and a prior session deliberately
  moved that into `apply_lighting` reading `sensor_entity_id`
  internally, for two stated reasons - a hard-fail-on-missing-attribute
  safety fix, and "genericity across multiple sensor instances."
  Neither survived re-examination: the hard fail is preserved
  automatically by `vol.Required`/`vol.Coerce(int)` failing at the
  schema layer, arguably more robust than the custom
  `ServiceValidationError` it replaced; and entity *selection* (which
  named schedule sensor to point at) happens identically via
  `adaptive_sensor` regardless of which layer reads its attributes
  afterward, so "genericity" was never actually gated on where the read
  happened. Only one blueprint call site existed (confirmed via
  full-file grep) - the old scoped-resync second call was removed in an
  earlier session (see the `recovered` trigger's own dated entries
  above), so this stayed a one-block blueprint change.

  **Field naming confirmed via `AskUserQuestion`**: drop the `sensor_`
  prefix on *both* services, not just `apply_lighting` -
  `compute_lighting_groups` also lost its `sensor_` prefix
  (`sensor_brightness`/`sensor_color_temp_kelvin` →
  `brightness`/`color_temp_kelvin`), a second breaking change taken on
  deliberately rather than leaving `apply_lighting` with clean names
  while its sibling kept a prefix that no longer meant anything for
  either service. `grouping.py`'s own internal `build_groups()`
  parameter names (`sensor_brightness`/`sensor_color_temp_kelvin`) were
  deliberately left untouched - insulated from the service-level rename
  the same way they already were from `compute_lighting_groups`'s prior
  rename; `tests/test_grouping.py`, which calls `build_groups()`
  directly, needed zero changes, confirmed by actually running it, not
  just asserted from the reasoning.

  **A real, previously-latent schema bug found while working through
  the exact Jinja the blueprint would need to send `rgb_color`, before
  any code was written.** The blueprint has to pass
  `rgb_color: "{{ state_attr(adaptive_sensor, 'rgb_color') }}"`
  unconditionally, the same way `entities: "{{ adaptive_target_entities
  }}"` already does - and a hand-rolled "bring your own sensor" entity
  is documented as free to omit that attribute entirely (see the new
  `docs/BLUEPRINT.md#bring-your-own-sensor` section below). When it's
  missing, that template renders a literal `None`, not an omitted key.
  The old schema on both services
  (`vol.Optional("rgb_color"): vol.All([vol.Coerce(int)],
  vol.Length(min=3, max=3))`) has no path for an explicit `None` -
  `vol.Length` applied to `None` fails outright ("not a list") - so
  *every* `apply_lighting` call for *every* room without a custom
  RGB-providing sensor would have started failing the instant this
  shipped, had it not been caught here. Fixed on both schemas with
  `vol.Any(None, vol.All([vol.Coerce(int)], vol.Length(min=3,
  max=3)))`. Mutation-verified in `tests/integration/test_services.py`:
  `test_apply_lighting_accepts_an_explicit_null_rgb_color` and
  `test_compute_lighting_groups_accepts_an_explicit_null_rgb_color`
  both fail (a real `ValueError` from voluptuous) when the fix is
  reverted back to the bare `vol.All(...)`, and only those two.

  **Shape of the change**: `APPLY_LIGHTING_SCHEMA` dropped
  `sensor_entity_id` entirely; gained `brightness`/`color_temp_kelvin`
  (both `vol.Required`, matching `compute_lighting_groups`'s existing
  requirement - `color_temp_kelvin` stays required even though
  `rgb_color` is optional, since it's still the fallback target for any
  entity in a mixed-capability room that doesn't support RGB, even
  under `prefer_rgb_color: true`) and `rgb_color` (optional, the
  None-tolerant validator above). `_read_sensor_targets()` - the
  function that used to call `hass.states.get(sensor_entity_id)` and
  raise `ServiceValidationError` on a missing/invalid attribute - was
  deleted outright, along with the now-unused
  `homeassistant.exceptions.ServiceValidationError` import. The
  `apply_lighting` handler now reads `brightness`/`color_temp_kelvin`/
  `rgb_color` straight off `call.data`, identically to how
  `compute_lighting_groups`'s handler already did; everything
  downstream (`build_groups(...)`, the dispatch loop,
  `write_tracker.async_record`) needed no changes at all, since it only
  ever consumed the plain locals, never `sensor_entity_id` itself.

  **Blueprint**: the single `apply_lighting` action's `data:` block
  gained `brightness: "{{ state_attr(adaptive_sensor, 'brightness')
  }}"`, `color_temp_kelvin: "{{ state_attr(adaptive_sensor,
  'color_temp') }}"` (the sensor's own attribute is named `color_temp`,
  not `color_temp_kelvin` - `sensor.py`'s `_AdaptiveLightingSensor`
  exposes `"color_temp": data.get("kelvin")`; the blueprint reads that
  attribute name but forwards it under the service's `color_temp_kelvin`
  field), and `rgb_color: "{{ state_attr(adaptive_sensor, 'rgb_color')
  }}"`, replacing the old `sensor_entity_id: "{{ adaptive_sensor }}"`.
  The stale comment nearby (previously explaining why extraction was
  moved *out* of the blueprint) was rewritten to narrate the full
  two-designs history honestly - extraction moved into the blueprint,
  then into `apply_lighting`, then back into the blueprint again "to
  stay" - rather than left describing a decision this change reverses,
  matching this project's practice of keeping decision history visible
  instead of erasing it (see the comment starting "Brightness/colour
  temperature are extracted here" in the blueprint file itself).

  **Docs**: the "Bring your own sensor" section moved from
  `docs/HELPERS.md` to `docs/BLUEPRINT.md` outright, not just
  cross-referenced - neither service reads any sensor entity anymore,
  so the concept belongs entirely with `adaptive_sensor`, the blueprint
  input that's the only thing still doing any sensor-reading. This also
  resolved a standing internal inconsistency in `docs/HELPERS.md` (its
  intro paragraph claimed `apply_lighting`/`compute_lighting_groups`
  "read brightness/colour targets off any sensor entity," directly
  contradicting a later section that correctly described
  `compute_lighting_groups` as taking `rgb_color` as an explicit field
  "since it isn't reading a sensor at all" - both statements are true
  now, instead of contradicting each other).

  **Test impact**: `tests/integration/test_services.py`'s ~14 affected
  test functions were mechanically updated - `sensor_entity_id` +
  `_set_sensor(...)` setup replaced with literal `brightness`/
  `color_temp_kelvin` values directly in each call's own data dict;
  `sensor_brightness`/`sensor_color_temp_kelvin` dict keys on
  `compute_lighting_groups` calls renamed to `brightness`/
  `color_temp_kelvin`. The one direct `build_groups(sensor_brightness=
  200, sensor_color_temp_kelvin=3200, ...)` call (calling `grouping.py`
  directly, not through the service) was deliberately left untouched,
  per the reasoning above. The now-fully-unused `_set_sensor` test
  helper was deleted. `test_apply_lighting_missing_sensor_attribute_raises`/
  `test_apply_lighting_unknown_sensor_raises` (testing
  `_read_sensor_targets`'s now-deleted custom validation) were replaced
  with `test_apply_lighting_missing_brightness_raises`/
  `test_apply_lighting_non_numeric_color_temp_kelvin_raises`, both
  confirming voluptuous's own `vol.Invalid` is what's actually raised
  on a bad service call now - verified empirically by running them in
  isolation, not assumed.

  `tests/integration/test_blueprint.py`'s existing
  `test_periodic_adaptive_tick_updates_an_already_on_light` gained
  direct assertions on `apply_lighting_calls[-1].data["brightness"]`/
  `["color_temp_kelvin"]`/`["rgb_color"]` - the only test proving the
  blueprint's new Jinja extraction actually reaches `apply_lighting`
  with correct values (`rgb_color is None`, since the autouse `_sensor`
  fixture never sets that attribute), not just that the call happens at
  all. **Known limitation of this specific assertion, found while
  attempting to mutation-verify it**: `apply_lighting_calls` is wired
  through `async_mock_service`, which intercepts the service call
  *before* voluptuous schema validation ever runs - reverting the
  `vol.Any(None, ...)` schema fix does **not** make this blueprint test
  fail, since the mock never exercises the real schema at all. This
  test's real job is proving the blueprint's own template renders the
  right literal values (including a genuine `None`, not an omitted key
  or a stringified `"None"`); the schema-level regression coverage for
  the `None`-acceptance fix itself lives entirely in
  `test_services.py`'s two new tests, which do mutation-verify
  correctly against the real, unmocked service. Full suite: 196/196.

  **Deployed and confirmed live.** A stale module-level docstring
  (still saying `apply_lighting` "read its brightness/colour target off
  any sensor entity you point it at") survived the PR's own review and
  was only caught by `ha_read_file`'s post-HACS-download content check
  - fixed in a direct follow-up commit
  (`7cdbfb3`, docstring only, no behaviour change) and redeployed before
  restarting. One real, expected transient during the deploy window
  itself: the blueprint half is a separate deploy step from the
  integration half, so the first `adaptive_tick` after HA came back up
  (still running the *old*, not-yet-reimported blueprint, against the
  *already-restarted* integration with the new schema) errored with
  `extra keys not allowed @ data['sensor_entity_id']` - self-resolved
  the moment `ha_import_blueprint` ran moments later, and every trace
  since has been clean. Confirmed via `ha_get_automation_traces` against
  `automation.dining_room_lighting`: the `apply_lighting` call in a real
  post-deploy trace carries `brightness: 80`, `color_temp_kelvin: 2700`,
  `rgb_color: [255, 167, 87]` (Night phase, `prefer_rgb_color: true`),
  matching the sensor's real state at that moment - no `sensor_entity_id`
  anywhere. Also called `apply_lighting` directly against
  `light.extension_left_1` with `brightness: 180, color_temp_kelvin:
  3200, force: true` and confirmed via `ha_get_state` it landed exactly
  there (`color_temp_kelvin: 3205`, device-rounded). `repair_count: 0`
  and `config_check: valid` throughout.

## Testing

`pip install pytest pytest-homeassistant-custom-component && pytest`
from the repo root (see CONTRIBUTING.md). Two layers:

- `tests/test_curve.py`/`test_grouping.py`/`test_scenes.py` - no Home
  Assistant dependency at all. `tests/conftest.py` puts
  `custom_components/adaptive_lighting_helpers/` on `sys.path` and
  tests import `curve`/`grouping` as bare modules (not through the
  package, which would pull in `homeassistant` via `__init__.py`);
  `tests/fakes.py` provides a fake `EntityLookup` so `grouping.py` is
  exercised with plain dicts.
- `tests/integration/` (`test_services.py`, `test_blueprint.py`) -
  added 2026-08-15, real Home Assistant via
  [pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component).
  `test_services.py` exercises the actual registered services
  end-to-end (calling `async_setup_entry` directly, not
  `hass.config_entries.async_setup()` - the latter also resolves the
  manifest's `http`/`frontend` dependencies and pulls in the separate,
  large `home-assistant-frontend` package for something these tests
  never touch); `test_blueprint.py` loads the real blueprint file into
  a test automation and fires real triggers, mocking only
  `adaptive_lighting_helpers.apply_lighting`/`scene.turn_on`/`light.turn_off`
  via `async_mock_service` so these tests are purely about what the
  blueprint decides to call with, for a given trigger and room state.
  This is the only layer that can catch a bug living in the blueprint's
  own trigger/condition/action wiring - both the `recovered` trigger's
  dead-on-arrival bug and the off-light-turn-on bug it then exposed
  (see their own dated entries above) were exactly that kind of bug:
  syntactically fine, wrong only at runtime, invisible to plain YAML
  parsing or an isolated `ha_eval_template` check. `tests/integration/conftest.py`
  overrides pytest-homeassistant-custom-component's own `hass_config_dir`
  fixture (which otherwise points at the package's own bundled
  `testing_config/`, not this repo) to symlink this repo's
  `custom_components/` and `blueprints/` into a throwaway `tmp_path`,
  confirmed live as the fix for an initial "Integration not found"
  failure - not guessed.

  **`test_blueprint.py` reorganised and substantially expanded the same
  day, from an initial 7 tests to 26, one test class per feature -
  user's own explicit push-back, not a self-driven expansion**: "7
  tests doesn't feel like a lot for the blueprint... I'd expect the
  tests to essentially act as documentation for the blueprint
  behaviour." Restructured to mirror `docs/BLUEPRINT.md`'s own section
  headings exactly (`TestAdaptiveScheduleAndTransitions`,
  `TestRoomTargetResolution`, `TestOccupancyDrivenOnOff`,
  `TestAllowTurnOn`, `TestOverrideDetection`, `TestRecoveredTrigger`,
  `TestSceneHandoff`, `TestBrightnessScaling`, `TestRgbColour`,
  `TestAdditionalTriggers`, `TestSelfHealing`) so the file reads
  top-to-bottom as a spec of what the blueprint actually does, not just
  a regression net for the two incidents that prompted it. Real bugs
  caught building this out, not just added coverage for its own sake:
  - **A scene can't be activated by a plain periodic `adaptive` sensor
    tick once it's already considered active** - deliberate, existing
    behaviour (the blueprint's own `condition:` comment: "extra stays
    exempt from that suppression" - only `extra`/`motion_on`/a manual
    run can activate a scene; `adaptive` is suppressed entirely once
    `scene_active`, to avoid re-activating the same scene every minute).
    The first version of the scene-handoff tests triggered via the
    `adaptive` sensor changing, which is *never* the trigger that
    actually activates a scene in production either - silently
    asserting nothing, not failing, since `assert calls and ...` on an
    empty list just reads as "no calls happened" rather than crashing
    loud. Fixed by triggering those tests via a manual run instead,
    which exercises the `action:` scene-handling logic directly without
    tripping this specific suppression.
  - **A same-force, same-entity-count heuristic can't tell a manual
    run's own main call apart from the `recovered` trigger's scoped
    resync call** - an earlier `_main_calls()` helper filtered
    `apply_lighting` calls by "not forced, or more than one entity", to
    exclude `recovered`'s always-forced, always-single-entity resync
    call from assertions about the main room-wide call. Wrong for a
    single-light room's *manual run*, which also produces a
    single-entity, `force: true` main call - filtered out by the same
    heuristic, silently breaking that test. Removed entirely: no test
    in this file that isn't specifically about the `recovered` trigger
    ever produces more than one `apply_lighting` call per tick, so a
    plain `apply_lighting_calls[-1]` needs no filtering at all.
  - `async_fire_time_changed(hass, dt_util.utcnow().replace(minute=5, ...))`
    for testing the `reconcile` `time_pattern` trigger can silently
    target a time *earlier* than "now" depending on real wall-clock
    alignment at test-run time, rather than reliably crossing a future
    trigger boundary. Fixed by advancing relative to `utcnow()` instead
    (`utcnow() + timedelta(minutes=6)`, guaranteed past at least one
    5-minute boundary regardless of current alignment) - confirmed
    stable across 5 repeated local runs before landing, not just
    "passed once."

  This is also why `pyproject.toml`'s `requires-python` floor moved to
  3.14 (from 3.9) the same day: pytest-homeassistant-custom-component
  pins a specific Home Assistant release, which itself pins the Python
  it needs - since this repo only ever runs inside a real HA install,
  tracking that floor is the right target, not a separate, broader
  compatibility matrix kept for its own sake. **User's own call, not a
  default**: "just move everything to 3.14 - I'm not sure why you're
  targeting multiple python versions?" - the previous 3.9/3.13 CI
  matrix (3.9 as pyproject.toml's floor, 3.13 approximating a current
  HA runtime) is gone, replaced by a single Python 3.14 job.
  `pip install -e ".[dev]"` doesn't work here and isn't used anywhere
  (CI, CONTRIBUTING.md) - confirmed live, not assumed: this repo's flat
  layout (`custom_components/`, `blueprints/`, `dashboard/`, `brand/`
  all at the root) isn't set up for setuptools package discovery, and
  doesn't need to be, since nothing here is ever distributed as an
  installable Python package (HACS copies `custom_components/`
  directly; the blueprint is imported by URL) - `dev` in
  `pyproject.toml`'s `[project.optional-dependencies]` is a versions
  reference only, both dependencies installed by name directly instead.

  **Known, pre-existing, not yet fixed**: running the blueprint's own
  regex-matching templates (`resolved_entities`, `scope_entities`,
  `room_occupancy_entities`, the `recovered` trigger - anywhere using
  `select('match', '^light\.')` or `'^binary_sensor\.'`) through a real
  Jinja engine for the first time (via `tests/integration/test_blueprint.py`)
  surfaced a `DeprecationWarning`: `"\." is an invalid escape sequence.
  Such sequences will not work in the future.` - not introduced by this
  test suite, already present in the blueprint's own regex literals,
  just never previously run through anything that would surface it at
  this log level. Currently cosmetic (the templates still work
  correctly - all `tests/integration/test_blueprint.py` cases pass),
  but the warning's own text says future Python/Jinja versions may turn
  this into a hard error, which would break entity resolution
  everywhere this pattern is used. Flagged to the user, not yet
  addressed - fix is presumably `'^light\\.'` (properly-escaped
  backslash) or an equivalent raw-string-safe form, needs verifying
  against a real Jinja render before landing, not assumed.
