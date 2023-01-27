import typing

from homeassistant.components.binary_sensor import (
    DOMAIN as PLATFORM_BINARY_SENSOR,
    BinarySensorEntity,
    DEVICE_CLASS_WINDOW,
    DEVICE_CLASS_PROBLEM,
)

from . import meross_entity as me

if typing.TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.config_entries import ConfigEntry


async def async_setup_entry(
    hass: 'HomeAssistant', config_entry: 'ConfigEntry', async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_BINARY_SENSOR)


async def async_unload_entry(hass: 'HomeAssistant', config_entry: 'ConfigEntry'):
    return me.platform_unload_entry(hass, config_entry, PLATFORM_BINARY_SENSOR)


class MLBinarySensor(me.MerossEntity, BinarySensorEntity):

    PLATFORM = PLATFORM_BINARY_SENSOR
