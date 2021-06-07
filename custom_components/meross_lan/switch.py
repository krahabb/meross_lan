
from homeassistant.components.switch import SwitchEntity, DEVICE_CLASS_OUTLET

from .meross_entity import _MerossToggle, platform_setup_entry, platform_unload_entry
from .const import PLATFORM_SWITCH


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_SWITCH)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_SWITCH)



class MerossLanSwitch(_MerossToggle, SwitchEntity):

    PLATFORM = PLATFORM_SWITCH

    def __init__(self, device: 'MerossDevice', id: object, toggle_ns: str, toggle_key: str):
        super().__init__(device, id, DEVICE_CLASS_OUTLET, toggle_ns, toggle_key)

