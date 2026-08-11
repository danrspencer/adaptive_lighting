# CLAUDE.md

Context for resuming work on this repo in a fresh session. See README.md
for the user-facing description; this file is about *how* to work on it
and *why* it's shaped the way it is.

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
chasing three separate bugs (see lessons 7-9), each looking exactly
like the others' symptom (nothing happens, no error). That experience
is why the computation now lives in `custom_components/adaptive_lighting_helpers/`
instead - a real Home Assistant integration that registers its own
services directly, with no pyscript dependency and none of that
machinery to go wrong. Lessons 7-9 are kept below as the reasoning for
that decision and in case pyscript ever comes up again elsewhere, not
because they're live concerns in this repo anymore.

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
below (see "A same-named blueprint can take out every room at once") -
don't reintroduce that collision.

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
  occupancy detection (`occupied`), and the action *structure* (which
  service to call, on what target).
- Scene compatibility checking (`scene_active`/`scene_valid`) is a
  partial exception - the *logic* now also exists as a standalone
  service (`compute_scene_coverage`, see below), but the blueprint's
  own inline Jinja version was deliberately left in place rather than
  rewired to call it. Two reasons: it's used by a `condition:` block
  (see lesson 4 - conditions can't call services at all), and this
  repo's two existing services hadn't even been confirmed working
  against a live instance yet when this one was added - not the moment
  to add a second live dependency to the blueprint on top of an
  unconfirmed first one. Revisit once `compute_lighting_groups` is
  actually confirmed working live.
- The blueprint's `manual` trigger (context.user_id on the *triggering*
  state change) only ever blocks the one automation run where it fires
  - it has no memory, so it does NOT stop a later `adaptive` tick from
  overwriting the same light a minute afterwards. See "Manual overrides
  don't persist by default" below - the actual sustained protection
  lives in `grouping.py`, not here.
- Why: HA `condition:` blocks cannot call a service — only `action:`
  steps can — so anything condition:-gating needs to stay template-
  based. These pieces are also relatively compact (a few lines each)
  and get real value from HA's native trace/debug UI
  (`ha_get_automation_traces`), which was used heavily to diagnose the
  original bugs this rewrite fixes. Losing that observability wasn't
  worth it for logic that isn't actually that bad.

**Moved to `custom_components/adaptive_lighting_helpers/` (a standalone
HACS integration, `adaptive_lighting_helpers.compute_lighting_groups`
service, backed by `grouping.py`):**
- Reachability filtering, multiplier bucketing, the tolerance-based
  "already at target" check, manual-override protection, and
  two-step-vs-combined label routing.
- Why: this was the genuinely gnarly part — nested namespace loops,
  nothing pytest-testable, and a real correctness gap (exact-match
  brightness/colour-temp comparisons that silently stopped skipping
  for any bulb with device-side rounding quirks).

**Also moved there:** the day-phase brightness/Kelvin curve math
(`curve.py`, exposed as `adaptive_lighting_helpers.compute_curve`),
ported from `custom_templates/adaptive_lighting.jinja` in the live HA
config - not because it was complicated, but because it's exactly the
kind of small, reusable, independently-useful piece of logic that
belongs as a documented service in its own right, not duplicated
Jinja someone has to copy into `custom_templates/` and a
`packages/*.yaml`.

**Also moved there, as of the most recent session:** scene-coverage
gap filling (`scenes.py`, exposed as `compute_scene_coverage`) - "does
this scene exist, is it within scope, and which of my target entities
does it leave uncovered" - ported from what used to be the blueprint's
own `desired_scene`/`scene_covered_entities`/`scene_valid`/
`scene_active`/`adaptive_target_entities` variables. Explicitly generic:
nothing about it is specific to adaptive lighting, or lighting at all -
"apply a scene, then a default for whatever it doesn't cover" is a
reusable pattern on its own. **The blueprint was NOT rewired to call
it** - see the note under "Stays in the blueprint" above for why.

All three services are deliberately written and documented (see
`services.yaml`) as standalone tools - useful to anyone building their
own automation, not just to the blueprint in this repo. The blueprint
is one consumer of them (and only of two of the three, currently), not
their reason for existing.

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
transition before its own bail-out stops it) and erodes the trace-
visibility benefit lesson upfront in "The architectural split" already
cites as *why* this stuff stays in `condition:`. (2) A Jinja-macro
alternative (viable in `trigger:`/`condition:`/`variables:` where a
service categorically isn't, via the `to_json`/`from_json` round-trip
lesson 1 describes) would still mean adding a new required manual
install step (`custom_templates/*.jinja`, restart needed per lesson 2)
to a blueprint that's currently fully self-contained. (3) A third
option was also raised and rejected: registering a genuine Python-
backed global Jinja function (callable bare, like `is_state()`, no
`{% import %}` and no `to_json` round-trip needed, since it'd return a
real list directly). Confirmed technically possible - a real published
integration (PiotrMachowski's Custom Templates) does exactly this -
but its own README warns it "tampers with internal code of Home
Assistant which *might* cause some unforeseen issues (especially after
HA updates)"; there's no documented/supported HA extension point for
this, unlike `hass.services.async_register` or the `custom_templates/`
convention. Monkey-patching the one shared template engine every
automation/sensor/script on the instance depends on was judged not
worth it for this. Decided not worth any of the three costs - the
blueprint's inline copies stay as they are. Don't re-propose without
new information changing this trade-off.

`curve.py`'s math is also available as sensors (`sensor.py`), not just
the `compute_curve` service - optional, only set up if the integration
is configured with schedule `input_datetime` entities. This is a
genuine replacement for a Jinja `packages/*.yaml` day-phase setup, not
just a service wrapper - see "Current status" below for what it covers
and what it deliberately leaves out.

## Hard-won lessons (don't repeat these)

Lessons 7-9 are about pyscript specifically, from when that was this
repo's backend. They're kept because they're the actual reasoning
behind abandoning pyscript for a native integration instead (see
"Where this came from" above) and in case pyscript comes up again in
some other context - not because pyscript is still part of this repo.

1. **Jinja macros can only return rendered text, never a native Python
   list.** `{% macro x() %}{{ some_list }}{% endmacro %}` returns a
   *string* that looks like a list. Chaining macro calls (`resolve_targets()`
   calling another macro internally) silently breaks list operations
   downstream. If a macro needs to hand back a real list to be used
   *within the same template* (not just as the template's final
   output), round-trip through `to_json`/`from_json`. This is why an
   earlier attempt at a shared `custom_templates/*.jinja` macro for
   target resolution was abandoned in favour of duplicating that logic
   inline in the blueprint — not worth re-attempting without a strong
   reason.

2. **`custom_templates/*.jinja` files are scanned once at HA startup,
   not on a config/automation reload.** Adding a *new* file there and
   then `ha_reload_core` will NOT pick it up — it needs a full HA
   restart. This broke every automation sharing the blueprint in
   production for about a minute before being caught and reverted.
   Don't add new custom_templates files without warning the user a
   restart is required, or better: avoid depending on custom_templates
   at all now that the logic lives in pyscript instead.

3. **Symlinks for deployment must be created on the HA host itself,
   not through a Samba/SMB mount from another machine.** A symlink's
   target is just a string; if it's written from macOS via a mounted
   share, the target needs to be a path meaningful to *Home Assistant's
   own filesystem* (e.g. `/config/...`), not the mounting machine's
   path. Simplest correct approach: clone this repo directly on the HA
   host (e.g. under `/config/`) and symlink from there. See README's
   Deploying section.

4. **HA `condition:` blocks cannot call services.** This is *the*
   constraint that shaped the architectural split above — any value a
   `condition:` needs must be computed via template, not via a pyscript
   call. If future work wants to move `occupied`/`scene_active` into
   pyscript too, this needs solving first (e.g. always let `action:`
   run and have the pyscript service itself decide there's nothing to
   do, rather than gating in `condition:`).

5. **A trigger firing once does not mean protection persists.** The
   blueprint's original `manual` trigger (both the old 60-second-window
   version and the `context.user_id` version that replaced it) only
   ever blocked the specific automation run where *it* fired. Neither
   stopped the very next `adaptive` tick (~60s later, independent
   trigger) from recomputing and overwriting the same light - so a
   manually-set brightness/colour would get reverted within about a
   minute, silently, the whole time this looked "fixed". Caught by the
   user asking "won't the next tick just overwrite it?" - worth
   remembering as a class of bug: a one-shot trigger-level check is not
   the same as a standing invariant later code respects.

   Fixed properly in `grouping.py`'s `EntityLookup.manually_set()`:
   instead of remembering that an override happened, it re-checks the
   entity's *current* state's context on every call. This has a nice
   property - the three release conditions the user wanted (room goes
   empty -> off; light isn't on at all; device recovers from an outage)
   all fall out for free with no persisted state:
     - room empty -> `motion_off`/`reconcile` bypass grouping entirely
       (they target `reachable_entities` directly), so they're
       unaffected by any override.
     - light off -> `manually_set()` requires `is_state(e, "on")`, so
       there's nothing to protect.
     - device recovery -> a device regaining power sets its own state
       with no `context.user_id`, so it was never "protected" as soon
       as its state changes at all.
   **This fix lives only in `grouping.py` so far - the live deployed
   blueprint (still pure Jinja, not yet calling pyscript) has NOT been
   patched and still has this gap today.** Whether to backport a
   matching Jinja-only fix to the live blueprint ahead of the full
   pyscript rewiring was raised but not decided as of this writing -
   check before assuming either way.

6. **A same-named blueprint can take out every room at once.** This
   repo's blueprint used to be named `adaptive_lighting_unified.yaml`
   - identical to the file already live on the real HA instance, which
   every one of the 15 room automations references by that exact path.
   Running `scripts/link_into_ha.sh` symlinked the repo's (materially
   different - new `scene_template`/`extra_triggers` inputs) blueprint
   directly over the live one. Every room automation broke at once.

   Recovering it was messier than expected, too: the script's own
   backup (`adaptive_lighting_unified.yaml.bak-<timestamp>`, left next
   to the original) was the right fix, but restoring it through the
   Samba mount failed in a confusing way - `ls` showed only the backup
   file, but `mv`/`cp` targeting the original filename both failed
   ("No such file", then "Permission denied" for the identical
   operation), consistent with a symlink sitting there that Samba
   wasn't surfacing in listings but was still enforcing underneath.
   Attempting the fix through the network share was abandoned in favour
   of doing it directly on the HA host, which is where it actually got
   fixed. Lesson within the lesson: when a Samba-mounted view of
   `/config` disagrees with itself (a file `ls` won't show but which
   still blocks writes), stop trying to fix it through that mount -
   don't trust it enough to keep operating blind, go to the host.

   Fixed at the root by giving this repo's blueprint a different file
   name *and* a different in-blueprint `name:` (`adaptive_lighting.yaml`
   / "Adaptive Lighting", vs. the live `adaptive_lighting_unified.yaml`
   / "Adaptive Lighting (Unified)") - see "Where this came from" above.
   The two can now be installed side by side with zero interaction;
   rooms move over to the new one individually, deliberately, not by
   the symlink script silently replacing what 15 rooms already depend
   on.

