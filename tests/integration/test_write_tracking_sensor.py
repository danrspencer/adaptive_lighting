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


async def test_status_confirmed_when_live_context_matches_confirmed_claim(
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
    assert record["status"] == "confirmed"
    assert record["confirmed"]["context_id"] == "ctx-1"


async def test_status_pending_when_live_context_matches_pending_only(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    await write_tracker.async_record(["light.a"], {"light.a": None}, "ctx-1", "automation.room")
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], context=Context(id="ctx-1"))

    record = sensor_entity.extra_state_attributes["entities"]["light.a"]
    assert record["status"] == "pending"
    assert record["live_context_id"] == "ctx-1"


async def test_status_mismatched_when_live_context_matches_neither_claim(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    _set_light(hass, "light.a", "off", supported_color_modes=["color_temp"])
    await write_tracker.async_record(["light.a"], {"light.a": None}, "ctx-1", "automation.room")
    _set_light(hass, "light.a", "on", supported_color_modes=["color_temp"], context=Context(id="ctx-1"))
    await write_tracker.async_record(["light.a"], {"light.a": "ctx-1"}, "ctx-2", "automation.room")
    # An external change - a fresh context matching neither confirmed
    # (ctx-1) nor pending (ctx-2). Brightness deliberately differs from
    # the prior _set_light calls: HA only replaces an entity's context
    # on a genuine state change, not a same-state "state_reported"
    # re-set - confirmed live and already documented in this repo's own
    # test_force_bypasses_protection_and_reclaims_ownership.
    _set_light(
        hass, "light.a", "on", supported_color_modes=["color_temp"], brightness=42, context=Context(id="ctx-3")
    )

    record = sensor_entity.extra_state_attributes["entities"]["light.a"]
    assert record["status"] == "mismatched"


async def test_status_unavailable_when_the_entity_has_no_live_state(
    hass: HomeAssistant, write_tracker: LastWriteTracker, sensor_entity: _WriteTrackingSensor
):
    await write_tracker.async_record(["light.a"], {"light.a": None}, "ctx-1", "automation.room")
    hass.states.async_remove("light.a")

    record = sensor_entity.extra_state_attributes["entities"]["light.a"]
    assert record["status"] == "unavailable"
    assert record["live_context_id"] is None


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
