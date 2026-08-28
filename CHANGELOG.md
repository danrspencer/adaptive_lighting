# Changelog

Notable changes per release. Versions follow [semantic versioning](https://semver.org),
with the 0.x caveat that a **minor** bump is where breaking changes land until 1.0.

The version in `custom_components/flare/manifest.json` is what
HACS shows as installed, so it and the release tag are checked against each other in
CI — see `.github/workflows/release.yml`.

## [0.5.1] - 2026-08-28

### Changed

- The curve card names itself after the schedule sensor it is pointed
  at, rather than always reading "FLARE" — so two cards on one
  dashboard are told apart without configuring a title. An explicit
  `title: ""` still suppresses the header entirely.

### Fixed

- Documentation: seven broken links, entity ids in the reference table
  that still carried the old domain (`sensor.<name_>adaptive_lighting`
  rather than `sensor.<name>_flare`), and a copy-paste dashboard
  snippet that still set `day_end_kelvin` and omitted the transition
  entities. Contributing has moved to `CONTRIBUTING.md` in the
  repository.

## [0.5.0] - 2026-08-28

### Changed

- Adding FLARE now creates both entries in one pass. It was two trips
  through Add Integration, once per entry; two entries is a grouping
  decision and never implied two trips to get there. The flow asks the
  one thing there is to ask - which rooms to track - and raises the
  other entry itself. Either half is still creatable on its own, so
  deleting one and adding it back works.
- The documentation site uses the FLARE icon: favicon, sidebar, and
  link previews. The README leads with it too, which is the one surface
  HACS renders for us.

### Fixed

- The repository had no description, which is exactly what HACS shows
  under a store listing's name. Set, along with topics.

## [0.4.2] - 2026-08-28

### Changed

- Transition defaults tuned: an hour at most boundaries, half an hour
  into Morning, and a full-phase colour slide across Day. Only affects
  new schedule sensors - existing ones keep whatever they are set to.

### Fixed

- The sixteen curve defaults live in `curve.py`, `curve.js` and
  `services.yaml`, and nothing compared the three. Both copies are now
  pinned against `curve.py`, so a stale playground default or a stale
  number in Developer Tools -> Actions fails the build.

## [0.4.1] - 2026-08-28

### Fixed

- The dashboard card's default title was still "Adaptive Lighting".
- The curve playground drew nothing: its `buildPoints` still called
  `targetsForPhase` with the pre-transitions signature, so every point
  came out NaN. The parity test drives the two per-phase functions
  directly and never exercised `buildPoints`, so nothing caught it -
  that gap is now covered.
- Transition durations were displayed with the time-of-day formatter,
  which wraps at 1440, so a whole-phase transition read as "00:00".

## [0.4.0] - 2026-08-28

Every phase transition is now configurable, and Day is no longer a
special case.

### Breaking

- **`day_end_kelvin` is replaced by `day_kelvin`**, which is Day's own
  colour rather than the value it ramped toward. It defaults to Morning's
  (6667), because Day's default transition covers the whole phase. Any
  `compute_curve` caller passing `day_end_kelvin` must rename it.
- **Evening's opening colour ramp moved before the boundary.** The change
  now happens in Day's tail, so the Evening boundary *is* the evening
  colour rather than the start of a ramp toward it.
- **Evening's brightness fade lost its 1.6x ratio**, which made it reach
  the night value about 22 minutes early and hold. It now lands exactly
  on the boundary.

### Added

- **Eight transition durations per schedule sensor** - one per phase per
  channel, in minutes, named for the phase the transition runs in.
  `0` is a hard cut; a duration longer than its phase covers the whole
  phase. Exposed as `number.*` config entities and as `compute_curve`
  fields.
- The curve playground gains a Transitions panel.

## [0.3.1] - 2026-08-28

### Fixed

- Every GitHub and documentation-site URL now points at the renamed
  `danrspencer/flare` repository, including the HACS custom-repository
  URL and the blueprint import badge. The site is published at
  `/flare/`.
- The README - which HACS renders as the store page - was still
  describing "Adaptive Lighting" and linking to a documentation page the
  restructure had removed.

## [0.3.0] - 2026-08-28

Renamed to **FLARE** (Flexible Lighting Automation & Reconciliation Engine).

### Breaking

- **The domain is now `flare`.** Every service is `flare.apply_lighting`,
  `flare.check_control` and so on. Home Assistant keys config entries by
  domain and cannot migrate across one, so FLARE installs as a new
  integration: add it, then remove the old one. Deleting the old
  `custom_components/adaptive_lighting_helpers/` directory is part of the
  upgrade - left in place, its code keeps loading alongside and registers
  its own services against its own entries.
- **The card is `custom:flare-curve-card`**, and the blueprint is
  `flare.yaml` - re-import it and repoint your automations.
- **Entity ids** drop "adaptive" for "flare": `sensor.<name>_flare` for a
  schedule sensor, and `sensor.<name>_flare_tracking` / `_controlled` /
  `_overridden` plus `button.<name>_flare_clear` for a tracking scope.
  The schedule's own time.* and number.* config entities keep their ids.

### Changed

- The documentation site is restructured into three tiers - Home,
  Quickstart, and a Power users section carrying the integration
  reference, scene handoff, and building without the blueprint.

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
  `_flare_tracking` sensor holding the claims, `_flare_controlled`
  and `_flare_overridden` counters, and a Clear button.
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
