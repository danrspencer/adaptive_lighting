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

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
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

    for instance in schedule_instances(entry):
        coordinator: ScheduleCoordinator = hass.data[DOMAIN][instance.subentry_id]
        entities = [_AdaptiveLightingSensor(coordinator, instance)]
        async_add_entities(entities, config_subentry_id=instance.subentry_id)


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
    """Diagnostic view into write_tracking.py's confirmed/pending
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

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_WRITE_TRACKING_UPDATED, self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return len(self._write_tracker.snapshot())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        entities: dict[str, Any] = {}
        for entity_id, record in self._write_tracker.snapshot().items():
            state = self.hass.states.get(entity_id)
            live_context_id = state.context.id if state is not None else None
            confirmed = record.get("confirmed")
            pending = record.get("pending")
            claim_owner = None
            if state is None or state.state in ("unavailable", "unknown"):
                # No live state to classify at all - override_protection.classify()
                # has no equivalent of this case (it only ever sees a real
                # is_on/off), so it stays this sensor's own first check.
                status = "unavailable"
            else:
                # Delegates to the exact same classifier grouping.py's
                # externally_set() uses, rather than a second,
                # separately-maintained copy of this comparison - that
                # drift is exactly what let a merely-off light (never
                # actually protected - see classify()'s own "off" case)
                # fall through to "overridden" here despite
                # externally_set() correctly saying "not excluded" for
                # the same light. See override_protection.py's module
                # docstring.
                raw_status, claim_owner = classify(
                    state.state == "on",
                    confirmed,
                    pending,
                    live_context_id,
                    state.attributes.get("brightness"),
                    state.attributes.get("color_temp_kelvin"),
                    state.attributes.get("rgb_color"),
                )
                # "unclaimed" (no confirmed/pending at all, or only an
                # unconfirmed first attempt) displays the same as
                # "controlled" - both mean "nothing to worry about, this
                # entity isn't excluded from the next tick" from a
                # diagnostic viewer's perspective; the distinction only
                # matters to externally_set()'s own owner-conflict logic.
                status = "controlled" if raw_status == "unclaimed" else raw_status
            entities[entity_id] = {
                "status": status,
                # Whichever claim actually matched right now (None for
                # off/unavailable/unclaimed/overridden, where there's
                # nothing to attribute) - classify()'s own second return
                # value, surfaced directly so a viewer doesn't have to
                # cross-reference confirmed/pending themselves to answer
                # "who owns this light right now".
                "owner_id": claim_owner,
                "live_context_id": live_context_id,
                "confirmed": confirmed,
                "pending": pending,
            }
        return {"entities": entities}
