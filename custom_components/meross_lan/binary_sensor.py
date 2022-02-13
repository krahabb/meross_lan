
from homeassistant.components.binary_sensor import (
    DOMAIN as PLATFORM_BINARY_SENSOR,
    BinarySensorEntity,
    DEVICE_CLASS_WINDOW,
)

from .meross_entity import _MerossEntity, platform_setup_entry, platform_unload_entry


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_BINARY_SENSOR)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_BINARY_SENSOR)


class MLBinarySensor(_MerossEntity, BinarySensorEntity):

    PLATFORM = PLATFORM_BINARY_SENSOR

    @staticmethod
    def build_for_subdevice(subdevice: "MerossSubDevice", device_class: str):
        return MLBinarySensor(subdevice.hub, subdevice.id, device_class, device_class, subdevice)
