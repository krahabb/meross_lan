from typing import TYPE_CHECKING

from homeassistant.components import button
from homeassistant.util import slugify

from .helpers.entity import MLEntity

if TYPE_CHECKING:
    from types import CoroutineType
    from typing import Any, Callable, ClassVar, NotRequired, Unpack

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.entity import ChannelType
    from .helpers.manager import EntityManager


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    MLEntity.platform_setup_entry(hass, config_entry, async_add_devices, button.DOMAIN)


class MLButton(MLEntity.PartialAvailableMixin, MLEntity, button.ButtonEntity):
    # MEPartialAvailableMixin is needed here since this entity state is not being updated
    # by our component. This will ensure (by default) the entity is available/unavailable
    # when the device is online/offline
    if TYPE_CHECKING:

        class Args(MLEntity.Args):
            name: str  # Override
            device_class: NotRequired[button.ButtonDeviceClass | None]

        # HA core entity attributes:
        _attr_device_class: ClassVar[button.ButtonDeviceClass | None]

    PLATFORM = button.DOMAIN
    DeviceClass = button.ButtonDeviceClass

    # HA core entity attributes:

    __slots__ = ()

    def __init__(
        self,
        channel: "ChannelType | None",
        manager: "EntityManager",
        press_func: "Callable[[], CoroutineType[Any, Any, None]]",
        **kwargs: "Unpack[MLButton.Args]",
    ):
        kwargs.setdefault("entity_key", f"button_{slugify(kwargs['name'])}")
        super().__init__(channel, manager, **kwargs)
        self.async_press = press_func

    def shutdown(self):
        super().shutdown()
        del self.async_press


class MLPersistentButton(MLButton):

    # HA core entity attributes:
    _attr_available = True
