
from homeassistant.helpers.entity import Entity

from .meross_entity import _MerossEntity, _MerossHubEntity, platform_setup_entry, platform_unload_entry
from .const import PLATFORM_SENSOR


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_SENSOR)
    return

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_SENSOR)


class MerossLanSensor(_MerossEntity, Entity):

    PLATFORM = PLATFORM_SENSOR

    def __init__(self, device: 'MerossDevice', id: any, device_class: str):
        super().__init__(device, id, device_class)



class MerossLanHubSensor(_MerossHubEntity, Entity):

    PLATFORM = PLATFORM_SENSOR

    def __init__(self, subdevice: 'MerossSubDevice', device_class: str):
        super().__init__(subdevice, f"{subdevice.id}_{device_class}", device_class)
