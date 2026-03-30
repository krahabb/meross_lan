from typing import TYPE_CHECKING

from homeassistant.components import button
from homeassistant.util import slugify

from .helpers.entity import Entity

if TYPE_CHECKING:
    from types import CoroutineType
    from typing import Any, Callable, ClassVar, NotRequired, Unpack

    from .helpers.entity import ChannelType
    from .helpers.manager import ConfigEntryManager


class Button(Entity, button.ButtonEntity):
    # MEPartialAvailableMixin is needed here since this entity state is not being updated
    # by our component. This will ensure (by default) the entity is available/unavailable
    # when the device is online/offline
    if TYPE_CHECKING:

        # HA core entity attributes:
        _attr_device_class: ClassVar[button.ButtonDeviceClass | None]

        class Args(Entity.Args):
            name: str  # Override
            device_class: NotRequired[button.ButtonDeviceClass | None]

    PLATFORM = button.DOMAIN
    DeviceClass = button.ButtonDeviceClass

    # HA core entity attributes:
    _attr_available = False

    def __init__(
        self,
        channel: "ChannelType | None",
        parent: "ConfigEntryManager",
        press_func: "Callable[[], CoroutineType[Any, Any, None]]",
        **kwargs: "Unpack[Button.Args]",
    ):
        kwargs.setdefault("entity_key", f"button_{slugify(kwargs['name'])}")
        Entity.__init__(self, channel, parent, **kwargs)
        self.async_press = press_func

    def shutdown(self):
        Entity.shutdown(self)
        del self.async_press


class PersistentButton(Button):

    # HA core entity attributes:
    _attr_available = True


async_setup_entry = Button.platform_setup_entry
