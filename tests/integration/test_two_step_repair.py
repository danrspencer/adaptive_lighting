"""
End-to-end tests for the missing-two-step-label repair: real entity,
device, label and issue registries via
pytest-homeassistant-custom-component.

tests/test_two_step.py already covers the matching logic in isolation.
What can only be tested here is the wiring - that a real TRADFRI-shaped
device in the registry actually raises the issue, that the fix flow
actually writes a label, and that the issue then clears. Every one of
those is registry access that the pure module deliberately doesn't do.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.util import dt as dt_util

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import label_registry as lr

from custom_components.adaptive_lighting_helpers import async_setup_entry
from custom_components.adaptive_lighting_helpers.const import CONF_TWO_STEP_MODELS
from custom_components.adaptive_lighting_helpers.repairs import async_create_fix_flow
from custom_components.adaptive_lighting_helpers.two_step import TWO_STEP_LABEL_ID
from custom_components.adaptive_lighting_helpers.two_step_check import ISSUE_ID

DOMAIN = "adaptive_lighting_helpers"

TRADFRI_MODEL = "TRADFRI bulb GU10, color/white spectrum, 345 lm"


def _add_light(
    hass: HomeAssistant,
    bulb_entry: MockConfigEntry,
    unique_id: str,
    *,
    manufacturer: str = "IKEA",
    model: str = TRADFRI_MODEL,
) -> tuple[str, str]:
    """Registers a light entity on a device with a real manufacturer/model."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=bulb_entry.entry_id,
        identifiers={(DOMAIN, unique_id)},
        manufacturer=manufacturer,
        model=model,
        name=unique_id.replace("_", " ").title(),
    )
    entity = er.async_get(hass).async_get_or_create(
        "light", "test", unique_id, device_id=device.id, suggested_object_id=unique_id
    )
    return entity.entity_id, device.id


@pytest.fixture
async def bulb_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain="test", data={})
    entry.add_to_hass(hass)
    return entry


async def _flush_debounce(hass: HomeAssistant) -> None:
    """Let the registry-change debouncer fire.

    Also stops the pending timer outliving the test, which the harness
    fails on - the same leak async_start_watching's unsub now closes for
    a real reload."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()


async def _setup(hass: HomeAssistant, **options) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options)
    entry.add_to_hass(hass)
    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()
    return entry


async def test_unlabelled_tradfri_bulb_raises_a_fixable_repair(hass, bulb_entry):
    _add_light(hass, bulb_entry, "spot_1")
    await _setup(hass)

    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID)
    assert issue is not None
    assert issue.is_fixable is True
    assert issue.translation_placeholders["count"] == "1"
    assert "light.spot_1" in issue.translation_placeholders["lights"]


async def test_an_already_labelled_bulb_raises_nothing(hass, bulb_entry):
    entity_id, _ = _add_light(hass, bulb_entry, "spot_1")
    er.async_get(hass).async_update_entity(entity_id, labels={TWO_STEP_LABEL_ID})
    await _setup(hass)

    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID) is None


async def test_a_label_on_the_device_counts_the_same_as_on_the_entity(hass, bulb_entry):
    """grouping.py's EntityLookup.tags() accepts either, so the check
    must too - otherwise the repair would nag about bulbs that are in
    fact already routed correctly."""
    _, device_id = _add_light(hass, bulb_entry, "spot_1")
    dr.async_get(hass).async_update_device(device_id, labels={TWO_STEP_LABEL_ID})
    await _setup(hass)

    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID) is None


async def test_a_non_matching_bulb_raises_nothing(hass, bulb_entry):
    _add_light(hass, bulb_entry, "hue_1", manufacturer="Signify", model="Hue color spot")
    await _setup(hass)

    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID) is None


async def test_a_configured_pattern_detects_a_bulb_the_defaults_do_not_know(hass, bulb_entry):
    """Straight off the options flow - a bulb the shipped list has never
    heard of."""
    _add_light(hass, bulb_entry, "odd_1", manufacturer="Acme", model="Weird Bulb 9000")

    await _setup(hass, **{CONF_TWO_STEP_MODELS: "*weird bulb*"})

    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID)
    assert issue is not None
    assert "light.odd_1" in issue.translation_placeholders["lights"]


async def test_a_saved_list_replaces_the_defaults_so_a_removed_pattern_stays_removed(hass, bulb_entry):
    """The whole reason the field is pre-populated rather than additive:
    deleting a shipped pattern has to actually take effect, or the box
    would be lying about what it controls."""
    _add_light(hass, bulb_entry, "spot_1")

    await _setup(hass, **{CONF_TWO_STEP_MODELS: "*weird bulb*"})

    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID) is None


async def test_an_empty_list_falls_back_to_the_defaults(hass, bulb_entry):
    """Clearing the box by accident mustn't silently switch detection
    off - ignoring the repair is the way to stop being told."""
    _add_light(hass, bulb_entry, "spot_1")

    await _setup(hass, **{CONF_TWO_STEP_MODELS: "   \n  "})

    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID) is not None


async def test_fix_flow_applies_the_label_to_the_device_and_clears_the_issue(hass, bulb_entry):
    """The whole point of making it fixable rather than advisory."""
    entity_id, device_id = _add_light(hass, bulb_entry, "spot_1")
    entry = await _setup(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID) is not None

    flow = await async_create_fix_flow(hass, ISSUE_ID, None)
    flow.hass = hass
    await flow.async_step_init()
    result = await flow.async_step_confirm({})

    assert result["type"] == "create_entry"
    assert result["data"]["labelled"] == [entity_id]

    # Applied to the device, not the entity - it survives entity renames
    # and covers every light entity the device exposes.
    assert TWO_STEP_LABEL_ID in dr.async_get(hass).async_get(device_id).labels

    # And the label itself now exists properly, rather than being a
    # dangling id nothing in the UI would show.
    label = lr.async_get(hass).async_get_label(TWO_STEP_LABEL_ID)
    assert label is not None and label.name == "No Combined Transition"

    # The issue clears on its own: writing the label is a device-registry
    # update, which is exactly what the watcher is subscribed to. Nothing
    # re-checks by hand here - that it self-heals is the point.
    await _flush_debounce(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID) is None


async def test_fix_flow_leaves_an_unrelated_bulb_alone(hass, bulb_entry):
    """Only matching bulbs get labelled - a Hue spot in the same house
    must not be swept up by pressing Fix."""
    _, tradfri_device = _add_light(hass, bulb_entry, "spot_1")
    _, hue_device = _add_light(hass, bulb_entry, "hue_1", manufacturer="Signify", model="Hue color spot")
    await _setup(hass)

    flow = await async_create_fix_flow(hass, ISSUE_ID, None)
    flow.hass = hass
    await flow.async_step_init()
    await flow.async_step_confirm({})

    assert TWO_STEP_LABEL_ID in dr.async_get(hass).async_get(tradfri_device).labels
    assert TWO_STEP_LABEL_ID not in dr.async_get(hass).async_get(hue_device).labels
    await _flush_debounce(hass)


async def test_a_disabled_light_is_not_flagged(hass, bulb_entry):
    """A disabled entity can't be driven by apply_lighting at all, so
    labelling it would be pure noise."""
    entity_id, _ = _add_light(hass, bulb_entry, "spot_1")
    er.async_get(hass).async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    await _setup(hass)

    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID) is None
