# Changelog

Notable changes per release. Versions follow [semantic versioning](https://semver.org),
with the 0.x caveat that a **minor** bump is where breaking changes land until 1.0.

The version in `custom_components/adaptive_lighting_helpers/manifest.json` is what
HACS shows as installed, so it and the release tag are checked against each other in
CI — see `.github/workflows/release.yml`.

## [0.2.0] - 2026-08-27

A large release. Override protection was rebuilt around user-configured
tracking scopes, and the integration now installs as two config entries.
Both halves — integration and blueprint — must be deployed together.

### Breaking

- **`owner_id` is gone** from every service and from the blueprint. Which
  scope tracks a light is resolved from configuration, not declared by the
  caller, so two automations driving one room now co-operate instead of
  each reading the other's write as an override.
- **Services renamed** to match what they actually do, now that nothing is
  "owned" by a caller: `check_ownership` → `check_control`,
  `record_ownership` → `record_write`, `clear_ownership` → `clear_claims`.
- **`check_control` no longer takes `force`.** As a question it had one
  possible answer; forcing is something a write does.
- **Two config entries** instead of one — *Schedules* and *Tracking* — so
  the integration page stops flattening both kinds of thing into one list.
  The services live with Tracking. Migrated automatically: the existing
  entry becomes Schedules, keeping every schedule time and curve value.
- **Claims are no longer persisted.** They live on each state device's
  tracking entity and are lost on restart, which leaves every light
  manageable — the state the old startup resync existed to reconstruct.
- **The `adaptive-lighting-write-tracking` card and its global sensor are
  removed**, replaced by per-scope entities that need no custom card.

### Added

- **State devices**: named tracking scopes with an area/device/entity
  target, seeded per area at setup and on upgrade. Each carries a
  `_adaptive_tracking` sensor holding the claims, `_adaptive_controlled`
  and `_adaptive_overridden` counters, and a Clear button.
- **`EVENT_LIGHT_OVERRIDDEN`**, described in the logbook, carrying the
  scope's `device_id` so hand-overs appear in that device's Activity.
- `check_control` reports the `scope` tracking each light, or null when
  nothing does.

### Fixed

- `record_write` reported every entity passed in as recorded, including
  ones skipped for matching no scope.
- Kelvin churn on bulbs whose advertised colour-temperature range is
  narrower than what they actually report.
- The Evening→Night Kelvin fade was not clamped.
- Scope counters went stale until a claim changed, so a light could be
  reported overridden long after it had been turned off.

## [0.1.0]

Initial version, unreleased and untagged — everything before the above.
