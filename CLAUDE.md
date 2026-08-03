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
hard to change safely. This repo is the migration to pyscript for the
parts that are genuinely computation (not triggers/conditions), while
keeping the blueprint native for the parts HA already does well.

This repo's blueprint is deliberately named `adaptive_lighting.yaml`
(blueprint name "Adaptive Lighting"), not `adaptive_lighting_unified`
- different file, different in-UI name, so it can be installed and
tested alongside the live `adaptive_lighting_unified.yaml` without
touching it, and rooms migrated over individually. Linking the two
blueprints to the same filename is exactly what caused the incident
below (see "A same-named blueprint can take out every room at once") -
don't reintroduce that collision.

## The architectural split (deliberate, not arbitrary)

**Stays in the blueprint (Jinja/YAML):**
- All triggers, conditions, target resolution (`resolved_entities`),
  occupancy detection (`occupied`), scene compatibility checking
  (`scene_active`/`scene_valid`), and the action *structure* (which
  service to call, on what target).
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

**Moved to pyscript (`pyscript/modules/adaptive_lighting/grouping.py`):**
- Reachability filtering, multiplier bucketing, the tolerance-based
  "already at target" check, and two-step-vs-combined label routing.
- Why: this was the genuinely gnarly part — nested namespace loops,
  nothing pytest-testable, and a real correctness gap (exact-match
  brightness/colour-temp comparisons that silently stopped skipping
  for any bulb with device-side rounding quirks).

