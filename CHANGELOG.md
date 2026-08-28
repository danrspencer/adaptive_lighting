# Changelog

Notable changes per release. Versions follow [semantic versioning](https://semver.org),
with the 0.x caveat that a **minor** bump is where breaking changes land until 1.0.

The version in `custom_components/flare/manifest.json` is what
HACS shows as installed, so it and the release tag are checked against each other in
CI — see `.github/workflows/release.yml`.

## [0.9.3] - 2026-08-28

### Fixed

- **Overriding the day phase now shows that phase's own values, not the
  next phase's.** `_value_at()`'s ramp-easing math computed the
  interpolation factor from the real clock relative to the requested
  phase's own natural time span, then clamped it to `[0, 1]` - which
  stops the ramp extrapolating past the next phase's value, but doesn't
  stop it sliding all the way *to* that value once the real time is
  anywhere past the phase's own end. Forcing `select.<slug>_flare_phase`
  to "Night" during actual evening real time showed Morning's
  brightness/colour (255/7000K) instead of Night's own (80/2700K); the
  same happened forcing any other phase. Fixed by holding the phase's
  own value once real time is strictly past its span's end, rather than
  falling through to the ramp/clamp computation.

## [0.9.2] - 2026-08-28

### Fixed

- **The per-scope Clear button now clears every light in one press.**
  `async_clear` built its "did anything change" check as
  `any(store.claims.pop(...) is not None for ...)` - `any()` short-circuits
  on the first `True`, and `.pop()` is what actually clears each claim, so
  the moment the first entity's claim came back non-`None`, every entity
  after it in the list was silently left untouched. A room with several
  tracked lights needed one press per light instead of clearing the whole
  scope at once.

## [0.9.1] - 2026-08-28

### Fixed

- **The curve card's "now" marker is legible against any colour.** It
  used to be a filled dot on the chart itself, which all but vanished
  whenever the current colour temperature came out pale - a cold white
  sits close to the chart's own background. Replaced with a colour
  swatch on the "Now HH:MM · ..." label instead, matching the swatch
  the hover tooltip already used. Both swatches now carry a black
  border so a pale swatch stays visible against a light card background,
  dropped entirely in dark mode.

## [0.9.0] - 2026-08-28

### Changed

- **`scope_device_id` is renamed to `tracking_device_id`** across all
  five services. It was reusing "scope" for an unrelated concept already
  spoken for by `compute_scene_coverage`'s own `scope_entities` field -
  the exact collision the blueprint's Jinja variable
  (`tracking_scope_device_id`) was already renamed once to avoid.
  Re-import the blueprint alongside this release; without it, every
  automation still sending the old field name stops tracking its lights
  (they still get driven correctly, just without override protection)
  until it's updated.
- **`compute_scene_coverage`'s `scene_entity_id` is now required.** The
  service answers a question about one specific scene, and with no
  candidate scene this tick the caller already knows the answer
  (nothing's covered) without asking - same reasoning as the
  `check_control`/`record_write`/`clear_claims` change in 0.8.0. The
  blueprint doesn't call this service today (it keeps its own inline
  Jinja version), so this has no effect on the shipped automation - it
  only affects direct callers.
- Two `services.yaml` selector fixes found in the same review:
  `compute_scene_coverage`'s `target_entities` now correctly declares
  `multiple: true` (it was rendering as a single-entity picker in
  Developer Tools -> Actions despite the backend requiring a list), and
  `record_write`'s `targets` field no longer declares `object:
  multiple: true`, aligning it with the structurally identical
  `brightness_multipliers` field - both are maps, not lists.

## [0.8.0] - 2026-08-28

### Changed

- **`scope_device_id` is now required on `check_control`, `record_write`
  and `clear_claims`.** Each of those exists only to read or write
  tracking claims, so a call naming nothing has nothing useful to do -
  the schema now rejects a missing/null scope outright rather than
  always answering "untracked" or silently recording/clearing nothing.
  `apply_lighting`/`compute_lighting_groups` are unaffected - both stay
  optional, since either still does something useful (dispatch/plan
  lights) with no scope at all.
- The blueprint's own `record_write`/`clear_claims` calls are now
  skipped, not sent, when Room Target resolves to no tracking scope -
  the turn-off/hand-off still happens, it just isn't recorded. Re-import
  the blueprint alongside this release; without it, a room with no
  resolvable scope would otherwise send a now-invalid call and fail its
  tick.

## [0.7.0] - 2026-08-28

### Changed

- **Override-protection services now take an explicit `scope_device_id`**
  instead of resolving which state device owns a light by searching every
  configured one. Pass the tracking scope's own device (a picker in
  Developer Tools -> Actions) on `apply_lighting`, `compute_lighting_groups`,
  `check_control`, `record_write` or `clear_claims`. **Omitting it now
  writes the light but tracks nothing** - no claim is recorded, and
  nothing is excluded as already externally-set - where it previously
  searched by entity, device and area to find a scope automatically.
  A `scope_device_id` naming something other than one of your own
  tracking scopes raises rather than being silently ignored.
- The blueprint resolves this for you from Room Target - no new input,
  and no behaviour change for rooms whose Room Target already names an
  area. Re-import the blueprint alongside this release; without it,
  every automation still calling the old-shaped services stops tracking
  its lights (they still get driven correctly, just without override
  protection) until it's updated.

## [0.6.0] - 2026-08-28

### Changed

- **Switching a light off is now an override.** FLARE used to ignore it
  and relight the light on the next tick; it now leaves it off. The
  claim is released when every light in its tracking scope is off, so
  a room that empties comes back under FLARE's control on its own.
  Anything not reporting `on` counts as off, unavailable included.
- A turn-off records `{"state": "off"}` as its target, so FLARE's own
  off is distinguishable from anyone else's once the write's context
  expires.
- The blueprint records its own turn-offs. Re-import it alongside this
  release — without that step every light in a room reads as externally
  switched off each time the room empties.

### Fixed

- `overridden` now has an automatic way out. Previously only the Clear
  button or a device drop could end it.

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
