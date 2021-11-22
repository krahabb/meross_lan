from __future__ import annotations

from homeassistant.components.climate import DOMAIN as PLATFORM_CLIMATE

from .meross_entity import platform_setup_entry, platform_unload_entry


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_CLIMATE)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_CLIMATE)

