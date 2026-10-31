"""
Logbook descriptions for MSG150 garage door trigger events.
"""

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback

EVENT_GARAGEDOOR_TRIGGER = "meross_lan_garagedoor_trigger"


@callback
def async_describe_events(
    hass: HomeAssistant, async_describe_event: Callable[..., None]
) -> None:
    """Register garage door trigger descriptions with Logbook."""

    @callback
    def describe_trigger(event: Any) -> dict[str, str]:
        # Activity details shows the context name for these custom events.
        return {"name": f"Garage Door triggered by {event.data['source']}"}

    async_describe_event("meross_lan", EVENT_GARAGEDOOR_TRIGGER, describe_trigger)
