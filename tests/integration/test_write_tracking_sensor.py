"""
Tests for sensor.py's _WriteTrackingSensor - the diagnostic view into
write_tracking.py's confirmed/pending claims (see CLAUDE.md's dated
entry on this feature).

Deliberately constructs the entity directly (hass + entity_id set by
hand, native_value/extra_state_attributes read as plain properties)
rather than through hass.config_entries.async_setup()'s full platform-
forwarding path. adaptive_lighting_helpers declares `frontend`/`http`
as manifest dependencies (needed for the dashboard card's own
async_setup, unrelated to this sensor) - resolving those pulls in the
separate `home-assistant-frontend` PyPI package, which isn't installed
in this test venv, exactly the "large, untouched dependency" this
project's test_services.py already avoids by calling async_setup_entry
directly rather than going through the full config-entry lifecycle (see
that file's own _setup_entry docstring). These tests are about the
sensor's own computation (status classification, live push-updates via
the dispatcher signal), which needs none of that machinery.

Two claims this file deliberately does NOT cover, because they're
specifically about the platform-forwarding/dependency-resolution path
this file avoids: that the entity actually gets auto-registered by
async_setup_entry with zero schedule instances configured, and that it
genuinely ends up with no device in the real entity registry. Both were
confirmed live against the real instance instead (see CLAUDE.md).
"""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import Context, HomeAssistant

from custom_components.adaptive_lighting_helpers.sensor import _WriteTrackingSensor
from custom_components.adaptive_lighting_helpers.write_tracking import LastWriteTracker


def _set_light(hass: HomeAssistant, entity_id: str, state: str, *, context: Context | None = None, **attrs) -> None:
    hass.states.async_set(entity_id, state, attrs, context=context)


@pytest.fixture
async def write_tracker(hass: HomeAssistant) -> LastWriteTracker:
    tracker = LastWriteTracker(hass)
    await tracker.async_load()
    return tracker


@pytest.fixture
def sensor_entity(hass: HomeAssistant, write_tracker: LastWriteTracker) -> _WriteTrackingSensor:
    entry = MockConfigEntry(domain="adaptive_lighting_helpers", data={})
    entry.add_to_hass(hass)
    entity = _WriteTrackingSensor(hass, entry, write_tracker)
    entity.hass = hass
    entity.entity_id = "sensor.adaptive_lighting_write_tracking"
    return entity


