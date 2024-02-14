from __future__ import annotations

import typing

from homeassistant.components import button

from . import meross_entity as me

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.manager import EntityManager
    from .meross_device import MerossDevice


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, button.DOMAIN)


class MLButton(me.MerossEntity, button.ButtonEntity):
    PLATFORM = button.DOMAIN
    DeviceClass = button.ButtonDeviceClass

    # HA core entity attributes:
    entity_category = me.EntityCategory.CONFIG

    __slots__ = ()

    def __init__(
        self,
        manager: EntityManager,
        channel: object | None,
        entitykey: str | None = None,
        device_class: DeviceClass | None = None,
    ):
        super().__init__(manager, channel, entitykey, device_class)

    # interface: button.buttonEntity
    async def async_press(self) -> None:
        """Press the button.(BOOM!)"""
        pass


class _MLUnbindButton(MLButton):
    """
    This button, will send the Appliance.Control.Unbind PUSH
    through the broker thus completely resetting the binding of
    the device and removing it from the Meross account (if Meross paired)
    pretty destructive...

    BTW:
    I think having a simple UI button which could just be pressed and BOOM!
    is a very bad idea from a UX perspective. Any trick (like raising an
    error on first attempt or so) could just prove to be useless since people
    don't read pop-ups. I'll move this feature to a redesign of the OptionsFlow
    (lot of work to be done...sadly..) by introducing a menu and letting the
    user consciously step by step proceed to destruction.

    This code is left for reference...buttons might prove to be useful in
    other contexts
    """

    manager: MerossDevice

    def __init__(
        self,
        manager: MerossDevice,
    ):
        super().__init__(manager, None, "unbind", None)
