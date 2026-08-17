"""
Home Assistant glue for two_step.py: reads the entity/device/label
registries, and raises or clears a fixable repair when a known two-step
bulb is missing its `no_combined_transition` label.

Split out of __init__.py rather than added to it because this is a
self-contained concern with its own registry access and its own event
subscriptions - __init__.py is already the service-registration adapter
and doesn't need to also know how labels are discovered.

The check re-runs on entity- and device-registry changes (a new bulb
paired, a label added or removed) rather than on a timer: those are
exactly the events that can change the answer, and both are low-volume.
It's debounced because pairing one device emits a burst of registry
writes, and re-deriving the whole answer per write would be wasteful for
something whose result only matters to a repair card.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers.event import async_call_later

from .const import CONF_EXTRA_TWO_STEP_MODELS, DOMAIN
from .two_step import (
    DEFAULT_TWO_STEP_MODEL_PATTERNS,
    TWO_STEP_LABEL_DESCRIPTION,
    TWO_STEP_LABEL_ICON,
    TWO_STEP_LABEL_ID,
    TWO_STEP_LABEL_NAME,
    CandidateLight,
    describe,
    find_unlabelled_two_step_lights,
    parse_extra_patterns,
)

ISSUE_ID = "missing_two_step_label"
_DEBOUNCE_SECONDS = 5.0


def configured_patterns(entry: ConfigEntry) -> list[str]:
    """Shipped defaults plus whatever this install added on top.

    Additive by design - the options field can only ever widen the set,
    never disable a shipped pattern. A user who disagrees with a default
    can ignore the repair itself (Home Assistant persists that), which
    is a clearer escape hatch than a subtractive config field nobody
    would think to look at.
    """
    extra = parse_extra_patterns(entry.options.get(CONF_EXTRA_TWO_STEP_MODELS))
    return [*DEFAULT_TWO_STEP_MODEL_PATTERNS, *extra]


def collect_candidates(hass: HomeAssistant) -> list[CandidateLight]:
    """Every light entity in the registry, paired with its device identity.

    Disabled entities are skipped - they can't be driven by
    apply_lighting at all, so labelling them would be noise. Entities
    with no device are skipped too: there's no manufacturer/model to
    match on, so they can never be identified as a known two-step bulb
    (a hand-written template light, for instance).
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    candidates: list[CandidateLight] = []
    for entry in ent_reg.entities.values():
        if entry.domain != "light" or entry.disabled_by is not None or entry.device_id is None:
            continue
        device = dev_reg.async_get(entry.device_id)
        if device is None:
            continue
        candidates.append(
            CandidateLight(
                entity_id=entry.entity_id,
                device_id=entry.device_id,
                name=entry.name or entry.original_name or device.name or entry.entity_id,
                manufacturer=device.manufacturer or "",
                model=device.model or "",
            )
        )
    return candidates


def _has_label_factory(hass: HomeAssistant):
    """Mirrors grouping.py's EntityLookup.tags(): entity labels OR device labels."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    def has_label(light: CandidateLight) -> bool:
        entry = ent_reg.async_get(light.entity_id)
        if entry is not None and TWO_STEP_LABEL_ID in entry.labels:
            return True
        if light.device_id is not None:
            device = dev_reg.async_get(light.device_id)
            if device is not None and TWO_STEP_LABEL_ID in device.labels:
                return True
        return False

    return has_label


def unlabelled_lights(hass: HomeAssistant, entry: ConfigEntry) -> list[CandidateLight]:
    return find_unlabelled_two_step_lights(
        collect_candidates(hass), configured_patterns(entry), _has_label_factory(hass)
    )


@callback
def async_check(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Raise the repair if anything's missing its label, clear it if not."""
    missing = unlabelled_lights(hass, entry)
    if not missing:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ID)
        return

    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_ID,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_ID,
        translation_placeholders={"count": str(len(missing)), "lights": describe(missing)},
    )


def async_apply_label(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """The repair's actual fix - label every currently-missing light.

    Applied to the *device* rather than the entity wherever there is one.
    grouping.py accepts either, but the device is the more durable place:
    it survives an entity being renamed or removed and re-added, and one
    assignment covers every light entity the device exposes.

    Creates the label first if it doesn't exist. Matching is by label_id,
    and Home Assistant derives that id from the name when a label is
    created - so creating it here with the exact expected name is also
    what guarantees the id lines up, rather than relying on a user to
    hand-type it correctly.

    Returns the entity_ids that were covered, for the flow to report.
    """
    label_reg = lr.async_get(hass)
    if label_reg.async_get_label(TWO_STEP_LABEL_ID) is None:
        label_reg.async_create(
            name=TWO_STEP_LABEL_NAME,
            icon=TWO_STEP_LABEL_ICON,
            description=TWO_STEP_LABEL_DESCRIPTION,
        )

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    missing = unlabelled_lights(hass, entry)

    for light in missing:
        if light.device_id is not None and (device := dev_reg.async_get(light.device_id)) is not None:
            dev_reg.async_update_device(light.device_id, labels=device.labels | {TWO_STEP_LABEL_ID})
            continue
        if (existing := ent_reg.async_get(light.entity_id)) is not None:
            ent_reg.async_update_entity(light.entity_id, labels=existing.labels | {TWO_STEP_LABEL_ID})

    return [light.entity_id for light in missing]


def async_start_watching(hass: HomeAssistant, entry: ConfigEntry) -> CALLBACK_TYPE:
    """Check now, then re-check whenever the registries change.

    Coalesces bursts by hand rather than via helpers.debounce.Debouncer:
    pairing one device emits a flurry of registry writes, and re-walking
    the whole registry per write is wasted effort for something whose
    only output is a repair card. A single tracked timer is enough here,
    and unlike Debouncer it leaves nothing pending once it has fired -
    Debouncer schedules a further cooldown timer after every execution,
    which then has to be explicitly shut down or it outlives a reload.
    """
    pending: list[CALLBACK_TYPE] = []

    @callback
    def _fire(_now) -> None:
        pending.clear()
        async_check(hass, entry)

    @callback
    def _schedule(_event) -> None:
        if pending:
            # Already queued - let the in-flight timer cover this one
            # too, which is the entire point of coalescing.
            return
        pending.append(async_call_later(hass, _DEBOUNCE_SECONDS, _fire))

    unsubs = [
        hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _schedule),
        hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, _schedule),
    ]
    async_check(hass, entry)

    @callback
    def _unsub() -> None:
        for unsub in unsubs:
            unsub()
        for cancel in pending:
            cancel()
        pending.clear()
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ID)

    return _unsub
