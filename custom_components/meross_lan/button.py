import typing

from homeassistant.components import button

from .helpers import entity as me

if typing.TYPE_CHECKING:
    from types import CoroutineType
    from typing import Any, Callable, Unpack

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.manager import EntityManager


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, button.DOMAIN)


class MLButton(me.MEPartialAvailableMixin, me.MLEntity, button.ButtonEntity):
    # MEPartialAvailableMixin is needed here since this entity state is not being updated
    # by our component. This will ensure (by default) the entity is available/unavailable
    # when the device is online/offline

    PLATFORM = button.DOMAIN
    DeviceClass = button.ButtonDeviceClass

    # HA core entity attributes:

    __slots__ = ()

    def __init__(
        self,
        manager: "EntityManager",
        channel: object | None,
        entitykey: str | None,
        press_func: "Callable[[], CoroutineType[Any, Any, None]]",
        device_class: DeviceClass | None = None,
        **kwargs: "Unpack[MLButton.Args]",
    ):
        super().__init__(manager, channel, entitykey, device_class, **kwargs)
        self.async_press = press_func

    async def async_shutdown(self):
        self.async_press = None  # type: ignore BOOM!
        return await super().async_shutdown()


class MLPersistentButton(me.MEAlwaysAvailableMixin, MLButton):
    pass