7. **A symlink's target is only meaningful from the shell session that
   created it - and the failure this caused looked nothing like a path
   bug at first.** `danspencer/adaptive_lighting.yaml` was symlinked
   (right name, no collision with lesson 6) and showed up in
   `ha_get_blueprint`'s listing, but creating an automation from it
   failed every time with `"Unable to find
   danspencer/adaptive_lighting.yaml"`, and querying that path returned
   `"warnings":["Blueprint body could not be read or parsed ...
   returning metadata only"]` - the directory entry existed but nothing
   could read through it. The initial hypothesis was an AppArmor/
   container-boundary restriction on following symlinks at all - wrong,
   but plausible enough to run with for a while. Switching the
   blueprint to a plain `cp` fixed it, which looked like it confirmed
   the theory.

   pyscript then hit what looked like the exact same wall - symlinked
   deliberately, as a test of whether the restriction was blueprint-
   specific. It wasn't: `pyscript.compute_lighting_groups` never
   appeared after symlinking `pyscript/modules/adaptive_lighting` and
   `pyscript/apps/adaptive_lighting` in, not even an error anywhere in
   the logs. Copying pyscript's files too "fixed" it (actually just
   moved the failure further down the stack - see lesson 9), which
   looked like further confirmation of a blanket "symlinks don't
   resolve here" restriction.

   The AppArmor theory was wrong. Re-copying the dashboard card's
   symlink later surfaced a `RELINK ... (was ->
   /root/config/repos/adaptive_lighting/www/...)` line - the *old*
   symlink's target was `/root/config/...`, not `/config/...`.
   Whichever shell session first ran the deploy script had `/root/config`
   as its own path to the same files (plausibly a convenience alias in
   that SSH session), and `dirname "$0"` + `pwd` baked that session-
   specific path into the symlink as a literal string. A symlink's
   target is just text; a path that resolves fine in the writing
   shell can be completely meaningless to whatever process reads the
   symlink back later, even on "the same host". No AppArmor or
   container boundary needed to explain any of this - it's a plain
   path bug that happened to be very difficult to see from the
   symptoms alone (a missing file *reads* a lot like a permissions/
   sandboxing problem).

   `scripts/link_into_ha.sh` copies the blueprint and pyscript instead
   of symlinking them (see the `copy()` function, which handles both
   files and directories via `cp -r`) not because symlinks are
   fundamentally broken here, but because copying sidesteps the whole
   path-aliasing problem rather than requiring every future shell
   session to get the absolute path right. The dashboard card is still
   symlinked and untested; if it turns out to have a stale/wrong
   target too, the same fix applies.

8. **pyscript only autoloads a folder-based app from a file named
   exactly `__init__.py`.** `pyscript/apps/<name>/app.py` (or any other
   filename) is silently skipped - no error, no log line, at any log
   level, even with debug logging on. Cost a lot of confused debugging
   before finding it, because it's indistinguishable from the file just
   not being deployed at all. If a pyscript app "isn't showing up" with
   zero evidence why, check the filename before anything else.

