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

from datetime import timedelta

import pytest
from freezegun import freeze_time
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import Context, HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.adaptive_lighting_helpers.override_protection import PENDING_GRACE_SECONDS
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


async def test_status_controlled_when_live_context_matches_confirmed_claim(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    # First write: promotes the pre-write context to `confirmed` on the
    # *next* record call that observes it landing.
    await write_tracker.async_record(["light.a"], {"light.a": None}, "ctx-1", "automation.room")
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], context=Context(id="ctx-1"))
    await write_tracker.async_record(["light.a"], {"light.a": "ctx-1"}, "ctx-2", "automation.room")
    # Live context still "ctx-1" (never reflected the second write) -
    # matches the now-confirmed first write.

    record = sensor_entity.extra_state_attributes["entities"]["light.a"]
    assert record["status"] == "controlled"
    assert record["confirmed"]["context_id"] == "ctx-1"
    assert record["matched_via"] == "context"


async def test_status_pending_when_live_context_matches_pending_only(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    await write_tracker.async_record(["light.a"], {"light.a": None}, "ctx-1", "automation.room")
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], context=Context(id="ctx-1"))

    record = sensor_entity.extra_state_attributes["entities"]["light.a"]
    assert record["status"] == "pending"
    assert record["live_context_id"] == "ctx-1"
    assert record["matched_via"] == "context"


async def test_status_overridden_when_live_context_and_value_both_mismatch(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    with freeze_time(dt_util.utcnow()) as frozen:
        _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
        await write_tracker.async_record(["light.a"], {"light.a": None}, "ctx-1", "automation.room")
        _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], context=Context(id="ctx-1"))
        await write_tracker.async_record(
            ["light.a"],
            {"light.a": "ctx-1"},
            "ctx-2",
            "automation.room",
            targets={"light.a": {"brightness": 200, "color_temp_kelvin": 3000}},
        )
        # An external change - a fresh context matching neither confirmed
        # (ctx-1) nor pending (ctx-2), and a value nowhere near pending's own
        # recorded target either, so the value-match rescue doesn't apply.
        # Brightness deliberately differs from the prior _set_light calls: HA
        # only replaces an entity's context on a genuine state change, not a
        # same-state "state_reported" re-set - confirmed live and already
        # documented in this repo's own
        # test_force_bypasses_protection_and_reclaims_ownership.
        _set_light(
            hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=42, context=Context(id="ctx-3")
        )
        # Past the pending-grace-period window - see PENDING_GRACE_SECONDS -
        # otherwise this mismatch reads as "still in flight", not overridden.
        frozen.move_to(dt_util.utcnow() + timedelta(seconds=PENDING_GRACE_SECONDS + 1))

        record = sensor_entity.extra_state_attributes["entities"]["light.a"]
        assert record["status"] == "overridden"


async def test_owner_id_surfaces_whichever_claim_currently_matches(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    """classify()'s second return value - whichever claim's owner
    matched right now - is exposed at the top level of each entity's
    dict, not just buried inside confirmed/pending, so a viewer doesn't
    have to cross-reference the two claims themselves to answer "who
    owns this light right now"."""
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    await write_tracker.async_record(["light.a"], {"light.a": None}, "ctx-1", "automation.room_a")
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], context=Context(id="ctx-1"))

    record = sensor_entity.extra_state_attributes["entities"]["light.a"]
    assert record["status"] == "pending"
    assert record["owner_id"] == "automation.room_a"


async def test_owner_id_is_none_when_nothing_currently_matches(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    with freeze_time(dt_util.utcnow()) as frozen:
        _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
        await write_tracker.async_record(["light.a"], {"light.a": None}, "ctx-1", "automation.room_a")
        _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], context=Context(id="ctx-1"))
        await write_tracker.async_record(
            ["light.a"],
            {"light.a": "ctx-1"},
            "ctx-2",
            "automation.room_a",
            targets={"light.a": {"brightness": 200, "color_temp_kelvin": 3000}},
        )
        _set_light(
            hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=42, context=Context(id="ctx-3")
        )
        # Past the pending-grace-period window - see PENDING_GRACE_SECONDS.
        frozen.move_to(dt_util.utcnow() + timedelta(seconds=PENDING_GRACE_SECONDS + 1))

        record = sensor_entity.extra_state_attributes["entities"]["light.a"]
        assert record["status"] == "overridden"
        assert record["owner_id"] is None


