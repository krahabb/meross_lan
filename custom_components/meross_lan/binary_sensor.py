
from homeassistant.components.binary_sensor import BinarySensorEntity

from .meross_entity import _MerossEntity, _MerossHubEntity, platform_setup_entry, platform_unload_entry
from .const import PLATFORM_BINARY_SENSOR

async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_BINARY_SENSOR)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_BINARY_SENSOR)


class MerossLanBinarySensor(_MerossEntity, BinarySensorEntity):

    PLATFORM = PLATFORM_BINARY_SENSOR

    def __init__(self, device: 'MerossDevice', id: any, device_class: str):
        super().__init__(device, id, device_class)



class MerossLanHubBinarySensor(_MerossHubEntity, BinarySensorEntity):

    PLATFORM = PLATFORM_BINARY_SENSOR

    def __init__(self, subdevice: 'MerossSubDevice', device_class: str):
        super().__init__(subdevice, f"{subdevice.id}_{device_class}", device_class)