9. **A pyscript app can't share its name with a module package it
   imports from - it recurses forever instead of raising a normal
   import error.** `pyscript/apps/adaptive_lighting/__init__.py` doing
   `from adaptive_lighting import ...` to reach
   `pyscript/modules/adaptive_lighting/` - identical names - sent
   pyscript's import resolution in circles (`module_import -> load_file
   -> module_import -> ...`) until Python's recursion limit hit and
   raised `RecursionError: maximum recursion depth exceeded`. Renamed
   the app folder to `adaptive_lighting_app` to break the collision;
   the module package keeps the plain name since tests import it
   directly (`from adaptive_lighting import build_groups`) and aren't
   affected by what the pyscript app folder is called.

   Separately, and easy to conflate with lesson 8's symptom because
   both present as "nothing happens, no error": **a pyscript app also
   needs an explicit entry (even empty) under `pyscript: apps:` in
   YAML config.** A folder existing under `pyscript/apps/` is not
   enough by itself - with debug logging on, pyscript logs `load_scripts:
   skipping .../__init__.py (app_name=...) because config not present`
   and does nothing otherwise, at debug level only (invisible at the
   default WARNING level, which is exactly why this and lesson 8 both
   went unnoticed for so long). `packages/adaptive_lighting_pyscript.yaml`
   ships this config - genuinely required by anyone deploying this
   repo's pyscript half, not instance-specific, so (unlike the git-sync
   automation) it belongs in the repo and is linked in by
   `scripts/link_into_ha.sh` like everything else.

   Diagnostic notes for next time this class of thing happens: `pyscript.reload`
   does NOT re-scan for brand-new apps/files, only reloads ones already
   known - use `homeassistant.reload_config_entry` (with the pyscript
   config entry's `entry_id`) to force a full re-scan without a full HA
   restart. It's flaky though - has returned a "dispatched but timed
   out" partial response and then not actually completed the reload
   more than once this session; a full restart has been 100% reliable
   every time by contrast, at the cost of briefly dropping every
   automation/device in the house. `logger.set_level` on
   `custom_components.pyscript` to `debug` is what actually surfaces
   the load_scripts skip messages and the real exception behind a
   `module_import: failed to load module ...` line (the default
   WARNING level shows neither) - but gets silently reset back to
   WARNING by `homeassistant.reload_core_config`, so re-set it after
   calling that, not before.

10. **A stray `.bak-<timestamp>` directory under `custom_components/`
    isn't inert - it can break the domain it's a backup of.** Home
    Assistant discovers custom integrations by scanning every directory
    under `custom_components/` for a `manifest.json` and reading its
    `domain` key, not by the directory's own name. A leftover
    `custom_components/adaptive_lighting_helpers.bak-20260803-231501/`
    (created by `scripts/link_into_ha.sh`'s own backup-before-overwrite
    step during an earlier manual deploy, then never cleaned up) still
    had a `manifest.json` declaring `domain: adaptive_lighting_helpers`
    - the same domain the real, HACS-installed directory declares. When
    Home Assistant tried to resolve the config flow handler for that
    domain, it built the Python import path from the literal folder
    name, dots and all - `custom_components.adaptive_lighting_helpers.
    bak-20260803-231501` - which isn't importable, and `ha_set_integration`'s
    config-flow call failed with a bare `404 Invalid handler specified`
    that gave no hint the actual fault was a stray sibling directory.
    The `ha-mcp` tool's file access couldn't help find it either -
    `custom_components/` is readable only file-by-file (`*.py` only,
    read-only) and not listable or deletable through it at all - so the
    real culprit only surfaced by grepping `home-assistant.log` for the
    domain name and finding the `.bak-` path in the error. Deleting the
    directory on the host (via the Advanced SSH & Web Terminal add-on;
    see lesson 3 on why not through a Samba mount) did NOT take effect
    until a *second* full restart - Home Assistant's flow-handler
    registry is apparently built once at startup, so un-discovering a
    directory needs the same "restart to rescan" treatment that
    discovering a new one does. Lesson: clean up `link_into_ha.sh`'s
    `.bak-*` backups promptly, especially under `custom_components/` -
    and don't expect a fix made by deleting a file mid-runtime to take
    effect without a restart, any more than adding one would.

## Current status / what's not done

**The pyscript backend has been fully replaced by a native integration.**
`custom_components/adaptive_lighting_helpers/` now registers
`adaptive_lighting_helpers.compute_lighting_groups` and
`adaptive_lighting_helpers.compute_curve` as real HA services
(`hass.services.async_register`, `config_flow`-based, HACS-installable
as an "Integration" category repo, `hacs.json` at repo root). `pyscript/`
no longer exists in this repo at all. This resolves the "open question"
that used to be documented in this section - decided and implemented,
not just proposed. A third service, `compute_scene_coverage` (see
`scenes.py`), was added in the same push once the pattern was
established - the integration now covers all three genuinely reusable
pieces that used to be blueprint-only Jinja: grouping, curve math, and
scene-coverage gap filling.

- `curve.py`, `grouping.py`, and `scenes.py` - unchanged/new pure
  Python, no Home Assistant dependency, unit-tested (`pytest`, 26/26
  passing). `tests/conftest.py`'s comment explains how tests import
  them without triggering the integration's own `__init__.py`, which
  does need `homeassistant`.
- `custom_components/adaptive_lighting_helpers/__init__.py` - the thin
  HA adapter (equivalent to the old pyscript app), built from what was
  actually confirmed during the pyscript attempt: `is_state`/`state_attr`
  work as expected against `hass.states`, `device_id`/`labels` go
  through `entity_registry`/`device_registry` (`RegistryEntry.labels`),
  and `context.user_id` comes from `hass.states.get(entity_id).context.user_id`.
  These translations were originally carried over from the pyscript
  attempt (a different runtime) rather than tested directly - **now
  confirmed against live HA state as of 2026-08-10** (see below):
  `is_state`/`state_attr` behave as expected against `hass.states`, and
  a full blueprint run exercised `manually_set()`'s `context_user_id`
  path with no errors. `device_id`/`labels` were exercised too (no
  labelled `no_combined_transition` lights existed to hit the non-empty
  branch, but the lookup calls themselves completed cleanly against the
  real entity/device registries).
- The blueprint's action: block now calls
  `adaptive_lighting_helpers.compute_lighting_groups` instead of
  `pyscript.compute_lighting_groups` - a one-line change, same
  `response_variable`-based flow as before.
- **`sensor.py` - optional day-phase/curve sensors**, a native
  replacement for the live `packages/adaptive_lighting.yaml` Jinja
  setup (ported faithfully from reading that actual file - same
  boundary logic, same sunset clamp, same entity IDs). Only set up if
  the config entry has schedule `input_datetime` entities configured
  (see `config_flow.py` - all five fields are optional, so the two
  services still work with zero config). Deliberately left out the two
  household-specific parts of the live package that don't belong in a
  reusable integration: the nightlight/sleep-mode brightness override
  (referenced a specific `input_select` entity) and the IKEA-label
  diagnostic sensor. One coordinator (`DataUpdateCoordinator`, 60s
  interval, plus immediate refresh on the tracked entities'/`sun.sun`'s
  state changes) recomputes everything in one pass each time rather
  than staggering cadences the way the Jinja version did - `curve.py`'s
  functions are cheap enough that recomputing the 289-point curve every
  60 seconds is a non-issue. **Still untested live** - config entry was
  added with all five schedule fields blank this pass (services only,
  deliberately - see below), so no sensors have been set up or
  confirmed yet. Same caveat as before, just narrower in scope now.
- **Deployed via HACS and confirmed working against the live Home
  Assistant instance, as of 2026-08-10.** Installed through the actual
  HACS custom-repository flow this time - `danrspencer/adaptive_lighting`,
  category "Integration" - rather than `scripts/link_into_ha.sh`,
  confirming that path works end to end for the first time. This is the
  better long-term answer since it's what end users would actually do;
  `link_into_ha.sh` remains for local iteration before a change is
  pushed. Hit one new blocker along the way: a stray `.bak-*` directory
  left under `custom_components/` from an earlier manual deploy broke
  config-flow resolution until removed and HA restarted a second time -
  see lesson 10 above for the full story.

  Added via Settings → Devices & Services → Add Integration → "Adaptive
  Lighting Helpers" with all five schedule fields left blank -
  services only, no sensors, so as not to conflate two untested things
  in one pass. All three services confirmed registered
  (`ha_list_services(domain="adaptive_lighting_helpers")`) and each
  exercised for real, not just installed:
  - `compute_curve` called directly with hand-supplied boundaries -
    returned the correct phase/brightness/Kelvin.
  - `compute_lighting_groups` called directly against `light.kitchen_spots`
    (off at the time) - returned one correct `combined` group at full
    target brightness.
  - `automation.living_room_lights_new` manually triggered end-to-end
    (`automation.trigger`) - the full blueprint → `compute_lighting_groups`
    → `light.turn_on` path, traced with zero errors
    (`ha_get_automation_traces`), and `light.living_room_pendant_1`
    actually turned on at brightness 255 / 5952K against a computed
    target of 255 / 5929K - the small Kelvin delta being exactly the
    kind of device-side rounding the tolerance check in `grouping.py`
    exists to absorb, seen here for the first time against a real bulb
    rather than a fake in a test.

  This confirms the `__init__.py` adapter's HA-specific translations
  (documented above) hold up against real state, registries, and
  context - no longer carried-over guesses from the pyscript era.

  **The optional day-phase/curve sensors were redesigned after this
  pass, before ever being deployed live** (design discussion, not a
  live-tested change - still open per item 1 below):
  - The five schedule boundaries are no longer separate `input_datetime`
    helpers the user has to create first - they're plain `TimeSelector`
    fields stored directly on the config entry (`morning_time`/`day_time`/
    `evening_earliest_time`/`evening_latest_time`/`night_time`), editable
    later via a proper `async_step_reconfigure` (previously missing
    entirely - `supports_reconfigure` was `false` on the entry this
    session created, a real gap, not just an unexercised feature).
  - `sensor.day_phase` plus the separate `solar_adaptive_lighting_brightness`/
    `_color_temperature` sensors collapsed into one `sensor.adaptive_lighting`
    (state = phase, `attributes.brightness`/`color_temp`) - closes a
    shape mismatch that would otherwise have blocked ever fully
    retiring the live package (see item 2 below): the blueprint's
    `adaptive_sensor` input needs one entity with `brightness`/
    `color_temp` attributes, which separate value-only sensors never
    provided. Losing a standalone phase entity costs nothing
    automations care about - `platform: state` triggers can watch just
    the `phase` attribute on the combined sensor (`attribute: phase`),
    the same pattern the blueprint's own `adaptive_attr` trigger already
    uses for the whole attribute set.
  - Added `select.adaptive_lighting_phase` (Auto/Morning/Day/Evening/Night)
    as the write side of what used to be a single dual-purpose entity
    (`input_select.day_phase`, both computed-value display and manual
    override in one). Self-clears at the next phase boundary by
    default, matching the live Jinja system's actual behaviour (pin
    Evening to Day and it still becomes Night once Evening would
    naturally have ended, not stuck on Day forever) - a
    `sticky_phase_override` config field disables that and keeps an
    override until cleared by hand instead. Implemented by remembering
    `computed_phase` at override time and comparing against it on every
    coordinator refresh (`select.py`), not a timer - consistent with
    this repo's general "check live state fresh, don't invent a
    persisted expiry" style (`grouping.py`'s `manually_set()` does the
    same thing for manual light overrides).
  - The precomputed curve (`sensor.adaptive_lighting_curve`) deliberately
    does NOT follow the override - it's a full-day schedule/forecast,
    and pinning "right now" doesn't change what the schedule would have
    looked like at 9am. This was an unintentional inconsistency in the
    old Jinja version (see `phase_at()`'s docstring); here it's the
    same behaviour, now deliberate and documented rather than a wart.
  - `www/adaptive-lighting-curve-card.js`'s `DEFAULT_ENTITIES` and
    `dashboard/preview.html`'s synthetic state were both updated to
    match - `phase`/`brightness_now`/`kelvin_now` now default to the
    same `sensor.adaptive_lighting` entity (attribute-based, with a
    `.state` fallback for custom configs still pointing at separate
    sensors), and evening's earliest/latest bounds moved from two
    standalone `input_datetime` entities to `attributes.earliest`/
    `latest` on `sensor.evening_start` itself.

  **Still open:**
  1. None of the above has been configured or tested against the live
     instance yet - still just the services (confirmed live, see above).
     Configuring the sensors now means filling in the five `TimeSelector`
     fields via the integration's Configure screen, then checking the
     sensors land at the forced entity_ids (`sensor.morning_start` etc.
     - expect a `_2` suffix if the live `packages/adaptive_lighting.yaml`
     sensors of the same name are still active; see `sensor.py`'s
     docstring), and separately confirming the phase-override select
     and its self-clearing behaviour actually work against a real
     `sun.sun`-driven boundary crossing, not just by inspection.
  2. **Once the sensors are confirmed working, retire the live
     `packages/adaptive_lighting.yaml`** (or at least its generic
     boundary/phase/brightness/curve parts - the household-specific
     nightlight and IKEA-diagnostic sensors have no replacement here
     and would need to move somewhere else first, or just stay as a
     much smaller leftover package).
  3. Three stale `service_not_found` repairs are still showing under
     Settings → Repairs - two on `automation.living_room_lights_new`
     (one from the pyscript era, one from before this session's fix)
     and one on the already-removed git-sync automation (see below).
     Home Assistant doesn't auto-clear a repair just because a later
     run succeeds; these need dismissing by hand. Cosmetic only -
     nothing is actually broken.
- **The live instance's pyscript-era leftovers were cleaned up in an
  earlier session, before this repo's HACS install existed.**
  `/config/pyscript` (both the app and module directories, plus the
  dangling `.bak-*` symlinks from lesson 7's incident) and
  `packages/adaptive_lighting_pyscript.yaml` were deleted from the live
  host directly. The pyscript HACS integration itself was deliberately
  left installed (harmless with nothing left to load; cheap to remove
  later if wanted).
- **The dev/test git-sync automation has been removed.** It used to
  poll this repo for new commits and re-run `link_into_ha.sh`
  automatically (a `shell_command` + `time_pattern` automation,
  `automation.adaptive_lighting_sync_from_git`, plus
  `packages/adaptive_lighting_sync.yaml` and
  `scripts/adaptive_lighting_sync.sh` on the live host - never
  committed to this repo, see git history around the "Rewire blueprint
  to pyscript, add git auto-sync..." commit for why). Torn down at the
  same time as the pyscript cleanup above, since the whole point of
  moving to a real HACS integration is that HACS handles install/update
  natively - a git-polling shell script doing the same job by hand is
  exactly the thing this migration was meant to replace, not something
  to keep running alongside it. Don't re-add it without checking
  whether the HACS install actually covers everything it used to first.
- `dashboard/preview.html` + `generate_preview_data.py` let you see the
  actual Lovelace card rendered with synthetic data, without a running
  HA instance - regenerate data with
  `python3 dashboard/generate_preview_data.py`, then serve the repo
  root over HTTP (not `file://` - the card's `fetch()` needs it, and
  it must be the repo root, not `dashboard/`, since `preview.html`
  imports `../www/adaptive-lighting-curve-card.js`) and open
  `dashboard/preview.html`.