async def test_status_off_when_the_light_is_legitimately_off(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    """Mirrors override_protection.classify()'s own precedence - its
    very first check is is_on, before any context/value comparison, so
    a light turned off by a code path outside apply_lighting (e.g. the
    blueprint's own motion_off action, whose context write_tracking.py
    never records) must never fall through to "overridden" just
    because that context matches neither claim. Found live, 2026-08-22:
    a whole room's worth of lights, switched off together under one
    shared motion_off context, all showed "overridden" while genuinely,
    correctly off."""
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    await write_tracker.async_record(["light.a"], {"light.a": None}, "ctx-1", "automation.room")
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], context=Context(id="ctx-1"))
    await write_tracker.async_record(
        ["light.a"],
        {"light.a": "ctx-1"},
        "ctx-2",
        "automation.room",
        targets={"light.a": {"brightness": 200, "color_temp_kelvin": 3000}},
    )
    # A separate code path (not apply_lighting/write_tracker) turns the
    # light off under its own, never-recorded context - exactly what
    # the blueprint's motion_off action does.
    _set_light(hass, "light.a", "off", context=Context(id="ctx-motion-off"))

    record = sensor_entity.extra_state_attributes["entities"]["light.a"]
    assert record["status"] == "off"


async def test_status_off_takes_precedence_over_a_stale_context_match(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    """Branch order, not just presence: a light going off under a
    context.id that happens to equal a stale claim's own context_id
    must still read "off", not "pending"/"controlled"."""
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    await write_tracker.async_record(["light.a"], {"light.a": None}, "ctx-1", "automation.room")
    # Off again, deliberately reusing ctx-1 as the off-transition's own
    # context id.
    _set_light(hass, "light.a", "off", context=Context(id="ctx-1"))

    record = sensor_entity.extra_state_attributes["entities"]["light.a"]
    assert record["status"] == "off"


async def test_status_controlled_when_context_mismatches_but_value_matches_pendings_target(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    """The sensor-level twin of grouping.py's own rescue - see its
    externally_set() docstring for the live incident (light.kitchen_3/
    light.kitchen_5) this closes. Without it this light would show as
    "overridden" despite being genuinely, correctly controlled."""
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    await write_tracker.async_record(["light.a"], {"light.a": None}, "ctx-1", "automation.room")
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], context=Context(id="ctx-1"))
    await write_tracker.async_record(
        ["light.a"],
        {"light.a": "ctx-1"},
        "ctx-2",
        "automation.room",
        targets={"light.a": {"brightness": 200, "color_temp_kelvin": 3000}},
    )
    # The device's own delayed echo: a fresh, untracked context, but
    # reporting exactly what pending asked for.
    _set_light(
        hass,
        "light.a",
        "on",
        supported_color_modes=["color_temp"],
        brightness=200,
        color_temp_kelvin=3000,
        context=Context(id="ctx-device-echo"),
    )

    record = sensor_entity.extra_state_attributes["entities"]["light.a"]
    assert record["status"] == "controlled"
    assert record["matched_via"] == "value"


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
    assert record["pending"]["recorded_at"] is not None
    # A real, parseable ISO 8601 timestamp - not just any truthy value.
    from homeassistant.util import dt as dt_util

    assert dt_util.parse_datetime(record["pending"]["recorded_at"]) is not None


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
    assert record["confirmed"] is not None
    assert record["confirmed"]["recorded_at"] is None


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
    assert stale["status"] != "pending", "test premise broken: expected the pre-poll snapshot to be stale"

    await sensor_entity.async_update_ha_state(force_refresh=True)

    fresh = hass.states.get("sensor.adaptive_lighting_write_tracking").attributes["entities"]["light.a"]
    assert fresh["status"] == "pending"
