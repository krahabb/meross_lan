from typing import TYPE_CHECKING

from homeassistant.components import binary_sensor

from .helpers import entity as mle

if TYPE_CHECKING:
    from typing import ClassVar, NotRequired, Unpack


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
            parent: mle.Device,
            /,
            **kwargs: Unpack[Args],
        ): ...

    __slots__ = mle.BinaryParser._calc_slots()


async_setup_entry = BinarySensor.platform_setup_entry
