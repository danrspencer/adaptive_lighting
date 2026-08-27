"""Puts EVENT_LIGHT_OVERRIDDEN into the logbook.

Without this the event is still fired and still recorded (the recorder
listens on MATCH_ALL and keeps anything not explicitly excluded -
confirmed against its own core.py), but you'd have to go looking for it
with a template or a database query. Describing it here makes it appear
in the light's own logbook timeline, interleaved with the state changes
that surround it, which is where anyone actually investigating "why did
this light stop tracking" will be looking.

The description deliberately reads as a hand-over rather than a failure.
A light being taken is a supported outcome - something else asked for it
and adaptive lighting stepped back.
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.logbook import LOGBOOK_ENTRY_ENTITY_ID, LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_LIGHT_OVERRIDDEN


def _describe_owner(owner_id: str | None) -> str:
    """"automation.kitchen_lights" -> "kitchen_lights". Matches what the
    write-tracking card shows, so the same name appears in both places."""
    if not owner_id:
        return "adaptive lighting"
    return owner_id.split(".", 1)[-1] if "." in owner_id else owner_id


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Describe adaptive lighting logbook events."""

    @callback
    def async_describe_override(event: Event) -> dict[str, str]:
        data = event.data
        live = data.get("live") or {}
        latest = data.get("latest") or {}
        target = latest.get("target") or {}
        # The whole point of the event: what was asked for against what is
        # actually there. Stated inline so the timeline entry is useful on
        # its own, without expanding the raw event.
        if target:
            asked = f"{target.get('brightness')}/{target.get('color_temp_kelvin') or target.get('rgb_color')}"
            found = f"{live.get('brightness')}/{live.get('color_temp_kelvin') or live.get('rgb_color')}"
            detail = f" (last asked for {asked}, found {found})"
        else:
            detail = ""
        return {
            LOGBOOK_ENTRY_NAME: _describe_owner(data.get("owner_id")),
            LOGBOOK_ENTRY_MESSAGE: f"released this light to something else{detail}",
            LOGBOOK_ENTRY_ENTITY_ID: data["entity_id"],
        }

    async_describe_event(DOMAIN, EVENT_LIGHT_OVERRIDDEN, async_describe_override)
