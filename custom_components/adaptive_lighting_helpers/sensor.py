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

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_TRACKING, EVENT_LIGHT_OVERRIDDEN
from .coordinator import ScheduleCoordinator, ScheduleInstance, StateInstance, schedule_instances, state_instances
from .override_protection import classify
from .write_tracking import SIGNAL_WRITE_TRACKING_UPDATED, ClaimRegistry


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    # Both entry types use this platform, and each owns a different set
    # of entities - see const.py's CONF_ENTRY_TYPE.
    if entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_TRACKING:
        for instance in schedule_instances(entry):
            coordinator: ScheduleCoordinator = hass.data[DOMAIN][instance.subentry_id]
            async_add_entities([_AdaptiveLightingSensor(coordinator, instance)], config_subentry_id=instance.subentry_id)
        return

    registry: ClaimRegistry = hass.data[DOMAIN][entry.entry_id]
    for instance in state_instances(entry):
        async_add_entities(
            [
                _StateTrackingSensor(hass, registry, instance),
                _ScopeCountSensor(hass, registry, instance, "controlled"),
                _ScopeCountSensor(hass, registry, instance, "overridden"),
            ],
            config_subentry_id=instance.subentry_id,
        )


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



@callback
def _assign_scope_area(hass: HomeAssistant, entity, instance: StateInstance) -> None:
    """Puts a state device in the area it targets, when it targets
    exactly one - which is what both the setup offer and the upgrade
    migration create. A scope spanning several areas, or targeting
    devices and entities directly, has no single right answer and is
    left unassigned.

    Only ever fills in a *blank* area, never overwrites one, so moving a
    state device by hand sticks. Done on the device rather than via
    DeviceInfo.suggested_area, which is deprecated (breaks in HA 2026.9)
    and takes an area *name*, creating the area as a side effect."""
    areas = instance.target.get("area_id") or []
    areas = [areas] if isinstance(areas, str) else list(areas)
    if len(areas) != 1 or entity.registry_entry is None or entity.registry_entry.device_id is None:
        return
    registry = dr.async_get(hass)
    device = registry.async_get(entity.registry_entry.device_id)
    if device is None or device.area_id is not None:
        return
    registry.async_update_device(device.id, area_id=areas[0])


