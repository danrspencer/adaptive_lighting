"""
Day-phase/curve sensors, backed by coordinator.py - a native
replacement for a Jinja `packages/*.yaml` template-sensor setup (see
CLAUDE.md for the live version this ports). One set of these per
schedule instance (see coordinator.py's ScheduleInstance/
schedule_instances) - one per named "sensor" subentry.

Entity IDs are prefixed with the sensor's slugified name (e.g.
sensor.living_room_adaptive_lighting) - see coordinator.py's
schedule_instances(). Every sensor also gets its own device (see
ScheduleInstance.device_info). has_entity_name=True plus name=None (the
idiomatic HA pattern for "the entity that represents the device") lets
it display as just the device's own name ("Upstairs", or "Adaptive
Lighting" by default), so renaming the sensor is one action
(Settings -> Devices -> rename) rather than us reconstructing a name
via string concatenation.

Day-phase, the brightness/colour-temperature "right now" values,
today's four phase-boundary timestamps, and the full-day brightness/
colour curve are all combined into a single sensor.adaptive_lighting
(state = phase, attributes = phase/brightness/color_temp/morning_start/
day_start/evening_start/night_start/evening_earliest/evening_latest/
points) rather than separate sensors per value - `brightness`/
`color_temp` are exactly the attribute names the blueprint's
`adaptive_sensor` input already reads via state_attr(), matching the
shape the old packages/adaptive_lighting.yaml `sensor.solar_adaptive_lighting`
sensor used, so this is a drop-in for that role. The four boundary
timestamps used to be their own sensor.morning_start/day_start/
evening_start/night_start entities - folded into attributes here
instead (four extra always-on entities per sensor that exist just to be
read as one-off attribute lookups was judged not worth it; a
phase-change automation reads sensor.adaptive_lighting's phase attribute
directly, and anything that specifically wants a boundary time - the
dashboard card, in particular - reads it off this same entity's
attributes). A standalone day-phase entity was considered and dropped
for the same reason - anything that wants to react to just the phase
changing can use a `platform: state, attribute: phase` trigger on this
entity, no separate entity required. `points` (the 289-sample day curve
the dashboard card renders) used to live on its own sensor.*_curve
entity - folded in here too, since `_unrecorded_attributes` (what keeps
it out of the recorder's 16KB-limited attribute storage) is a plain
per-attribute-name class field, not something that ever needed a
dedicated entity to work.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import CONF_OWNER_SENSORS, DOMAIN, EVENT_LIGHT_OVERRIDDEN
from .coordinator import ScheduleCoordinator, ScheduleInstance, schedule_instances
from .override_protection import classify
from .write_tracking import SIGNAL_WRITE_TRACKING_UPDATED, LastWriteTracker


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    # Entry-scoped, not tied to any one schedule instance - added
    # unconditionally (even with zero schedule sensors configured) via
    # its own async_add_entities call with no config_subentry_id, so it
    # gets no device at all (see _WriteTrackingSensor's own docstring
    # for why that matters).
    write_tracker: LastWriteTracker = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([_WriteTrackingSensor(hass, entry, write_tracker)])

    setup_owner_entities(
        hass,
        entry,
        write_tracker,
        async_add_entities,
        Platform.SENSOR,
        lambda owner_id, area_id: [
            _OwnerCountSensor(hass, entry, write_tracker, owner_id, status, area_id)
            for status in ("controlled", "overridden")
        ],
    )

    for instance in schedule_instances(entry):
        coordinator: ScheduleCoordinator = hass.data[DOMAIN][instance.subentry_id]
        entities = [_AdaptiveLightingSensor(coordinator, instance)]
        async_add_entities(entities, config_subentry_id=instance.subentry_id)



def setup_owner_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    write_tracker: LastWriteTracker,
    async_add_entities: AddEntitiesCallback,
    platform: Platform,
    factory: Callable[[str, str | None], list[Entity]],
) -> None:
    """Creates one platform's per-owner entities, and keeps up with
    owners that appear later.

    Shared by sensor.py (a controlled/overridden counter pair) and
    button.py (a Clear button) rather than copied into each: the
    disable-path cleanup below is exactly the kind of thing that drifts
    once there are two copies of it.

    Off by default (CONF_OWNER_SENSORS): these are derived entirely from
    what the global write-tracking sensor already exposes, so turning
    them on is a choice about how many entities you want, not about what
    gets tracked. Enabling on a house this size adds a couple of dozen.

    When disabled, any previously-created ones are removed from the
    registry outright rather than left as restored-but-never-recreated
    rows - that kind of litter outlives the feature that made it, and
    toggling off should actually mean off.

    Owners are derived from the write-tracking records themselves, which
    are already persisted, so nothing extra needs storing and a restart
    comes back with the same sensors before any write happens. The flip
    side: an owner whose records all prune away (nothing written for
    STALE_RECORD_MAX_AGE_DAYS) stops being derivable. Its sensors are
    deliberately left in place reporting 0 rather than removed, because a
    room whose lights are simply off for a day would otherwise have them
    vanish and reappear, breaking history continuity for no reason."""
    if not entry.options.get(CONF_OWNER_SENSORS, False):
        registry = er.async_get(hass)
        prefix = f"{entry.entry_id}_owner_"
        for existing in er.async_entries_for_config_entry(registry, entry.entry_id):
            # Filtering on the domain as well as the unique_id prefix
            # matters now that more than one platform creates per-owner
            # entities sharing that prefix - without it whichever
            # platform set up second would delete the first's entities.
            if existing.domain == platform.value and existing.unique_id.startswith(prefix):
                registry.async_remove(existing.entity_id)
        return

    known: set[str] = set()

    @callback
    def _sync() -> None:
        new: list[Entity] = []
        for owner in sorted(_owners(write_tracker.snapshot())):
            if owner in known:
                continue
            known.add(owner)
            new.extend(factory(owner, owner_area_id(hass, owner)))
        if new:
            async_add_entities(new)

    _sync()
    # write_tracking.py already fires this on every record, clear, resync
    # and prune, so a brand-new owner's first write brings its sensors
    # into existence with no polling.
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_WRITE_TRACKING_UPDATED, _sync))


def _classify_tracked(hass: HomeAssistant, entity_id: str, record: dict) -> tuple[str, Any, Any, Any]:
    """One light's current status, shared by every sensor in this module
    so they can't drift apart - this integration has already had one bug
    from two separate copies of this comparison disagreeing (see
    override_protection.py's module docstring).

    Returns (status, claim_owner_id, matched_via, live_context_id).
    "unavailable" is this layer's own case: classify() only ever sees a
    real on/off, so it has no equivalent."""
    state = hass.states.get(entity_id)
    live_context_id = state.context.id if state is not None else None
    if state is None or state.state in ("unavailable", "unknown"):
        return "unavailable", None, None, live_context_id
    raw_status, claim_owner, matched_via = classify(
        state.state == "on",
        record.get("observed"),
        record.get("latest"),
        live_context_id,
        state.attributes.get("brightness"),
        state.attributes.get("color_temp_kelvin"),
        state.attributes.get("rgb_color"),
    )
    # "untracked" (no claim at all, or only one unverified attempt)
    # displays as "controlled": from a viewer's point of view both mean
    # "not excluded from the next tick". The distinction only matters to
    # externally_set()'s own owner-conflict logic.
    return ("controlled" if raw_status == "untracked" else raw_status), claim_owner, matched_via, live_context_id


def owner_of(record: dict) -> str | None:
    """Which owner a tracked record belongs to - the most recent write's,
    falling back to the observed claim's.

    Deliberately the *record's* owner rather than classify()'s
    claim_owner: this answers "who last drove this light", which is the
    grouping a per-room sensor wants, and stays stable even when the
    light is currently held by someone else. A record whose claims carry
    no owner at all (a force/anonymous write, or a resync baseline)
    belongs to nobody and is skipped."""
    for key in ("latest", "observed"):
        claim = record.get(key)
        if claim and claim.get("owner_id"):
            return claim["owner_id"]
    return None


def _owners(snapshot: dict) -> set[str]:
    return {owner for record in snapshot.values() if (owner := owner_of(record)) is not None}


def owner_area_id(hass: HomeAssistant, owner_id: str) -> str | None:
    """The area of whatever entity an owner_id names, if it names one.

    An owner_id is an arbitrary caller-supplied string, but in practice
    it's the calling automation's own entity_id - and that automation is
    usually already assigned to the room it looks after. Following it
    puts these entities in that same room for free, so they turn up
    under the area rather than in an unsorted heap.

    None whenever that can't be established - not an entity_id, not in
    the registry, or the entity has no area of its own and no device to
    inherit one from. The entities are simply left unassigned then,
    which is what they did before this existed."""
    if "." not in owner_id:
        return None
    registry = er.async_get(hass)
    entry = registry.async_get(owner_id)
    if entry is None:
        return None
    if entry.area_id:
        return entry.area_id
    if entry.device_id and (device := dr.async_get(hass).async_get(entry.device_id)) is not None:
        return device.area_id
    return None


@callback
def assign_owner_area(hass: HomeAssistant, entity: Entity, area_id: str | None) -> None:
    """Called from a per-owner entity's async_added_to_hass, once its
    registry entry exists.

    Only ever fills in a *blank* area, never overwrites one - moving one
    of these by hand has to stick. The flip side, accepted: an area
    deliberately cleared back to nothing is treated as never-set and gets
    refilled on the next restart. Setting it to something else instead is
    both the more likely intent and the case that's respected.

    Assigned on the registry entry rather than by giving these entities a
    device, which would be the idiomatic way to get an area. See
    _WriteTrackingSensor's docstring: any device on this config entry
    makes HA's device-rename/area-picker dialog appear when *any* flow on
    the entry completes, including the options flow these entities are
    turned on from."""
    if area_id is None or entity.registry_entry is None or entity.registry_entry.area_id:
        return
    er.async_get(hass).async_update_entity(entity.entity_id, area_id=area_id)


def owner_slug(owner_id: str) -> str:
    """entity_id-safe form of an owner. Strips the domain first
    (automation.kitchen_lights -> kitchen_lights), matching what the
    dashboard card already does for display, so the result reads as the
    room rather than repeating "automation" on every entity."""
    return slugify(owner_id.split(".", 1)[-1] if "." in owner_id else owner_id)


class _OwnerCountSensor(SensorEntity):
    """How many of one owner's lights are currently in one status.

    Two of these per owner - `controlled` and `overridden` - rather than
    a single sensor carrying a status breakdown in its attributes: each
    is then a plain number that graphs, gets long-term statistics, and
    can be built on directly without digging through attributes for the
    figure you actually wanted.

    Note the two counts deliberately do NOT sum to `total_tracked`. A
    light that is off or unavailable is in neither, because override
    protection doesn't apply to it at all (see classify()'s own "off"
    case). `total_tracked` is exposed on both so that gap is visible
    rather than puzzling.

    An overridden light is a supported outcome, not a fault - something
    else deliberately took it and adaptive lighting correctly stepped
    back. These sensors report who currently holds what; they are not a
    health check.

    Deliberately has no device_info, for the same reason
    _WriteTrackingSensor doesn't - see its docstring."""

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "lights"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        write_tracker: LastWriteTracker,
        owner_id: str,
        status: str,
        area_id: str | None = None,
    ) -> None:
        self.hass = hass
        self._write_tracker = write_tracker
        self._owner_id = owner_id
        self._status = status
        self._area_id = area_id
        self._attr_icon = "mdi:lightbulb-group" if status == "controlled" else "mdi:lightbulb-alert-outline"
        # Keyed on the *full* owner_id, not the slug, so two owners that
        # happen to strip to the same slug (automation.kitchen and
        # script.kitchen) stay distinct in the registry - HA de-duplicates
        # the entity_id itself.
        self._attr_unique_id = f"{entry.entry_id}_owner_{owner_id}_{status}"
        slug = owner_slug(owner_id)
        self.entity_id = f"sensor.{slug}_adaptive_{status}"
        self._attr_name = f"{slug.replace('_', ' ').title()} Adaptive {status.title()}"

    async def async_added_to_hass(self) -> None:
        assign_owner_area(self.hass, self, self._area_id)
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_WRITE_TRACKING_UPDATED, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    def _matching_lights(self) -> tuple[list[str], int]:
        lights: list[str] = []
        total = 0
        for entity_id, record in self._write_tracker.snapshot().items():
            if owner_of(record) != self._owner_id:
                continue
            total += 1
            status, _owner, _via, _ctx = _classify_tracked(self.hass, entity_id, record)
            if status == self._status:
                lights.append(entity_id)
        return sorted(lights), total

    @property
    def native_value(self) -> int:
        return len(self._matching_lights()[0])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        lights, total = self._matching_lights()
        # `lights` is a short list of entity_ids, a few hundred bytes even
        # for a large room, so unlike _WriteTrackingSensor's `entities`
        # blob it needs no recorder exclusion - and its history is
        # genuinely useful (which lights, not just how many).
        return {"owner_id": self._owner_id, "lights": lights, "total_tracked": total}


