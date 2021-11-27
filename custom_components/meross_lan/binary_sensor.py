
from homeassistant.components.binary_sensor import (
    DOMAIN as PLATFORM_BINARY_SENSOR,
    BinarySensorEntity
)

from .meross_entity import _MerossEntity, platform_setup_entry, platform_unload_entry


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_BINARY_SENSOR)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_BINARY_SENSOR)


class MerossLanBinarySensor(_MerossEntity, BinarySensorEntity):

    PLATFORM = PLATFORM_BINARY_SENSOR

    """
    def __init__(self, device: 'MerossDevice', _id: object, device_class: str, subdevice: 'MerossSubDevice'):
        super().__init__(device, _id, device_class, subdevice)
    """

    @staticmethod
    def build_for_subdevice(subdevice: "MerossSubDevice", device_class: str):
        return MerossLanBinarySensor(subdevice.hub, f"{subdevice.id}_{device_class}", device_class, subdevice)