**Also moved to pyscript:** the day-phase brightness/Kelvin curve math
(`pyscript/modules/adaptive_lighting/curve.py`), ported from
`custom_templates/adaptive_lighting.jinja` in the live HA config - not
because it was complicated, but because the whole point of this repo
is "here's a blueprint and some pyscript", not "...and also a
custom_templates file and a packages/*.yaml you also need to copy".

## Hard-won lessons (don't repeat these)

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

## Current status / what's not done

- `pyscript/modules/adaptive_lighting/` (curve.py, grouping.py) - pure
  Python, done, unit-tested (`pytest`), and cross-checked against the
  exact scenarios validated live against the deployed Jinja blueprint
  before the port (same expected outputs). Also now includes
  persistent manual-override protection (`manually_set()`) that the
  live blueprint doesn't have - see lesson 5 above.
- `pyscript/apps/adaptive_lighting_app/__init__.py` - the original
  Phase 0 spike's open questions are now resolved: `is_state`/
  `state_attr`/`device_id`/`labels` work as plain pyscript globals as
  assumed; `hass.states.get(entity_id).context.user_id` is confirmed
  as the way to reach `context.user_id` (the one part that was a
  guess). Getting it to actually *load* took three real fixes -
  lessons 7, 8, and 9 above (path-aliased symlink, `__init__.py`
  naming, app/module name collision causing infinite recursion) - each
  looking like the others' symptom (nothing happens, no error) until
  debug logging cut through it.
- **Not yet confirmed end-to-end as of this writing.** All three fixes
  above are committed and deployed (blueprint copied, pyscript app
  copied under its new name, `packages/adaptive_lighting_pyscript.yaml`
  shipped with the `apps:` config entry). The last live check before
  writing this up showed `pyscript.compute_lighting_groups` still not
  registered after a `homeassistant.reload_config_entry` call - which
  has been flaky all session (dispatches, times out, sometimes doesn't
  actually complete) - and a restart to get a clean, definitive test
  hadn't happened yet when the conversation moved on to discussing the
  HACS-integration question below instead. **Next step: restart HA and
  check `ha_list_services(domain="pyscript")` for `compute_lighting_groups`
  before trusting any of this works.**
- The blueprint's action: block **has been rewired** to call
  `pyscript.compute_lighting_groups` (with `response_variable:
  lighting_plan`) instead of the ~90-line namespace-loop Jinja that
  used to compute multiplier bucketing/tolerance/two-step routing
  inline. The blueprint still owns turning the returned groups into
  actual `light.turn_on`/`light.turn_off` calls. Until the service is
  confirmed registered (above), every non-motion_off/reconcile trigger
  on any automation using this blueprint fails silently past that
  point (no lights commanded, "Service not found" in the automation's
  own error log/trace). Only `automation.living_room_lights_new` uses
  this blueprint right now (old `automation.living_room_lights`,
  original blueprint, is switched off for the duration of this test),
  so blast radius is one room.
- **Deployment splits blueprint vs. pyscript vs. dashboard card
  deliberately** (see lesson 7): blueprint and pyscript are both
  copied, the dashboard card is still symlinked and untested for the
  same path-aliasing problem.
- **The dev/test sync loop** (polling this repo for new commits and
  re-running `link_into_ha.sh` automatically) is set up directly on
  the live HA instance - a `shell_command` + `time_pattern` automation
  (`automation.adaptive_lighting_sync_from_git`), not committed here.
  It's specific to this user's instance and this repo's checkout path
  (`/config/repos/adaptive_lighting`), not something worth publishing -
  don't re-add it to the repo without checking first (see git history
  around the "Rewire blueprint to pyscript, add git auto-sync..."
  commit for what that looked like and why it was reverted). It's also
  currently running a temporary "always re-run link_into_ha.sh
  regardless of git changes" variant of `scripts/adaptive_lighting_sync.sh`
  on the live instance for this session's debugging - **revert it back
  to the normal git-pull-gated version once the pyscript spike above is
  confirmed working**, or every 15-minute tick will re-copy files
  whether or not anything changed.
- `dashboard/preview.html` + `generate_preview_data.py` let you see the
  actual Lovelace card rendered with synthetic data, without a running
  HA instance - regenerate data with
  `python3 dashboard/generate_preview_data.py`, then serve the repo
  root over HTTP (not `file://` - the card's `fetch()` needs it, and
  it must be the repo root, not `dashboard/`, since `preview.html`
  imports `../www/adaptive-lighting-curve-card.js`) and open
  `dashboard/preview.html`.

## Open question: rebuild as a proper HACS integration?

Raised but not decided or started. The pitch: replace
`pyscript/apps/adaptive_lighting_app` with a real
`custom_components/adaptive_lighting` package that registers
`compute_lighting_groups` as a native HA service itself
(`hass.services.async_register`) instead of going through pyscript at
all - which would eliminate the entire class of bugs in lessons 7-9
(pyscript's app-folder naming rules, the `apps:` config requirement,
the name-collision recursion bug, `reload_config_entry`'s flakiness)
since none of that machinery would exist anymore. `grouping.py`/
`curve.py` port over basically unchanged - they're already decoupled
from pyscript via `EntityLookup` dependency injection, that decision
already paying off here. HACS would also handle install/update instead
of `scripts/link_into_ha.sh` and the whole copy-vs-symlink saga (though
the blueprint would likely still need separate handling - HACS's
blueprint-distribution support is thinner than its integration
support).

The dashboard card is a much smaller, separate question: HACS's
original/core use case is distributing custom Lovelace cards (a
"plugin"/"frontend" repository category), and `www/adaptive-lighting-curve-card.js`
already fits that shape as-is (single file, no build step, no external
deps) - just needs a `hacs.json` at the repo root. Installing via HACS
would also remove the manual "register as a Lovelace resource" step.

Three HACS-installable pieces, in other words: a Lovelace plugin (the
card), an integration (replacing the pyscript half), and the blueprint
handled some other way. Nothing about this has been planned in detail
or started - check with the user before assuming this is happening,
since it's a real architecture change, not a small tweak, and the
pyscript-based approach above may still be worth finishing/confirming
first regardless of whether this happens later.

## Testing

`pip install pytest && pytest` from the repo root. No HA/pyscript
dependency for the test suite - `tests/fakes.py` provides a fake
`EntityLookup` so `grouping.py` is exercised with plain dicts. CI runs
this on push/PR (`.github/workflows/tests.yml`) across Python 3.9 and
3.13.