async def test_native_value_counts_tracked_entities(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    assert sensor_entity.native_value == 0

    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    await write_tracker.async_record(["light.a"], {"light.a": None}, "ctx-1", "automation.room")

    assert sensor_entity.native_value == 1


# The status/owner_id values themselves are override_protection.classify()'s
# output, and every branch of that table is exercised directly in
# tests/test_override_protection.py. What matters here is that the sensor
# hands classify() the live state it needs and surfaces all three of its
# return values - not that the table is right, which is settled elsewhere.
# `unavailable` below is the one status classify() has no equivalent of, so
# it stays a full test of its own.
async def test_the_sensor_surfaces_classifys_three_return_values(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    our_context = Context()
    _set_light(hass, "light.a", "on", brightness=200, color_temp_kelvin=3000, context=our_context)
    await write_tracker.async_record(["light.a"], {"light.a": None}, our_context.id, "automation.room")

    record = sensor_entity.extra_state_attributes["entities"]["light.a"]
    assert record["status"] == "controlled"
    assert record["owner_id"] == "automation.room"
    assert record["matched_via"] == "latest-context"
    assert record["live_context_id"] == our_context.id


async def test_the_sensor_reads_live_state_not_just_the_stored_claim(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    # An off light classifies as "off" whatever claim is recorded - only
    # reachable if the sensor passes live state through to classify().
    our_context = Context()
    _set_light(hass, "light.a", "on", brightness=200, color_temp_kelvin=3000, context=our_context)
    await write_tracker.async_record(["light.a"], {"light.a": None}, our_context.id, "automation.room")
    _set_light(hass, "light.a", "off", context=our_context)

    assert sensor_entity.extra_state_attributes["entities"]["light.a"]["status"] == "off"


async def test_status_unavailable_when_the_entity_has_no_live_state(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    await write_tracker.async_record(["light.a"], {"light.a": None}, "ctx-1", "automation.room")
    hass.states.async_remove("light.a")

    record = sensor_entity.extra_state_attributes["entities"]["light.a"]
    assert record["status"] == "unavailable"
    assert record["live_context_id"] is None


async def test_pending_claim_carries_a_recorded_at_timestamp(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    """recorded_at is what lets a dashboard card narrow a logbook lookup
    to resolve a context.id into what actually happened, rather than
    guessing a search window - see write_tracking.py's _ContextClaim."""
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    await write_tracker.async_record(["light.a"], {"light.a": None}, "ctx-1", "automation.room")

    record = sensor_entity.extra_state_attributes["entities"]["light.a"]
    assert record["latest"]["recorded_at"] is not None
    # A real, parseable ISO 8601 timestamp - not just any truthy value.
    from homeassistant.util import dt as dt_util

    assert dt_util.parse_datetime(record["latest"]["recorded_at"]) is not None


async def test_synthetic_first_write_baseline_has_no_recorded_at(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    """The synthetic confirmed baseline (see async_record's docstring)
    is a context we merely observed, not one we know the start time of -
    recorded_at stays None rather than claiming false precision."""
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    pre_write_context = hass.states.get("light.a").context.id
    await write_tracker.async_record(["light.a"], {"light.a": pre_write_context}, "ctx-1", "automation.room")

    record = sensor_entity.extra_state_attributes["entities"]["light.a"]
    assert record["observed"] is not None
    assert record["observed"]["recorded_at"] is None


async def test_updates_push_via_dispatcher_signal_without_waiting_for_a_poll(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    """No sleep, no time advance - async_record must fire
    SIGNAL_WRITE_TRACKING_UPDATED synchronously, which
    async_added_to_hass wires straight to async_write_ha_state(), so a
    real write is reflected instantly rather than waiting for the next
    poll (also enabled - see the entity's own docstring for why both are
    needed)."""
    await sensor_entity.async_added_to_hass()
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])

    await write_tracker.async_record(["light.a"], {"light.a": None}, "ctx-1", "automation.room")
    await hass.async_block_till_done()

    state = hass.states.get("sensor.adaptive_lighting_write_tracking")
    assert state is not None
    assert state.state == "1"


async def test_a_poll_refreshes_status_even_when_write_tracking_itself_has_not_changed(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    """The bug caught live right after this sensor's first deploy: every
    tracked light showed "unavailable" long after a restart, because the
    push signal only fires on a write_tracking.py mutation - a light's
    *live* state can change with no write ever happening (exactly what a
    restart does, briefly, to every reconnecting entity), and nothing
    refreshed the state machine's cached copy once that live state
    genuinely changed. Polling is the fix - this reproduces it by
    changing live state with no write_tracker activity at all, then
    forcing a poll cycle exactly as HA's own scheduler would."""
    # async_update_ha_state(force_refresh=True) below bypasses
    # should_poll entirely by design, so it alone can't prove polling is
    # actually enabled - check the flag directly too.
    assert sensor_entity.should_poll is True

    await sensor_entity.async_added_to_hass()
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    # Pushes once, capturing the light as still "off" at this instant -
    # write_tracker has no way to know the write is about to land.
    await write_tracker.async_record(["light.a"], {"light.a": None}, "ctx-1", "automation.room")
    await hass.async_block_till_done()

    # Reflect the write landing - a live-state-only change, no
    # write_tracker activity, so no push fires. The state machine's
    # cached copy is now stale, still showing the pre-write snapshot.
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], context=Context(id="ctx-1"))
    stale = hass.states.get("sensor.adaptive_lighting_write_tracking").attributes["entities"]["light.a"]
    assert stale["status"] != "controlled", "test premise broken: expected the pre-poll snapshot to be stale"

    await sensor_entity.async_update_ha_state(force_refresh=True)

    fresh = hass.states.get("sensor.adaptive_lighting_write_tracking").attributes["entities"]["light.a"]
    assert fresh["status"] == "controlled"


# --- optional per-owner count sensors (CONF_OWNER_SENSORS) ----------------
#
# Derived entirely from the same records the global sensor exposes, so
# these tests are about the wiring: does the right pair appear, for the
# right owner, counting the right lights.


async def _setup_sensor_platform(hass: HomeAssistant, *, owner_sensors: bool) -> list:
    """Runs sensor.py's async_setup_entry with the option on or off,
    capturing whatever it adds."""
    from custom_components.adaptive_lighting_helpers.const import CONF_OWNER_SENSORS, DOMAIN
    from custom_components.adaptive_lighting_helpers.sensor import async_setup_entry as sensor_setup

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={CONF_OWNER_SENSORS: owner_sensors})
    entry.add_to_hass(hass)
    tracker = LastWriteTracker(hass)
    await tracker.async_load()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = tracker

    added: list = []
    await sensor_setup(hass, entry, lambda entities, **kw: added.extend(entities))
    return added, tracker


async def _record(tracker: LastWriteTracker, entity_id: str, owner: str, context_id: str) -> None:
    """A realistic record: a real pre-write context so the entity gets an
    `observed` baseline. Without one, classify() returns "untracked" (not
    enough evidence yet) for anything that doesn't match `latest`, which
    is not the situation these tests are about."""
    await tracker.async_record([entity_id], {entity_id: f"ctx-before-{entity_id}"}, context_id, owner)


async def test_owner_sensors_are_not_created_by_default(hass: HomeAssistant):
    ctx = Context()
    _set_light(hass, "light.a", "on", brightness=200, color_temp_kelvin=3000, context=ctx)
    added, tracker = await _setup_sensor_platform(hass, owner_sensors=False)
    await _record(tracker, "light.a", "automation.room", ctx.id)

    assert [e for e in added if type(e).__name__ == "_OwnerCountSensor"] == []


async def test_owner_sensors_create_a_pair_per_owner_present_at_setup(hass: HomeAssistant):
    ctx = Context()
    _set_light(hass, "light.a", "on", brightness=200, color_temp_kelvin=3000, context=ctx)
    _set_light(hass, "light.b", "on", brightness=200, color_temp_kelvin=3000, context=ctx)
    seed = LastWriteTracker(hass)
    await seed.async_load()
    await _record(seed, "light.a", "automation.kitchen", ctx.id)
    await _record(seed, "light.b", "automation.hall", ctx.id)

    added, _ = await _setup_sensor_platform(hass, owner_sensors=True)
    owner_sensors = [e for e in added if type(e).__name__ == "_OwnerCountSensor"]

    assert sorted(e.entity_id for e in owner_sensors) == [
        "sensor.hall_adaptive_controlled",
        "sensor.hall_adaptive_overridden",
        "sensor.kitchen_adaptive_controlled",
        "sensor.kitchen_adaptive_overridden",
    ]


async def test_a_new_owner_appearing_later_gets_its_pair_and_an_existing_one_does_not_repeat(hass: HomeAssistant):
    """write_tracking fires SIGNAL_WRITE_TRACKING_UPDATED on every record,
    so a brand-new owner's first write brings its sensors into existence
    with no polling - and a further write by an owner already seen must
    not add a duplicate pair."""
    ctx = Context()
    _set_light(hass, "light.a", "on", brightness=200, color_temp_kelvin=3000, context=ctx)
    added, tracker = await _setup_sensor_platform(hass, owner_sensors=True)
    assert [e for e in added if type(e).__name__ == "_OwnerCountSensor"] == []

    await _record(tracker, "light.a", "automation.kitchen", ctx.id)
    await hass.async_block_till_done()
    assert len([e for e in added if type(e).__name__ == "_OwnerCountSensor"]) == 2

    # Same owner writing again - no new entities.
    _set_light(hass, "light.b", "on", brightness=201, color_temp_kelvin=3000, context=ctx)
    await _record(tracker, "light.b", "automation.kitchen", ctx.id)
    await hass.async_block_till_done()
    assert len([e for e in added if type(e).__name__ == "_OwnerCountSensor"]) == 2


async def test_each_sensor_counts_only_its_own_status_and_its_own_owner(hass: HomeAssistant):
    ours = Context()
    _set_light(hass, "light.mine_ok", "on", brightness=200, color_temp_kelvin=3000, context=ours)
    _set_light(hass, "light.mine_taken", "on", brightness=200, color_temp_kelvin=3000, context=ours)
    _set_light(hass, "light.theirs", "on", brightness=200, color_temp_kelvin=3000, context=ours)
    seed = LastWriteTracker(hass)
    await seed.async_load()
    await _record(seed, "light.mine_ok", "automation.kitchen", ours.id)
    await _record(seed, "light.mine_taken", "automation.kitchen", ours.id)
    await _record(seed, "light.theirs", "automation.hall", ours.id)
    # Something else grabs one of the kitchen's lights, at a value that
    # matches neither claim's target.
    _set_light(hass, "light.mine_taken", "on", brightness=12, color_temp_kelvin=6500, context=Context())

    added, _ = await _setup_sensor_platform(hass, owner_sensors=True)
    by_id = {e.entity_id: e for e in added if type(e).__name__ == "_OwnerCountSensor"}

    assert by_id["sensor.kitchen_adaptive_controlled"].native_value == 1
    assert by_id["sensor.kitchen_adaptive_overridden"].native_value == 1
    assert by_id["sensor.kitchen_adaptive_overridden"].extra_state_attributes["lights"] == ["light.mine_taken"]
    assert by_id["sensor.kitchen_adaptive_overridden"].extra_state_attributes["total_tracked"] == 2
    # The other owner's light is in neither of the kitchen's counts.
    assert by_id["sensor.hall_adaptive_controlled"].native_value == 1


async def test_an_off_light_is_in_neither_count_but_still_in_total_tracked(hass: HomeAssistant):
    """The two counts deliberately don't sum to total_tracked - override
    protection doesn't apply to a light that's off."""
    ctx = Context()
    _set_light(hass, "light.on_one", "on", brightness=200, color_temp_kelvin=3000, context=ctx)
    _set_light(hass, "light.off_one", "on", brightness=200, color_temp_kelvin=3000, context=ctx)
    seed = LastWriteTracker(hass)
    await seed.async_load()
    await _record(seed, "light.on_one", "automation.kitchen", ctx.id)
    await _record(seed, "light.off_one", "automation.kitchen", ctx.id)
    _set_light(hass, "light.off_one", "off", context=ctx)

    added, _ = await _setup_sensor_platform(hass, owner_sensors=True)
    by_id = {e.entity_id: e for e in added if type(e).__name__ == "_OwnerCountSensor"}

    assert by_id["sensor.kitchen_adaptive_controlled"].native_value == 1
    assert by_id["sensor.kitchen_adaptive_overridden"].native_value == 0
    assert by_id["sensor.kitchen_adaptive_controlled"].extra_state_attributes["total_tracked"] == 2


async def test_a_record_with_no_owner_creates_no_sensors(hass: HomeAssistant):
    """Force/anonymous writes and the resync baselines carry no owner -
    they belong to nobody, so there is no sensor for them to land on."""
    ctx = Context()
    _set_light(hass, "light.a", "on", brightness=200, color_temp_kelvin=3000, context=ctx)
    seed = LastWriteTracker(hass)
    await seed.async_load()
    await seed.async_record(["light.a"], {"light.a": None}, ctx.id, None)

    added, _ = await _setup_sensor_platform(hass, owner_sensors=True)
    assert [e for e in added if type(e).__name__ == "_OwnerCountSensor"] == []


async def test_an_anonymous_write_does_not_orphan_a_light_from_its_owner(hass: HomeAssistant):
    """A force/anonymous write records owner_id None, which becomes the
    new `latest`, pushing the owned claim down into `observed`. The light
    should stay with the owner that last identified itself rather than
    dropping off that owner's sensors entirely - hence the fallback from
    latest's owner to observed's."""
    ctx = Context()
    _set_light(hass, "light.a", "on", brightness=200, color_temp_kelvin=3000, context=ctx)
    seed = LastWriteTracker(hass)
    await seed.async_load()
    await _record(seed, "light.a", "automation.kitchen", ctx.id)
    # Now an anonymous write - no owner_id - on top of it.
    later = Context()
    _set_light(hass, "light.a", "on", brightness=201, color_temp_kelvin=3000, context=later)
    await seed.async_record(["light.a"], {"light.a": ctx.id}, later.id, None)

    added, _ = await _setup_sensor_platform(hass, owner_sensors=True)
    by_id = {e.entity_id: e for e in added if type(e).__name__ == "_OwnerCountSensor"}

    assert "sensor.kitchen_adaptive_controlled" in by_id
    assert by_id["sensor.kitchen_adaptive_controlled"].extra_state_attributes["total_tracked"] == 1


# --- EVENT_LIGHT_OVERRIDDEN ----------------------------------------------


def _capture_override_events(hass: HomeAssistant) -> list:
    from custom_components.adaptive_lighting_helpers.const import EVENT_LIGHT_OVERRIDDEN

    events: list = []
    hass.bus.async_listen(EVENT_LIGHT_OVERRIDDEN, events.append)
    return events


async def _take_light_by_hand(hass: HomeAssistant) -> None:
    """Something else sets the light, at values matching neither claim."""
    _set_light(hass, "light.a", "on", brightness=12, color_temp_kelvin=6500, context=Context())


async def test_the_first_pass_seeds_without_announcing_existing_overrides(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    """A restart must not re-announce every light that was already
    overridden before it - the event marks a light changing hands, and
    nothing changed hands here."""
    events = _capture_override_events(hass)
    ctx = Context()
    _set_light(hass, "light.a", "on", brightness=200, color_temp_kelvin=3000, context=ctx)
    await _record(write_tracker, "light.a", "automation.room", ctx.id)
    await _take_light_by_hand(hass)

    sensor_entity._refresh_statuses()
    await hass.async_block_till_done()

    assert sensor_entity.extra_state_attributes["entities"]["light.a"]["status"] == "overridden"
    assert events == []


async def test_a_light_changing_hands_fires_once_with_a_full_snapshot(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    events = _capture_override_events(hass)
    ctx = Context()
    _set_light(hass, "light.a", "on", brightness=200, color_temp_kelvin=3000, context=ctx)
    await write_tracker.async_record(
        ["light.a"],
        {"light.a": "ctx-before"},
        ctx.id,
        "automation.room",
        targets={"light.a": {"brightness": 200, "color_temp_kelvin": 3000}},
    )
    sensor_entity._refresh_statuses()  # seeds, still controlled

    await _take_light_by_hand(hass)
    sensor_entity._refresh_statuses()
    await hass.async_block_till_done()

    assert len(events) == 1
    data = events[0].data
    assert data["entity_id"] == "light.a"
    assert data["owner_id"] == "automation.room"
    assert data["previous_status"] == "controlled"
    # The live values at the moment it changed hands, and what we had last
    # asked for - the comparison you can't reconstruct afterwards.
    assert data["live"]["brightness"] == 12
    assert data["live"]["color_temp_kelvin"] == 6500
    assert data["latest"]["target"] == {"brightness": 200, "color_temp_kelvin": 3000}
    assert data["observed"] is not None

    # Still overridden on the next refresh - no repeat.
    sensor_entity._refresh_statuses()
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_it_fires_again_if_the_light_comes_back_and_is_taken_again(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    """Edge-triggered means every hand-over is recorded, not just the
    first - a light that flaps in and out is exactly the case worth
    seeing in the timeline."""
    events = _capture_override_events(hass)
    ctx = Context()
    _set_light(hass, "light.a", "on", brightness=200, color_temp_kelvin=3000, context=ctx)
    await _record(write_tracker, "light.a", "automation.room", ctx.id)
    sensor_entity._refresh_statuses()

    await _take_light_by_hand(hass)
    sensor_entity._refresh_statuses()
    await hass.async_block_till_done()
    assert len(events) == 1

    # Reclaimed by a fresh write of our own, then taken again.
    reclaim = Context()
    _set_light(hass, "light.a", "on", brightness=201, color_temp_kelvin=3000, context=reclaim)
    await _record(write_tracker, "light.a", "automation.room", reclaim.id)
    sensor_entity._refresh_statuses()
    assert sensor_entity.extra_state_attributes["entities"]["light.a"]["status"] == "controlled"

    await _take_light_by_hand(hass)
    sensor_entity._refresh_statuses()
    await hass.async_block_till_done()
    assert len(events) == 2


# --- the per-owner Clear button -------------------------------------------
#
# Shares setup_owner_entities() with the counter sensors above, so the
# opt-in, dynamic-add and disable-cleanup behaviour is already covered
# there. What's left is the button's own: does it appear per owner, and
# does a press clear exactly that owner's records.


async def _setup_button_platform(hass: HomeAssistant, *, owner_sensors: bool) -> list:
    from custom_components.adaptive_lighting_helpers.button import async_setup_entry as button_setup
    from custom_components.adaptive_lighting_helpers.const import CONF_OWNER_SENSORS, DOMAIN

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={CONF_OWNER_SENSORS: owner_sensors})
    entry.add_to_hass(hass)
    tracker = LastWriteTracker(hass)
    await tracker.async_load()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = tracker

    added: list = []
    await button_setup(hass, entry, lambda entities, **kw: added.extend(entities))
    return added, tracker


async def test_a_clear_button_is_created_per_owner_only_when_enabled(hass: HomeAssistant):
    ctx = Context()
    _set_light(hass, "light.a", "on", brightness=200, color_temp_kelvin=3000, context=ctx)
    _set_light(hass, "light.b", "on", brightness=200, color_temp_kelvin=3000, context=ctx)
    seed = LastWriteTracker(hass)
    await seed.async_load()
    await _record(seed, "light.a", "automation.kitchen", ctx.id)
    await _record(seed, "light.b", "automation.hall", ctx.id)

    off, _ = await _setup_button_platform(hass, owner_sensors=False)
    assert off == []

    on, _ = await _setup_button_platform(hass, owner_sensors=True)
    assert sorted(e.entity_id for e in on) == [
        "button.hall_adaptive_clear",
        "button.kitchen_adaptive_clear",
    ]
    kitchen = next(e for e in on if e.entity_id == "button.kitchen_adaptive_clear")
    assert kitchen.extra_state_attributes == {"owner_id": "automation.kitchen", "tracked": 1}


async def test_pressing_clears_that_owners_records_and_leaves_other_owners_alone(hass: HomeAssistant):
    """The whole point of the button - and the reason it clears by owner
    rather than wholesale. A press is a reset for one room, not for the
    house."""
    ctx = Context()
    for entity_id in ("light.a", "light.b", "light.c"):
        _set_light(hass, entity_id, "on", brightness=200, color_temp_kelvin=3000, context=ctx)
    seed = LastWriteTracker(hass)
    await seed.async_load()
    await _record(seed, "light.a", "automation.kitchen", ctx.id)
    await _record(seed, "light.b", "automation.kitchen", ctx.id)
    await _record(seed, "light.c", "automation.hall", ctx.id)

    added, tracker = await _setup_button_platform(hass, owner_sensors=True)
    kitchen = next(e for e in added if e.entity_id == "button.kitchen_adaptive_clear")
    assert kitchen.extra_state_attributes["tracked"] == 2

    await kitchen.async_press()

    assert sorted(tracker.snapshot()) == ["light.c"]
    assert kitchen.extra_state_attributes["tracked"] == 0


# --- the owner device -----------------------------------------------------
#
# Three entities per owner is three things to delete when an owner_id
# dies. A device makes that one delete that takes its entities with it,
# and gives the three one place to be renamed and one area.


def _owner_device(hass: HomeAssistant, owner_id: str):
    from homeassistant.helpers import device_registry as dr

    from custom_components.adaptive_lighting_helpers.const import DOMAIN

    return dr.async_get(hass).async_get_device(identifiers={(DOMAIN, f"owner_{owner_id}")})


async def _seed(hass: HomeAssistant, *pairs: tuple[str, str]) -> None:
    ctx = Context()
    seed = LastWriteTracker(hass)
    await seed.async_load()
    for entity_id, owner in pairs:
        _set_light(hass, entity_id, "on", brightness=200, color_temp_kelvin=3000, context=ctx)
        await _record(seed, entity_id, owner, ctx.id)


async def test_all_of_an_owners_entities_share_one_device_and_owners_get_their_own(hass: HomeAssistant):
    await _seed(hass, ("light.a", "automation.kitchen"), ("light.b", "automation.hall"))

    sensors, _ = await _setup_sensor_platform(hass, owner_sensors=True)
    buttons, _ = await _setup_button_platform(hass, owner_sensors=True)

    kitchen = _owner_device(hass, "automation.kitchen")
    hall = _owner_device(hass, "automation.hall")
    assert kitchen is not None and hall is not None
    assert kitchen.id != hall.id
    assert kitchen.name == "Kitchen"

    kitchen_entities = [
        e
        for e in sensors + buttons
        if type(e).__name__ in ("_OwnerCountSensor", "_OwnerClearButton")
        and e.device_info["identifiers"] == kitchen.identifiers
    ]
    assert len(kitchen_entities) == 3
    # Short names, composed by HA against the device name.
    assert sorted(e.name for e in kitchen_entities) == ["Clear", "Controlled", "Overridden"]
    # ...while entity_ids stay exactly as they were, so nothing churns.
    assert sorted(e.entity_id for e in kitchen_entities) == [
        "button.kitchen_adaptive_clear",
        "sensor.kitchen_adaptive_controlled",
        "sensor.kitchen_adaptive_overridden",
    ]


async def test_the_device_lands_in_the_area_of_the_entity_the_owner_names(hass: HomeAssistant):
    """An owner_id is in practice the calling automation's entity_id, and
    that automation is usually already assigned to the room it looks
    after - so following it sorts the device into that room."""
    from homeassistant.helpers import area_registry as ar, entity_registry as er

    kitchen = ar.async_get(hass).async_get_or_create("Kitchen")
    automation = er.async_get(hass).async_get_or_create("automation", "automation", "kitchen-lights-uid")
    er.async_get(hass).async_update_entity(automation.entity_id, area_id=kitchen.id)

    await _seed(hass, ("light.a", automation.entity_id))
    await _setup_button_platform(hass, owner_sensors=True)

    assert _owner_device(hass, automation.entity_id).area_id == kitchen.id


async def test_an_owner_that_names_nothing_resolvable_leaves_the_device_unassigned(hass: HomeAssistant):
    """owner_id is an arbitrary caller-supplied string. When it isn't an
    entity we can resolve an area for, the device stays unassigned rather
    than being guessed at."""
    await _seed(hass, ("light.a", "some-hand-rolled-owner"))
    await _setup_button_platform(hass, owner_sensors=True)

    assert _owner_device(hass, "some-hand-rolled-owner").area_id is None


async def test_an_area_set_by_hand_is_not_overwritten(hass: HomeAssistant):
    """Moving a device to a different room has to stick - the derived
    area only ever fills in a blank."""
    from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

    area_reg = ar.async_get(hass)
    kitchen = area_reg.async_get_or_create("Kitchen")
    utility = area_reg.async_get_or_create("Utility")
    automation = er.async_get(hass).async_get_or_create("automation", "automation", "kitchen-lights-uid")
    er.async_get(hass).async_update_entity(automation.entity_id, area_id=kitchen.id)

    await _seed(hass, ("light.a", automation.entity_id))
    await _setup_button_platform(hass, owner_sensors=True)
    device = _owner_device(hass, automation.entity_id)
    dr.async_get(hass).async_update_device(device.id, area_id=utility.id)

    # A second setup - i.e. the next restart - must leave that alone.
    await _setup_button_platform(hass, owner_sensors=True)

    assert _owner_device(hass, automation.entity_id).area_id == utility.id


async def test_turning_the_option_off_removes_the_owner_devices_and_their_entities(hass: HomeAssistant):
    """Toggling off should actually mean off. Removing the device is what
    takes all three entities with it."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    from custom_components.adaptive_lighting_helpers import _async_remove_owner_devices_if_disabled
    from custom_components.adaptive_lighting_helpers.const import CONF_OWNER_SENSORS, DOMAIN

    await _seed(hass, ("light.a", "automation.kitchen"))
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={CONF_OWNER_SENSORS: True})
    entry.add_to_hass(hass)
    tracker = LastWriteTracker(hass)
    await tracker.async_load()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = tracker

    from custom_components.adaptive_lighting_helpers.button import async_setup_entry as button_setup

    await button_setup(hass, entry, lambda entities, **kw: None)
    device = _owner_device(hass, "automation.kitchen")
    assert device is not None
    # config_entry matters: HA only sweeps a removed device's entities
    # when their config_entry_id is one of the device's own.
    child = er.async_get(hass).async_get_or_create(
        "button",
        DOMAIN,
        f"{entry.entry_id}_owner_automation.kitchen_clear",
        device_id=device.id,
        config_entry=entry,
    )
    # A schedule-instance device on the same entry is not ours to remove.
    schedule = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "some-subentry-id")}, name="Ground Floor"
    )

    hass.config_entries.async_update_entry(entry, options={CONF_OWNER_SENSORS: False})
    _async_remove_owner_devices_if_disabled(hass, entry)
    await hass.async_block_till_done()

    assert _owner_device(hass, "automation.kitchen") is None
    assert er.async_get(hass).async_get(child.entity_id) is None
    assert dr.async_get(hass).async_get(schedule.id) is not None


async def test_deleting_an_owner_device_is_allowed_and_clears_that_owners_records(hass: HomeAssistant):
    """The point of the device: cleaning up a dead owner_id is one
    delete. Clearing its records as part of that is what makes the delete
    stick - otherwise the very records the device is derived from rebuild
    it on the next write."""
    from custom_components.adaptive_lighting_helpers import async_remove_config_entry_device
    from custom_components.adaptive_lighting_helpers.const import CONF_OWNER_SENSORS, DOMAIN

    await _seed(hass, ("light.a", "automation.kitchen"), ("light.b", "automation.hall"))
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={CONF_OWNER_SENSORS: True})
    entry.add_to_hass(hass)
    tracker = LastWriteTracker(hass)
    await tracker.async_load()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = tracker

    from custom_components.adaptive_lighting_helpers.button import async_setup_entry as button_setup

    await button_setup(hass, entry, lambda entities, **kw: None)

    assert await async_remove_config_entry_device(hass, entry, _owner_device(hass, "automation.kitchen")) is True
    assert sorted(tracker.snapshot()) == ["light.b"]


async def test_a_schedule_device_is_refused(hass: HomeAssistant):
    """Defining the delete hook at all turns HA's Delete button on for
    every device on the entry. A schedule instance's device is removed by
    deleting its sensor, not here."""
    from homeassistant.helpers import device_registry as dr

    from custom_components.adaptive_lighting_helpers import async_remove_config_entry_device
    from custom_components.adaptive_lighting_helpers.const import DOMAIN

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    schedule = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "some-subentry-id")}, name="Ground Floor"
    )

    assert await async_remove_config_entry_device(hass, entry, schedule) is False
