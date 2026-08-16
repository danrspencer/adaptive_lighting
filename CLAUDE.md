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
