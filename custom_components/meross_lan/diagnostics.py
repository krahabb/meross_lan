from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any, Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.manager import ConfigEntryManager


async def async_get_device_diagnostics(
    hass: "HomeAssistant", config_entry: "ConfigEntry[ConfigEntryManager]", device
) -> "Mapping[str, Any]":

    return await config_entry.runtime_data.async_get_diagnostics()


async def async_get_config_entry_diagnostics(
    hass: "HomeAssistant", config_entry: "ConfigEntry[ConfigEntryManager]"
) -> "Mapping[str, Any]":

    return await config_entry.runtime_data.async_get_diagnostics()
