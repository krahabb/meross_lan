from typing import TYPE_CHECKING

from homeassistant.components import binary_sensor

from .helpers import entity as mle

if TYPE_CHECKING:
    from typing import ClassVar, NotRequired, Unpack

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    mle.Entity.platform_setup_entry(
        hass, config_entry, async_add_devices, binary_sensor.DOMAIN
    )


class BinarySensor(mle.BinaryEntity, binary_sensor.BinarySensorEntity):
    """Simple 'passive' binary sensor entity."""

    if TYPE_CHECKING:

        # HA core entity attributes:
        _attr_device_class: ClassVar[binary_sensor.BinarySensorDeviceClass | None]

        class Args(mle.BinaryEntity.Args):
            device_class: NotRequired[binary_sensor.BinarySensorDeviceClass | None]

    PLATFORM = binary_sensor.DOMAIN
    DeviceClass = binary_sensor.BinarySensorDeviceClass


class BinarySensorParser(mle.BinaryParser, BinarySensor):
    """Binary sensor entity automatically linked to namespace handling/parsing."""

    if TYPE_CHECKING:

        class Args(BinarySensor.Args, mle.BinaryParser.Args):
            pass

        def __init__(
            self,
            channel: mle.ChannelType | None,
            parent: mle.BaseDevice,
            /,
            **kwargs: Unpack[Args],
        ): ...