class _StateTrackingSensor(SensorEntity):
    """One state device's claims - the actual storage, not a view of it.

    `claims` is the dict ClaimRegistry reads and mutates, published as
    this entity's attribute. There is no copy kept in step behind it, so
    what Developer Tools shows and what override protection acts on are
    the same object by construction.

    Not recorded: the largest scope in a real house is around 8 KB of
    claims and it changes on every tick, so history of it would be both
    enormous and useless. The same `_unrecorded_attributes` treatment
    the day-curve `points` attribute already gets.

    Deliberately not restored across a restart either - see
    write_tracking.py's module docstring. A cold start tracks nothing,
    which leaves every light manageable, which is the desired state."""

    _attr_has_entity_name = True
    _attr_name = "Tracking"
    _attr_icon = "mdi:text-search"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "lights"
    # Polls as well as publishing on every mutation. classify() compares
    # claims against each light's *live* state, which changes
    # independently of anything this module does - a restart, an entity
    # reconnecting, a light dimmed by hand. Without the poll, a scope
    # whose claims happen not to change would never notice.
    _unrecorded_attributes = frozenset({"claims"})

    def __init__(self, hass: HomeAssistant, registry: ClaimRegistry, instance: StateInstance) -> None:
        self.hass = hass
        self._registry = registry
        self._instance = instance
        self.claims: dict[str, dict] = {}
        self._last_statuses: dict[str, str] | None = None
        self._attr_unique_id = f"{instance.subentry_id}_tracking"
        self.entity_id = f"sensor.{instance.prefix}adaptive_tracking"
        self._attr_device_info = instance.device_info

    async def async_added_to_hass(self) -> None:
        self._registry.register(self._instance.subentry_id, self)
        _assign_scope_area(self.hass, self, self._instance)

    async def async_will_remove_from_hass(self) -> None:
        self._registry.unregister(self._instance.subentry_id)

    @callback
    def async_claims_changed(self) -> None:
        self._refresh_statuses()
        self.async_write_ha_state()

    async def async_update(self) -> None:
        self._refresh_statuses()
        # The counters are views over these claims, but what they show
        # depends on each light's *live* state, which changes with
        # nothing here being touched - a light switched off, a device
        # reconnecting, someone dimming a bulb by hand. Without this
        # they only refresh when a claim actually mutates, and sit stale
        # in between: caught live reporting a light as overridden
        # minutes after it had been turned off.
        async_dispatcher_send(self.hass, SIGNAL_WRITE_TRACKING_UPDATED)

    @callback
    def _refresh_statuses(self) -> None:
        """Fires EVENT_LIGHT_OVERRIDDEN for any of this scope's lights
        that has just passed into someone else's hands.

        Edge-triggered deliberately: the event marks the moment a light
        changed hands, not the fact that it currently has. It carries
        both claims and the live values as they were at that instant,
        because that is exactly what can't be reconstructed later - by
        the time anyone looks, the curve has moved on and a stale target
        says nothing about why the light was excluded.

        Kept out of extra_state_attributes: firing events is a side
        effect, and HA reads that property on every state write."""
        statuses = {}
        for entity_id, record in self.claims.items():
            status, _owner, _via, live_context_id = _classify_tracked(self.hass, entity_id, record)
            statuses[entity_id] = status
            if self._last_statuses is None:
                continue
            if status == "overridden" and self._last_statuses.get(entity_id) != "overridden":
                self._fire_overridden(entity_id, record, self._last_statuses.get(entity_id), live_context_id)
        # Seeded on the first pass without firing, so a restart doesn't
        # re-announce every light that was already overridden before it.
        self._last_statuses = statuses

    @callback
    def _fire_overridden(
        self, entity_id: str, record: dict, previous: str | None, live_context_id: str | None
    ) -> None:
        state = self.hass.states.get(entity_id)
        device = dr.async_get(self.hass).async_get_device(
            identifiers=self._instance.device_info["identifiers"]
        )
        self.hass.bus.async_fire(
            EVENT_LIGHT_OVERRIDDEN,
            {
                "entity_id": entity_id,
                # The scope that lost it. device_id is what puts this row
                # in that device's own Activity: the logbook's
                # device-scoped query matches on event_data.device_id
                # (recorder db_schema's DEVICE_ID_IN_EVENT). Omitted
                # rather than sent as None when the device somehow isn't
                # registered, so the matcher can't see a null.
                "scope": self._instance.title,
                **({"device_id": device.id} if device else {}),
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
    def native_value(self) -> int:
        return len(self.claims)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"claims": self.claims}


class _ScopeCountSensor(SensorEntity):
    """How many of one scope's lights are currently in one status.

    Two per state device - `controlled` and `overridden` - rather than a
    single sensor with a status blob: each is then a plain number that
    graphs, gets long-term statistics, and can be built on directly.

    Note the two counts deliberately do NOT sum to the tracking sensor's
    own total. A light that is off or unavailable is in neither, because
    override protection doesn't apply to it at all (see classify()'s own
    "off" case).

    An overridden light is a supported outcome, not a fault - something
    else deliberately took it and adaptive lighting correctly stepped
    back. These report who holds what; they are not a health check.

    A view over the tracking sensor's claims, which is why they carry no
    storage of their own."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "lights"
    _attr_should_poll = False

    def __init__(
        self, hass: HomeAssistant, registry: ClaimRegistry, instance: StateInstance, status: str
    ) -> None:
        self.hass = hass
        self._registry = registry
        self._instance = instance
        self._status = status
        self._attr_icon = "mdi:lightbulb-group" if status == "controlled" else "mdi:lightbulb-alert-outline"
        self._attr_unique_id = f"{instance.subentry_id}_{status}"
        self.entity_id = f"sensor.{instance.prefix}adaptive_{status}"
        self._attr_name = status.title()
        self._attr_device_info = instance.device_info

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_WRITE_TRACKING_UPDATED, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    def _matching_lights(self) -> tuple[list[str], int]:
        lights: list[str] = []
        records = self._registry.records_for_scope(self._instance.subentry_id)
        for entity_id, record in records.items():
            status, _owner, _via, _ctx = _classify_tracked(self.hass, entity_id, record)
            if status == self._status:
                lights.append(entity_id)
        return sorted(lights), len(records)

    @property
    def native_value(self) -> int:
        return len(self._matching_lights()[0])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        lights, total = self._matching_lights()
        return {"lights": lights, "total_tracked": total}


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

