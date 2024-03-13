import typing

from homeassistant.components import binary_sensor

from . import meross_entity as me

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, binary_sensor.DOMAIN)


class MLBinarySensor(me.MerossBinaryEntity, binary_sensor.BinarySensorEntity):
    PLATFORM = binary_sensor.DOMAIN
    DeviceClass = binary_sensor.BinarySensorDeviceClass