class _ScheduleSensorBase(CoordinatorEntity[ScheduleCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: ScheduleCoordinator, instance: ScheduleInstance, unique_id_suffix: str, forced_object_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{instance.subentry_id}_{unique_id_suffix}"
        self.entity_id = f"sensor.{instance.prefix}{forced_object_id}"
        self._attr_device_info = instance.device_info


class _AdaptiveLightingSensor(_ScheduleSensorBase):
    _attr_icon = "mdi:home-lightbulb"
    _attr_name = None  # the entity that represents the device - displays as just the device's own name
    # points (the full-day curve, 289 samples) is comfortably over the
    # recorder's 16384-byte attribute limit (it was warning and silently
    # dropping this attribute in storage every update) - it's only ever
    # read live off coordinator.data by the dashboard card, never needed
    # from history, so excluding it from the recorder entirely is
    # strictly better than a warning-then-drop every 60s.
    _unrecorded_attributes = frozenset({"points"})

    def __init__(self, coordinator: ScheduleCoordinator, instance: ScheduleInstance) -> None:
        super().__init__(coordinator, instance, "adaptive_lighting", "adaptive_lighting")

    @property
    def native_value(self):
        return self.coordinator.data.get("phase")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        rgb_color = data.get("rgb_color")
        return {
            "phase": data.get("phase"),
            "brightness": data.get("brightness"),
            "color_temp": data.get("kelvin"),
            # list, not tuple - matches what apply_lighting/
            # compute_lighting_groups's rgb_color field and HA's own
            # color_rgb selector expect (see README's "Bring your own
            # sensor" section for the full attribute contract).
            "rgb_color": list(rgb_color) if rgb_color is not None else None,
            # Today's four phase-boundary timestamps, plus the two
            # configured bounds evening_start was actually clamped
            # between - the dashboard card reads all six of these
            # directly off this entity (see www/adaptive-lighting-curve-card.js).
            "morning_start": data.get("morning_ts"),
            "day_start": data.get("day_ts"),
            "evening_start": data.get("evening_ts"),
            "night_start": data.get("night_ts"),
            "evening_earliest": data.get("evening_earliest_ts"),
            "evening_latest": data.get("evening_latest_ts"),
            # The full-day brightness/colour curve (289 samples), also
            # read by the dashboard card - see _unrecorded_attributes
            # above for why this is excluded from the recorder.
            "points": data.get("points"),
        }


class _WriteTrackingSensor(SensorEntity):
    """Diagnostic view into write_tracking.py's observed/latest
    override-protection claims - otherwise a black box only inspectable
    indirectly by probing compute_lighting_groups, which tells you
    *whether* a light is currently excluded, never *why*.

    Deliberately has no device_info. This data is global to the whole
    config entry (one LastWriteTracker shared across every apply_lighting
    call, from every room automation - see write_tracking.py's module
    docstring), not scoped to any one schedule instance, so there's no
    single device it naturally belongs on. Giving it a new device of its
    own would risk reintroducing the "Add Integration creates a device"
    popup this integration's main entry deliberately avoids (see
    CLAUDE.md's "Auto-seeded Default sensor" entry) - confirmed against
    home-assistant/frontend's step-flow-create-entry.ts that the
    device-rename/area-picker dialog is gated purely on whether *any*
    device exists for the config entry at render time (a live registry
    read against `device.primary_config_entry`, not something scoped to
    just the flow that's completing), for every flow type including
    options/subentry flows - so an entity with no device at all can never
    trigger it, regardless of when or how it's created.

    Push-updated via SIGNAL_WRITE_TRACKING_UPDATED for instant feedback
    right when write_tracking.py actually mutates something (a real
    write recorded, or a record cleared on a genuine unavailable
    transition) - but that alone isn't enough to keep `status`/
    `live_context_id` correct, since those are computed by comparing
    against each light's *live* state, which can change independently
    of write_tracking.py ever being touched (a restart, an entity
    reconnecting, anything not funnelled through apply_lighting). Caught
    live: right after a restart, every tracked light briefly reports
    unavailable while its own integration reconnects - the sensor's
    first push (at entity add) captured that transient moment and then,
    with nothing calling apply_lighting again for a while, never
    refreshed, leaving the whole dashboard reading "unavailable" long
    after every light had genuinely settled back to a real state. Also
    polls (default should_poll=True, HA's own DEFAULT_SCAN_INTERVAL,
    currently 15s) as a correctness net for exactly that case - the push
    signal is still what makes a real write feel instant, the poll is
    what keeps everything else honest."""

    _attr_has_entity_name = True
    _attr_name = "Adaptive Lighting Write Tracking"
    _attr_icon = "mdi:text-search"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "entities"
    # The per-entity breakdown can run to several dozen lights - no
    # value in the recorder's history for a live diagnostic snapshot,
    # and it risks the same 16KB attribute-size warning `points` hit on
    # the schedule sensor above.
    _unrecorded_attributes = frozenset({"entities"})

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, write_tracker: LastWriteTracker) -> None:
        self.hass = hass
        self._write_tracker = write_tracker
        self._attr_unique_id = f"{entry.entry_id}_write_tracking"
        self.entity_id = "sensor.adaptive_lighting_write_tracking"
        # entity_id -> last status seen, so _refresh_statuses can fire on
        # the *transition* into overridden rather than every refresh.
        # None (not {}) until the first pass, which is what stops a
        # restart re-announcing every light that was already overridden
        # before it.
        self._last_statuses: dict[str, str] | None = None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_WRITE_TRACKING_UPDATED, self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self._refresh_statuses()
        self.async_write_ha_state()

    async def async_update(self) -> None:
        # The poll path (see the class docstring for why polling exists at
        # all): live state can change without write_tracking being touched,
        # so this is where an override by hand is actually noticed.
        self._refresh_statuses()

    @property
    def native_value(self) -> int:
        return len(self._write_tracker.snapshot())

    def _classify_all(self) -> dict[str, Any]:
        """Every tracked light's current status. Pure - no side effects,
        so `extra_state_attributes` can call it directly."""
        entities: dict[str, Any] = {}
        for entity_id, record in self._write_tracker.snapshot().items():
            status, claim_owner, matched_via, live_context_id = _classify_tracked(self.hass, entity_id, record)
            entities[entity_id] = {
                "status": status,
                # Whichever claim actually matched right now (None for
                # off/unavailable/unclaimed/overridden, where there's
                # nothing to attribute) - classify()'s own second return
                # value, surfaced directly so a viewer doesn't have to
                # cross-reference observed/latest themselves to answer
                # "who owns this light right now".
                "owner_id": claim_owner,
                # "context" or "value" for pending/controlled (how the
                # match was actually determined - a raw context.id
                # equality, or the delayed-echo/mired value rescue),
                # None otherwise - classify()'s third return value, for
                # the dashboard card's "why" explanation.
                "matched_via": matched_via,
                "live_context_id": live_context_id,
                "observed": record.get("observed"),
                "latest": record.get("latest"),
            }
        return entities

    @callback
    def _refresh_statuses(self) -> None:
        """Fire EVENT_LIGHT_OVERRIDDEN for any light that has just passed
        into someone else's hands.

        Edge-triggered deliberately: the event marks the moment a light
        changed hands, not the fact that it currently is. It carries both
        claims and the live values as they were at that instant, because
        that is exactly what can't be reconstructed later - by the time
        anyone looks the curve has moved on, and a stale target says
        nothing about why the light was excluded.

        Kept out of `extra_state_attributes`: firing events is a side
        effect, and HA reads that property on every state write."""
        entities = self._classify_all()
        if self._last_statuses is not None:
            for entity_id, data in entities.items():
                previous = self._last_statuses.get(entity_id)
                if data["status"] == "overridden" and previous != "overridden":
                    self._fire_overridden(entity_id, self._write_tracker.snapshot().get(entity_id, {}), previous, data["live_context_id"])
        # Seeded on the first pass without firing, so a restart doesn't
        # re-announce every light that was already overridden before it.
        self._last_statuses = {entity_id: data["status"] for entity_id, data in entities.items()}

    @callback
    def _fire_overridden(
        self, entity_id: str, record: dict, previous: str | None, live_context_id: str | None
    ) -> None:
        state = self.hass.states.get(entity_id)
        self.hass.bus.async_fire(
            EVENT_LIGHT_OVERRIDDEN,
            {
                "entity_id": entity_id,
                # Who lost it - the record's own owner, not classify()'s
                # matched owner, which is None for an overridden light.
                "owner_id": owner_of(record),
                "previous_status": previous,
                "live_context_id": live_context_id,
                # The live values at this exact moment. Comparing these
                # against each claim's target is the whole diagnosis, and
                # neither survives to be looked up afterwards.
                "live": {
                    "state": state.state if state else None,
                    "brightness": state.attributes.get("brightness") if state else None,
                    "color_temp_kelvin": state.attributes.get("color_temp_kelvin") if state else None,
                    "rgb_color": state.attributes.get("rgb_color") if state else None,
                },
                "observed": record.get("observed"),
                "latest": record.get("latest"),
            },
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"entities": self._classify_all()}
