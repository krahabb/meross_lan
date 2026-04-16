from typing import TYPE_CHECKING

from homeassistant.components import binary_sensor

from .helpers import entity as mle

if TYPE_CHECKING:
    from typing import ClassVar, NotRequired, Unpack


class BinarySensorEntity(mle.BinaryEntity, binary_sensor.BinarySensorEntity):
    """Simple 'passive' binary sensor entity."""

    if TYPE_CHECKING:

        # HA core entity attributes:
        _attr_device_class: ClassVar[binary_sensor.BinarySensorDeviceClass | None]

        class Args(mle.BinaryEntity.Args):
            device_class: NotRequired[binary_sensor.BinarySensorDeviceClass | None]

    PLATFORM = binary_sensor.DOMAIN
    DeviceClass = binary_sensor.BinarySensorDeviceClass


class BinarySensorParser(mle.BinaryParser, BinarySensorEntity):
    """Binary sensor entity automatically linked to namespace handling/parsing."""

    if TYPE_CHECKING:

        class Args(BinarySensorEntity.Args, mle.BinaryParser.Args):
            pass


async_setup_entry = BinarySensorEntity.platform_setup_entry
