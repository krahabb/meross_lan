"""Logbook descriptions for Meross LAN events."""

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback

EVENT_GARAGEDOOR_TRIGGER = "meross_lan_garagedoor_trigger"


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[..., None],
) -> None:
    """Register the garage door trigger event description with Logbook."""

    @callback
    def async_describe_garagedoor_trigger(event: Any) -> dict[str, str]:
        source = event.data["source"]
        return {
            "name": f"Garage Door triggered by {source}",
        }

    async_describe_event(
        "meross_lan",
        EVENT_GARAGEDOOR_TRIGGER,
        async_describe_garagedoor_trigger,
    )
