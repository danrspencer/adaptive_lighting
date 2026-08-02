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

## Current status / what's not done

- `pyscript/modules/adaptive_lighting/` (curve.py, grouping.py) - pure
  Python, done, unit-tested (`pytest`), and cross-checked against the
  exact scenarios validated live against the deployed Jinja blueprint
  before the port (same expected outputs). Also now includes
  persistent manual-override protection (`manually_set()`) that the
  live blueprint doesn't have - see lesson 5 above.
- `pyscript/apps/adaptive_lighting/app.py` - written but **not yet
  validated against a real pyscript install**. Its docstring lists
  exactly what to confirm first (a "Phase 0 spike"):
  - `is_state`/`state_attr`/`device_id`/`labels` available as plain
    pyscript globals with these names/signatures.
  - `import adaptive_lighting` resolves correctly from
    `pyscript/apps/` to `pyscript/modules/`.
  - `@service(supports_response="only")` + `response_variable` in the
    calling automation actually makes the returned dict usable in a
    later action step's templates, and whether that's dict-style
    (`plan['groups']`) or attribute-style (`plan.groups`) access.
- The blueprint itself has **not yet been rewired** to call
  `pyscript.compute_lighting_groups` - it's still the full-Jinja
  version, copied in as the migration's starting baseline. That
  rewiring is blocked on the Phase 0 spike above.
- The **dev/test sync loop was never finalized** with the user - i.e.
  how code in this repo gets from "written" to "running on the real HA
  instance for testing". Ask before assuming.
- `dashboard/preview.html` + `generate_preview_data.py` let you see the
  actual Lovelace card rendered with synthetic data, without a running
  HA instance - regenerate data with
  `python3 dashboard/generate_preview_data.py`, then serve `dashboard/`
  over HTTP (not `file://` - the card's `fetch()` needs it) and open
  `preview.html`.

## Testing

`pip install pytest && pytest` from the repo root. No HA/pyscript
dependency for the test suite - `tests/fakes.py` provides a fake
`EntityLookup` so `grouping.py` is exercised with plain dicts. CI runs
this on push/PR (`.github/workflows/tests.yml`) across Python 3.9 and
3.13.
