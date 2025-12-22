from typing import TYPE_CHECKING

from homeassistant.components import binary_sensor

from .helpers import entity as me

if TYPE_CHECKING:
    from typing import ClassVar, NotRequired

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, binary_sensor.DOMAIN)


class MLBinarySensor(me.MLBinaryEntity, binary_sensor.BinarySensorEntity):

    if TYPE_CHECKING:

        class Args(me.MLBinaryEntity.Args):
            device_class: NotRequired[binary_sensor.BinarySensorDeviceClass | None]

        # HA core entity attributes:
        _attr_device_class: ClassVar[binary_sensor.BinarySensorDeviceClass | None]

    PLATFORM = binary_sensor.DOMAIN
    DeviceClass = binary_sensor.BinarySensorDeviceClass