**RGB colour support added (`prefer_rgb_color`, design-only - not yet
live-tested).** Prompted by the user recalling that `basnijholt/
adaptive-lighting` (the most popular HA adaptive-lighting integration)
has a `prefer_rgb_color` option people like. Two real decisions came out
of planning this, both worth remembering if it's revisited:

- **A new service, `apply_lighting`, that actually calls
  `light.turn_on`/`light.turn_off` itself** - the first service in this
  integration with side effects; `compute_lighting_groups` and
  `compute_curve` are still pure planners. This was a mid-plan course
  correction: the first draft kept the blueprint dispatching
  `light.turn_on` itself (extending its existing `repeat:`/`parallel:`
  block with more branches for RGB), matching how `compute_lighting_groups`
  already worked. The user pushed back - a service that just does the
  work, hiding `light.turn_on`/two-step/RGB-vs-colour-temp dispatch
  entirely, is a better fit for why this repo exists at all (move logic
  out of blueprint Jinja into tested Python) - so the blueprint's entire
  dispatch block collapsed to one `apply_lighting` call instead of
  growing. `compute_lighting_groups` was kept alongside it, unchanged in
  spirit, for anyone who wants the plan without the side effect.
- **`apply_lighting` takes `sensor_entity_id`, not raw brightness/
  colour-temp values** - reads `brightness`/`color_temp`/`rgb_color`
  attributes directly off whatever entity you point it at, generically
  (see README's "Bring your own sensor" section, and
  `_read_sensor_targets` in `__init__.py`). Clarified with the user
  during planning: this is not about one call supporting multiple
  sensors - a call is always one sensor - it's that the reading logic
  must never assume it's reading *this integration's own*
  `sensor.adaptive_lighting`, because a future direction (not started)
  is letting the integration run multiple independent config-entry
  instances, each with its own schedule and its own sensor entity.
  Nothing about multi-instance config entries exists yet; the only
  thing this constrains is that `sensor_entity_id`'s handling stays
  generic.
