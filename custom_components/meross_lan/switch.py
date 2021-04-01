
from typing import Any, Callable, Dict, List, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.typing import HomeAssistantType
from homeassistant.components.switch import SwitchEntity, DEVICE_CLASS_OUTLET

from .const import DOMAIN, CONF_DEVICE_ID
from .meross_entity import _MerossToggle
from .logger import LOGGER

async def async_setup_entry(hass: HomeAssistantType, config_entry: ConfigEntry, async_add_devices):
    device_id = config_entry.data[CONF_DEVICE_ID]
    device = hass.data[DOMAIN].devices[device_id]
    async_add_devices([entity for entity in device.entities.values() if isinstance(entity, MerossLanSwitch)])
    LOGGER.debug("async_setup_entry device_id = %s - platform = switch", device_id)
    return

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    LOGGER.debug("async_unload_entry device_id = %s - platform = switch", config_entry.data[CONF_DEVICE_ID])
    return True


class MerossLanSwitch(_MerossToggle, SwitchEntity):
    def __init__(self, meross_device: object, channel: int, m_toggle_set, m_toggle_get):
        super().__init__(meross_device, channel, DEVICE_CLASS_OUTLET, m_toggle_set, m_toggle_get)
        meross_device.has_switches = True

