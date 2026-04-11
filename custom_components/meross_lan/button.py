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
            press: NotRequired[Callable[[], None]]
            async_press: NotRequired[Callable[[], CoroutineType[Any, Any, None]]]
            name: str  # Override
            device_class: NotRequired[button.ButtonDeviceClass | None]

        @classmethod
        def build_sibling(cls, sibling: Entity.Sibling, /, **kwargs: Unpack[Args]): ...

    PLATFORM = button.DOMAIN
    DeviceClass = button.ButtonDeviceClass

    # HA core entity attributes:
    _attr_available = False

    def __init__(self, id, parent: "ConfigEntryManager", **kwargs: "Unpack[Args]"):
        """Provide either 'async_press' or 'press' callback to install an action on this button."""
        kwargs.setdefault("entity_key", f"button_{slugify(kwargs['name'])}")
        try:
            self.async_press = kwargs.pop("async_press")  # type: ignore
        except KeyError:
            press = kwargs.pop("press")  # type: ignore

            async def _async_press():
                press()

            self.async_press = _async_press

        Entity.__init__(self, id, parent, **kwargs)

    def shutdown(self):
        Entity.shutdown(self)
        del self.async_press


class PersistentButton(Button):

    # HA core entity attributes:
    _attr_available = True


async_setup_entry = Button.platform_setup_entry