- The RGB target itself is always *derived from the existing Kelvin
  curve* (`curve.kelvin_to_rgb`, a Python port of the dashboard card's
  `kelvinToRgb()` - same rounding behaviour, `math.floor(x+0.5)` not
  Python's banker's-rounding `round()`, verified to actually match by
  hand-computing two reference points before trusting it). No separate
  "RGB curve" - `kelvin_for_phase` gained one keyword-only parameter,
  `night_floor` (default 2700, preserving today's output exactly), so
  Evening's final hour and Night can optionally target something warmer
  than a bulb's native `color_temp` range would allow. Left at the
  default, RGB mode is purely a wire-format change with an identical
  visual result; only actually diverges once `night_floor_kelvin` (the
  integration's config field for this) is lowered.
- Dashboard card gained a thin coloured overlay ("cap") above the bars
  wherever `kelvin_rgb` diverges from `kelvin`, plus a matching ring on
  the "now" dot and a tooltip addition - verified visually via
  `dashboard/preview.html` and a browser screenshot before considering
  it done, same as the earlier clamp-band feature this session.
  `dashboard/render_preview_svg.py` had its own third hand-maintained
  copy of the Kelvin→RGB algorithm; consolidated onto `curve.kelvin_to_rgb`
  while in there rather than adding a fourth copy.

**Duplication pass, prompted by the user directly asking for one after
the RGB work above.** Found and fixed three real cases (all from the
RGB work itself, not pre-existing): `curve.targets_for_phase()` now
the single place that turns a phase into brightness/kelvin/kelvin_rgb/
rgb_color - previously hand-copied in `compute_curve`'s handler,
`coordinator.py`'s "now" computation, its 289-point curve loop, and the
preview generator (four places, could silently drift). `curve.
DEFAULT_NIGHT_FLOOR_KELVIN` replaces the literal `2700` repeated across
three files. `grouping.py`'s `_already_set`/`_already_set_rgb` shared
brightness-tolerance-check lines, extracted to `_brightness_close()`.
Also surfaced two *pre-existing*, already-documented duplications
elsewhere in this file (target-resolution Jinja duplicated 3x in the
blueprint, deliberately kept - see "Considered and explicitly rejected"
above; and scene-coverage logic duplicated between the blueprint and
`compute_scene_coverage` - see below, this one *was* pursued this
session, then parked).

**Parked: moving scene handling out of the blueprint into
`apply_lighting`.** Explored in depth, decided against pursuing further
*this session* - not implemented, blueprint's scene handling is
untouched. Recorded here so the next session doesn't have to re-derive
any of this.

The blueprint actually has two independent scene mechanisms, not one:

- **A) `scene_template` → coverage → partial handoff.** The
  `desired_scene`/`scene_covered_entities`/`scene_valid`/`scene_active`
  Jinja block, which is a byte-for-byte semantic match for
  `compute_scene_coverage` (verified by comparing the two line by line -
  same existence check, same "covered entities must all be within
  scope" validation, same fallback-to-uncovered behaviour). This is the
  one flagged as "revisit once confirmed live" back when
  `compute_scene_coverage` was first added - that condition has now
  actually been met.
- **B) `sensor_service == 'scene.apply'`.** A separate, older mechanism
  read off the *adaptive sensor's* attributes (not a blueprint input) -
  if set, called `scene.apply` with `data: {scene_id: sensor_scene_id,
  ...}`. **This was dead code**: `scene.apply`'s actual service schema
  only accepts `entities` (a state-map) and `transition` - there's no
  `scene_id` field, so this call had probably never done anything.
  Confirmed by reading the blueprint precisely, not by running it live.
  **Removed** (along with the now-unused `sensor_service`/
  `sensor_scene_id` variables) once this was surfaced - unlike A, this
  wasn't parked, since it's simple dead-code deletion rather than an
  architecture decision. The blueprint's `default:` action sequence is
  now just the scene.turn_on `if:` (mechanism A, still inline, still
  parked) followed directly by the `apply_lighting` call - no more
  `choose:` wrapper between them.

Two designs were discussed for moving A into `apply_lighting`:

1. **Straight port**: `apply_lighting` gains optional `scene_entity_id`/
   `scope_entities`, calls `compute_scene_coverage` internally, then
   `scene.turn_on` (if active) + light dispatch on `uncovered_entities` -
   otherwise identical behaviour to today. Hit a real complication:
   `scene_active` is currently also read by the blueprint's own
   `condition:` block (gates the adaptive/extra tick so it doesn't
   redundantly reapply while a scene owns the room) - and `condition:`
   can't call services, the exact same constraint already documented
   above for why target-resolution extraction was rejected. Moving A
   server-side means that specific optimisation is lost - the tick
   would still fire and call `apply_lighting`/`scene.turn_on` every time,
   just as idempotent no-ops instead of not running at all. Functionally
   harmless, not behaviourally identical. User's call when this was
   surfaced: accept that tradeoff if this gets picked back up (don't
   leave a partial scene_active duplicate in `condition:` just to avoid
   it) - but the whole thing got parked before this was implemented.
2. **Bigger idea, raised by the user**: instead of calling `scene.turn_on`
   at all, read the scene's own *stored per-entity target values*
   (brightness/colour) and feed them through `apply_lighting`'s existing
   grouping/multiplier pipeline like any other target - so a brightness
   multiplier could scale a scene's own brightness, which today it
   explicitly cannot (scene-covered entities are excluded from
   multiplier application entirely). Checked this for real rather than
   guessing: `ha_config_get_scene("kitchen_night")` against the live
   instance confirms scenes do store full per-entity attributes
   (`brightness`, `hs_color`/`rgb_color`/`xy_color`, `effect`, etc.) and
   there's a documented way to read them. But real complications, not
   hypothetical: (a) a scene captures whichever colour mode was active
   when recorded - `kitchen_night`'s own config has one light in `xy`
   mode with `color_temp_kelvin: null` and another with only `hs_color`,
   no `color_temp` at all, so extraction needs real per-mode handling
   `apply_lighting` doesn't have today (it only understands colour-temp
   and RGB); (b) reading a scene's *stored config* (vs. the live
   `entity_id` attribute `compute_scene_coverage` already reads) is a
   different, less-trodden Home Assistant surface than the
   `hass.states`/`entity_registry`/`device_registry` trio this
   integration relies on everywhere else - exactly the class of
   internals lessons 7-9 warn about getting burned by; (c) scenes can
   carry `effect` and non-light domains `apply_lighting` has no model
   for at all. Judged a genuinely bigger feature - closer to "partially
   reimplement scene reproduction with brightness scaling" - than
   something to fold into an already-long session. Not started.

Whoever picks this back up: start from design 1 (the straight port) as
the smaller, already-mostly-scoped piece; treat design 2 as its own
separate decision, not a prerequisite.

**Multi-sensor support, via config subentries.** The "future direction
(not started)" flagged earlier in this section - "letting the
integration run multiple independent config-entry instances, each with
its own schedule and its own sensor entity" - is now implemented, using
Home Assistant's config *subentries* rather than multiple config
entries (confirmed against real HA core source - `home-assistant/core`'s
`wsdot` integration - before committing to the design, not guessed).

- `curve.py`'s `brightness_for_phase`/`kelvin_for_phase`/`targets_for_phase`
  gained keyword-only params for every previously-hardcoded brightness/
  Kelvin literal (`day_brightness`, `evening_brightness`,
  `night_brightness`, `morning_kelvin`, `day_end_kelvin`,
  `evening_kelvin`, `night_kelvin`) - up to 8 configurable numbers per
  schedule instance, each defaulting to the original literal.
- **`night_floor_kelvin` (the optional "let RGB go warmer than
  color_temp bulbs can" delta from the RGB colour work above) was
  dropped entirely**, at the user's explicit direction ("I'm honestly
  not sure why we want this, so bin it for now") rather than folded
  into the new design. `night_kelvin` is now simply the one real,
  always-used Night/late-Evening target; `kelvin_rgb` in
  `targets_for_phase`'s return always equals `kelvin` now (kept as a
  key since `sensor.py` and `apply_lighting`'s RGB path already read
  it). `DEFAULT_NIGHT_FLOOR_KELVIN` renamed to `DEFAULT_NIGHT_KELVIN`
  to match.
- Found two real, pre-existing quirks while generalizing the hardcoded
  formulas, both predating this session: the brightness fade's literal
  `80 + 160*t` doesn't reduce to `night_brightness + (evening_brightness
  - night_brightness)*t` (160 ≠ 100) - it's actually `1.6 ×` the true
  span, meaning the fade completes at ~37.5 minutes into the nominal
  1-hour window and holds for the rest; preserved as a ratio (not the
  literal span) so a custom brightness range keeps the same timing
  shape. Separately, the *old* Kelvin evening-tail fade used a fixed
  `+500` offset regardless of `night_floor`'s value (not derived from
  `evening_kelvin - night_floor`), which only ever produced a correct,
  continuous curve when `night_floor` was left at its default - passing
  a custom `night_floor` (as the old RGB feature allowed) silently
  produced a discontinuous curve (a jump at the fade boundary). Since
  this quirk was a direct symptom of the very feature just removed, the
  new `night_kelvin`/`evening_kelvin` fade uses the mathematically
  correct, continuous `evening_kelvin + (night_kelvin - evening_kelvin)*t`
  instead of reproducing the old discontinuity - the only intentional
  default-output change in this pass, and only reachable via a
  now-removed override, not the zero-config path.
- `config_flow.py` gained `SensorSubentryFlow` (subentry type
  `"sensor"`, registered via `async_get_supported_subentry_types`) -
  required name + 5 required time fields (vs. optional on the main
  entry, where blank means "services only") + the same optional curve
  fields. Dedup on `unique_id=slugify(name)` checked against existing
  subentries before creating.
- **Real gap found and fixed while implementing, not anticipated in
  planning**: adding/removing/reconfiguring a subentry does *not*
  automatically reload the config entry (confirmed by reading
  `ConfigSubentryFlowManager`/`async_add_subentry` in HA core directly -
  they only fire `update_listeners`, they don't reload). Fixed with one
  `entry.add_update_listener(...)` registered in `__init__.py`'s
  `async_setup_entry`, which reloads on any entry/subentry change - this
  is also why `config_flow.py`'s reconfigure steps call
  `async_update_and_abort`, not `*_reload_and_abort`: the subentry
  version of `*_reload_and_abort` actively **raises** `ValueError` if an
  update listener is registered, and the main-entry version logs a
  deprecation warning pointing at exactly this pattern
  ("has an update listener and should use it for scheduling a reload").
- `coordinator.py` gained `ScheduleInstance` (one per default-entry-if-
  configured plus one per `"sensor"` subentry) and `schedule_instances(entry)`,
  the single place that enumerates them; `__init__.py`/`sensor.py`/
  `select.py` all iterate it instead of each assuming one coordinator
  per entry. `ScheduleCoordinator` now takes a `ScheduleInstance`
  instead of a `ConfigEntry` directly. Named instances get entities
  prefixed with their slugified name (`sensor.living_room_adaptive_lighting`,
  `select.living_room_adaptive_lighting_phase`, etc.) and a prefixed
  friendly name too (not just a prefixed `entity_id` - two named
  sensors would otherwise both display as plain "Morning Start" in the
  UI, which was caught before shipping, not after).
- Confirmed directly against HA core source (`homeassistant/config_entries.py`):
  `ConfigSubentryFlow` has no `VERSION`/`MINOR_VERSION` of its own (that's
  `ConfigFlow`-only) - no subentry migration story needed.
- Dashboard card needed no *code* changes (its existing
  `config.entities` override already supports pointing a second card
  instance at a named sensor's entities) - only stale comments
  referencing the now-removed `night_floor_kelvin` were updated for
  accuracy.
- Not yet deployed or tested live - unit tests (`pytest`, 41/41 passing)
  and `py_compile` only so far, consistent with this repo's usual
  "verify locally first, live deployment is its own explicit step"
  pattern.

**Deployed via HACS, then simplified based on live user feedback -
before ever configuring a sensor.** Pulled onto the live instance via
`ha_manage_hacs` (`update_information` then `download`), restarted to
activate. Trying the new config flow live surfaced two real usability
gaps, both fixed the same session:

- The "Add Sensor" subentry form showed every field blank, forcing a
  full retype even when the new sensor's schedule would obviously match
  an existing one. Fixed by pre-filling from the most recently added
  sibling subentry (`existing[-1].data`) - falls back to a blank form
  only for the very first sensor ever added. A validation-error retry
  (a clashing name) re-suggests whatever was just submitted instead of
  reverting to that default, so fixing the name doesn't cost the rest
  of the form.
- **Bigger one, prompted by the user directly questioning the design**:
  "why am I still asked for schedule input when just adding the
  integration - isn't that only relevant for a new sensor instance?"
  Fair question - the main config entry carrying its own optional
  schedule was a leftover from before subentries existed, now a pure
  special case (an unnamed "instance zero" living alongside named ones)
  with no real benefit, since nothing on the live instance actually
  used it (it had only ever been configured services-only). Removed
  entirely: `async_step_user` now creates the entry immediately with
  `data={}` and no form at all; `async_step_reconfigure` was deleted
  from the main flow (nothing left to reconfigure - HA hides the
  "Configure" button once that method doesn't exist);
  `schedule_instances()` no longer has an entry-derived branch, only
  subentries. This does remove a capability - the unprefixed
  `sensor.adaptive_lighting` naming - which used to come from the main
  entry by default.

  That capability came back a different way, though, from a second
  round of user pushback: "can't we just make the prefix optional and
  throw an error on a name collision - we have to handle collisions
  anyway, right?" Correct, and simpler than maintaining two separate
  mechanisms (a main-entry path and a subentry path) for what's really
  one concept. `SensorSubentryFlow`'s `name` field became
  `vol.Optional("name", default="")` - blank gives bare entity IDs
  (`sensor.adaptive_lighting`, matching the original single-sensor
  naming and the old Jinja package's convention) instead of a
  slugified prefix. The existing duplicate-name collision check
  (comparing `slugify(subentry.title)` across siblings) covers the
  blank case for free, since `slugify("")` is consistently `""` for
  every blank-named subentry - no separate "is there already a default"
  check needed, and at most one subentry can end up unprefixed as a
  result. `ScheduleInstance.title` stays `""` in that case too (not a
  fallback display label), so entity *friendly names* stay bare
  ("Morning Start", not "Default Morning Start") exactly like the
  entity_ids do - the two needed to derive from the same blank/non-blank
  signal, not from two different sources with different fallbacks.

  Net effect: there's exactly one way to add a schedule (via "Add
  Sensor"), and exactly one way to get the historical bare-name
  behaviour (leave the name blank on that one call) - not a config-entry
  special case plus a subentry special case doing similar things two
  different ways.

  This was caught and fixed *before* any real sensor was ever
  configured on the live instance (the exchange happened right after
  the HACS update, while the user was still exploring the new "Add
  Sensor" flow for the first time) - so there was no live migration
  concern to design around, just get the shape right going forward.
  Still not deployed again / tested live as of this note - the fix
  needs another HACS update + restart cycle to reach the instance that
  originally surfaced the complaint.

**Pushed straight to `main` and deployed, at the user's explicit
request** ("just push to main and update my HA instance") - the only
time this repo's workflow skipped the usual PR-then-merge step. Local
`multi-sensor-subentries` branch was rebased onto the latest
`origin/main` (it had fallen behind its own already-merged PR #5) and
pushed directly with `git push origin multi-sensor-subentries:main`,
then pulled onto the live instance via the same `ha_manage_hacs`
update_information + download + restart sequence as every prior
deployment.

**First-ever real crash, caught immediately on restart**: setting up
the auto-seeded default sensor threw
`AttributeError: 'str' object has no attribute 'isEnabledFor'` from
inside `DataUpdateCoordinator._async_refresh`. Root cause:
`ScheduleCoordinator.__init__` called
`super().__init__(hass, __name__, name=..., ...)` -
`DataUpdateCoordinator`'s second positional parameter is `logger:
logging.Logger`, not a name string, and `__name__` is a plain string
(confirmed against HA core source before trusting the diagnosis, not
guessed). This bug predates the entire multi-sensor session - it was
in the very first version of `coordinator.py` - but had never actually
run, because every live test up to this point had the schedule left
unconfigured (services-only). The moment a real `ScheduleCoordinator`
was instantiated and refreshed for the first time ever (via the new
auto-seeded default sensor), the dormant bug surfaced immediately.
Fixed with `_LOGGER = logging.getLogger(__name__)` at module level,
passed to `super().__init__` instead of the bare string. Worth
remembering as its own class of bug, alongside lessons 7-9: a
constructor argument with a wrong-but-similar-shaped type (a string
standing in for a logger) can sit completely dormant through months of
"working" code as long as the path that actually uses it - here,
`self.logger.isEnabledFor(...)` inside the coordinator's own refresh
cycle - never runs.

**Same pass, three field-shape fixes from live feedback, once the
crash was out of the way**:
- `morning_brightness` split out from `day_brightness` (which
  previously covered both Morning and Day) - the four phases each get
  their own independent brightness knob now, matching how Kelvin
  already worked. `curve.py` gained named constants for all eight
  defaults (`DEFAULT_MORNING_BRIGHTNESS` etc.) plus a
  `DEFAULT_CURVE_VALUES` dict keyed exactly like `coordinator.py`'s
  `CURVE_KEYS`, replacing the bare literals the function signatures
  used to carry directly.
- The config_flow form's curve fields showed as blank optional boxes
  with no indication of what "leave it blank" actually resolves to.
  Fixed by giving each `vol.Optional(...)` a real `default=` pulled
  from `DEFAULT_CURVE_VALUES`, so the form now shows the actual ported
  values (255/6667/255/4000/180/3200/80/2700) rather than nothing.
- Fields reordered to group by phase (Morning brightness+Kelvin, Day,
  Evening, Night) instead of by attribute type (all brightness fields,
  then all Kelvin fields) - `coordinator.py`'s `CURVE_KEYS` tuple order
  now drives both the config_flow field order and the compute_curve
  service schema's order, since both are built by iterating it.
  `sticky_phase_override` moved from first to last in the form - a
  behaviour toggle, not part of the curve itself, so it doesn't belong
  ahead of the numbers it's unrelated to.

**Boundary sensors removed, folded into attributes - user feedback
after actually seeing them live.** Each sensor used to produce six
entities: `sensor.<name_>morning_start`/`day_start`/`evening_start`/
`night_start` (one per phase boundary) plus the combined
`sensor.<name_>adaptive_lighting` and `sensor.<name_>adaptive_lighting_curve`.
The four boundary sensors were called out directly as noise: "If I want
a boundary automation I'll just trigger it on the phase change and
check that the phase is the one I want" - i.e. `platform: state,
attribute: phase` on the combined sensor already covers the automation
case those existed for, so four always-on entities per sensor just to
occasionally read one attribute wasn't earning their keep.

Removed `sensor.py`'s `_BoundarySensor` class entirely; the same six
values (`morning_ts`/`day_ts`/`evening_ts`/`night_ts`/
`evening_earliest_ts`/`evening_latest_ts` already in
`coordinator.py`'s data - nothing new to compute) now go out as
`attributes.morning_start`/`day_start`/`evening_start`/`night_start`/
`evening_earliest`/`evening_latest` on `_AdaptiveLightingSensor`
instead. Two entities per sensor now, not six.

The dashboard card was a real consumer of the four removed sensors
(boundary lines on the chart, the evening clamp-band explanation) -
not just docs to update. `DEFAULT_ENTITIES` lost its `morning`/`day`/
`evening`/`night` keys; `set hass()` now reads all six boundary values
off the `phase` entity's attributes instead of four separate entity
lookups, and the "missing entity" check narrowed from four entities to
one. `dashboard/preview.html`'s fake `hass.states` updated to match
(the four fake boundary-sensor entries collapsed into extra attributes
on the fake `sensor.adaptive_lighting`). Verified by actually serving
the preview over HTTP and screenshotting it in the browser afterward,
not just by reading the diff - boundary lines, labels, and the evening
clamp-band footnote all rendered correctly with zero console errors.

**Second live thread-safety crash, same session, same root cause class
as the coordinator logger bug.** Logs showed a repeating
`RuntimeError: Detected that custom integration 'adaptive_lighting_helpers'
calls hass.async_create_task from a thread other than the event loop`
from `_refresh_all` - the listener that refreshes every schedule
instance's coordinator on `sun.sun`/override-select changes. Confirmed
against HA core source (`homeassistant/core.py`'s
`get_hassjob_callable_job_type`) before fixing: a plain `def` with no
`@callback` marker gets classified `HassJobType.Executor` and run in
the worker thread pool, where `hass.async_create_task()` isn't safe to
call. Fixed by decorating `_refresh_all` with `@callback` from
`homeassistant.core`. Same underlying lesson as the logger bug -
dormant until a real schedule instance existed to register this
listener for the first time live; neither bug could have been caught
by unit tests, both needed an actual live HA event loop.

**Live troubleshooting also surfaced a real, unrelated entity-naming
collision** - `sensor.adaptive_lighting_curve` got suffixed to `_2`
because a pre-existing `template`-platform sensor (not from this
integration, not the old `solar_adaptive_lighting` package either -
some other leftover template) already occupied that bare entity_id.
Confirmed via `ha_get_entity`'s `platform`/`config_entry_id` fields
before concluding it wasn't a bug in this integration. Not fixed here -
it's the user's own unrelated entity to rename or remove.

**Device-per-sensor grouping, prompted directly by the user disliking
the naming**: "I don't like how we create the sensor names currently -
can't we just let the user name the sensor whatever they want?" The
actual complaint traced to a concrete symptom: friendly names were
built by string-concatenating the typed name with `" Adaptive
Lighting"` (e.g. typing "upstairs" produced "upstairs Adaptive
Lighting" - lowercase and all, no proper title-casing). First checked
whether this was actually a renaming-doesn't-persist bug by reading
`entity_platform.py`'s registration flow directly - it isn't: an
explicitly-set `entity.entity_id` only seeds `suggested_object_id` on
first creation, and `entity_registry.async_get_or_create` looks up by
`unique_id` first, so a user rename via Settings → Entities already
persists across restarts. The real fix was giving each **named**
sensor its own HA **device** (`ScheduleInstance.device_info`, a new
property - `None` for a blank-named/bare-entity-ID instance, which
keeps its device-less bare name unchanged) with `has_entity_name=True`
on its entities and a bare `_attr_name` ("Adaptive Lighting", "Adaptive
Lighting Curve", "Adaptive Lighting Phase") - HA prefixes the device's
own name for display automatically, so renaming a sensor is now one
action (Settings → Devices → rename) instead of us reconstructing a
name via string concatenation. Confirmed against HA core source
(`entity_platform.py`'s `_async_add_entity`) that setting
`entity.device_info` is sufficient - HA auto-creates the device via
`device_registry.async_get_or_create`, correctly threading through the
same `config_subentry_id` already passed to `async_add_entities`, no
manual device-registry calls needed. Entity_id derivation deliberately
left unchanged (still forced, still stable) - only the *displayed*
name construction changed, so no existing automation/dashboard
reference breaks.

**Curve sensor: explored having the dashboard card fetch it directly
instead of reading a stored sensor, at the user's request** (prompted
by the "State attributes... exceed maximum size of 16384 bytes"
recorder warning showing up in the same log check). Worked through the
mechanics concretely rather than estimating: `compute_curve` returns
one instant, not a day - getting the full 289-point curve the card
needs would mean either 289 separate service calls per render (clearly
impractical) or a new curve-returning service, which would just move
the same computation from "coordinator computes once/60s, pushed as
state" to "card requests it every ~30s via its own render timer" -
more total computation, not less, plus a WS/service round-trip on
every render the current design doesn't have. The one real win a
service-based fetch would offer - avoiding the recorder-size warning,
since recorder doesn't store service responses - turned out to be
achievable directly on the existing sensor anyway, via HA's real
`_unrecorded_attributes` mechanism (confirmed present in
`homeassistant/helpers/entity.py`). Added
`_unrecorded_attributes = frozenset({"points"})` to `_CurveSensor` -
one line, keeps the sensor available to automations, no card changes,
no new service. Recommended against pursuing the fetch-directly
direction after this - the concrete exploration didn't surface an
actual win it would provide that the existing design couldn't already
get more simply.

**Device grouping fixed twice more, same live-testing session - both
times the user caught a real gap immediately.** First: the device
change above only applied to *named* instances - a blank-named
instance (including the auto-seeded first sensor everyone actually
gets) stayed device-less, on the old flat-name style. Caught the moment
the user looked at their own default sensor ("that's just a plain old
sensor... that must mean we've got a bunch of code supporting the old
style still hanging around"). Fixed by making `device_info` unconditional
- every instance gets a device, blank-named ones falling back to
"Adaptive Lighting" - and switching entity naming to the idiomatic HA
pattern throughout: the primary sensor sets `_attr_name = None` (displays
as just the device's name, not a repeat of it) while the curve
sensor/phase select get short names ("Curve"/"Phase") that HA
concatenates with the device name for display. This also explained a
second observation ("when I rename the device I don't get an offer to
update entity names") - that offer is specific to the *old*
`has_entity_name=False` style, where each entity's full name is a
static string that needs a one-time bulk update when the device is
renamed; with `has_entity_name=True` throughout, the displayed name is
computed live from the device name every time, so there's nothing to
offer - renaming just works instantly, which is correct, not a missing
feature.

Second: once every instance had a device, the user immediately
questioned whether the blank-name/bare-entity-ID special case still
made sense at all ("it doesn't make sense to have a blank named
device"). Agreed and removed it - `SUBENTRY_FIELDS`'s `name` field went
back to `vol.Required`, and the auto-seeded first sensor is now named
"Default" (`DEFAULT_SENSOR_NAME` in `config_flow.py`) instead of "".
Named "Default" specifically, not "Adaptive Lighting" - the latter
would have produced a stuttering `sensor.adaptive_lighting_adaptive_lighting`
entity_id once every instance is unconditionally prefixed. This
does give up byte-identical bare-name compatibility with the old Jinja
`packages/*.yaml` naming (`sensor.adaptive_lighting` with no prefix at
all) that was one of the original motivations for the blank-name
option - judged a reasonable trade once it became clear that same bare
name was *already* causing live friction anyway (see the unrelated
`sensor.adaptive_lighting_curve` collision noted above, which happened
precisely because nothing enforces bare names being actually free).
`coordinator.py`'s `slugify(...) or ""` prefix fallback and
`device_info`'s `self.title or "Adaptive Lighting"` fallback were both
deliberately left in place rather than ripped out - harmless defensive
code, and specifically what keeps the live instance's existing
blank-titled subentry (created before this change, still on disk)
working unmodified on its next reload rather than picking up a broken
`sensor._adaptive_lighting`-style single-underscore prefix. Not a full
migration - the user's existing default sensor still needs removing
and re-adding by hand to actually pick up the new "Default" naming and
device grouping; only new installs get it automatically.

**Full-repo review pass (cleanup / architecture / polish), at the
user's request - not yet deployed live.** Three real bugs found by
reading, all fixed:

- **Sunset rollover in `coordinator.py`'s `_compute_boundaries`**:
  `sun.sun`'s `next_setting` is exactly that - *next* - so the moment
  today's sunset passes it points at tomorrow's, ~24h ahead, and
  `max(earliest, min(sunset, latest))` clamps it to `latest`. Net
  effect: any day sunset lands before the latest bound, the Evening
  boundary jumped *later* right after Evening started and the phase
  flipped back to Day until the latest bound (in winter, Day until
  20:00 despite a 16:00 sunset). The card already knew about and
  corrected this exact rollover for display (`sunTimeInWindow`); the
  coordinator didn't. Fixed by projecting the sunset's local
  time-of-day onto today (same style as `_time_str_to_today_timestamp`).
  Never caught live because the sensors have only run live for short
  stretches, mostly around midday config-flow testing.
- **The blueprint's `adaptive_attr` trigger was dead code that would
  have bypassed occupancy gating if it ever fired.** A template trigger
  only re-evaluates when entities its template references change - that
  template referenced none (only `trigger.*`), so it rendered once at
  startup (false) and never again. It was also redundant: a bare
  `platform: state` trigger with no to:/from:/attribute: fires on
  attribute-only changes, which is exactly how the `adaptive` trigger
  already ticks every minute (the sensor's state string - the phase -
  only changes 4x/day; corroborated by live traces of the per-minute
  tick). And had it ever fired, `condition:` doesn't list
  `adaptive_attr` in the occupied-gated branch, so it would have run
  the default action in an empty room. Removed; the `adaptive`
  trigger's comment now documents the attribute-change behaviour.
- **`apply_lighting` silently dimmed every light to brightness 1 on a
  broken sensor**: `_read_sensor_targets` defaulted a missing
  `brightness` attribute to 0, which grouping's minimum-1 floor turned
  into "all lights on at brightness 1" with nothing logged anywhere.
  Now raises `ServiceValidationError` naming the missing attribute
  (brightness/color_temp are required by the documented contract;
  rgb_color stays optional). Behaviour change worth knowing: a
  motion-on against an unavailable sensor now errors in the trace
  instead of barely-turning-on the lights.

Cleanup in the same pass:

- **`kelvin_rgb` fully removed** - it was the vestige of the binned
  night_floor feature, always equal to `kelvin`, and its documented
  justification ("sensor.py and apply_lighting already read it") was
  no longer true: its only real consumer was the card's RGB-divergence
  display (caps/ring/legend/tooltip), which existed solely to visualise
  the same removed feature. Gone from `targets_for_phase`, the curve
  points, the card, the preview generator, and the docs.
- **The card's DEFAULT_ENTITIES pointed at entity_ids a fresh install
  no longer creates** - the auto-seeded sensor is named "Default", so
  its entities are `sensor.default_adaptive_lighting`(_curve). Card
  defaults, preview.html's fake states, and house-settings-card.yaml
  all updated to match; the card also gained a `sensor: living_room`
  config shorthand (slugified name) so pointing a card at another named
  sensor doesn't take four entity overrides. NOTE: the live instance's
  pre-rename blank-titled subentry produces bare entity_ids - its card
  will need `sensor:`/`entities:` config or (better) the
  already-planned remove-and-re-add of the Default sensor.
- **Override refresh collapsed to one mechanism**: __init__.py's state
  listener now tracks only `sun.sun`; the phase select refreshes its
  own coordinator (it's the only writer of its own state), including a
  new refresh after restart-restore of a pinned phase - previously the
  restore path only worked because the global listener happened to
  catch the entity's initial state write.
- `ScheduleInstance.key`/`subentry_id` (always identical) collapsed
  into `subentry_id`. Stale docs fixed: preview.html's "pyscript"/
  night-floor comments and wrong serve instructions (must serve the
  repo root, not dashboard/), the card's "updates every 10 minutes"
  footnote (it's 60s), the blueprint's `prefer_rgb_color` description
  still claiming warmer-than-native-range (night-floor era), blueprint
  `source_url` (was a bare github user page), README's Status/layout
  (sensors are live-tested; scenes.py is tested too), HELPERS.md's
  "two entities" (it's three).

Architecture review conclusions (assessed, deliberately NOT changed):
the four-service shape is right (two pure planners, one dispatcher,
one generic scene helper); the blueprint/integration split and the
parked scene-port (design 1) stand as documented above - design 1
remains the right next step only if the blueprint should shrink
further, with the already-accepted loss of `condition:`-level
scene_active suppression. `hass.data` could become
`entry.runtime_data` someday; not worth churn. The blank-title
defensive fallbacks in coordinator.py stay until the live blank-titled
subentry is re-added as "Default", then can go. Card distribution:
bundling the card into the integration (manifest depends on
frontend+http, JSModuleRegistration) still looks better than a second
HACS repo - unstarted either way.

Verified: 43/43 pytest, py_compile on every integration/dashboard
module, `node --check` on the card, and preview.html screenshotted in
a browser (bars/boundaries/clamp band/sun markers/now marker all
rendering, zero console errors).

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
