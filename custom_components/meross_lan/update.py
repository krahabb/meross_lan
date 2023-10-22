from __future__ import annotations

import typing

from homeassistant.components import update

from . import meross_entity as me

if typing.TYPE_CHECKING:
    from .meross_device import MerossDevice


async def async_setup_entry(hass, config_entry, async_add_devices):
    me.platform_setup_entry(hass, config_entry, async_add_devices, update.DOMAIN)


class MLUpdate(me.MerossEntity, update.UpdateEntity):
    PLATFORM = update.DOMAIN
    DeviceClass = update.UpdateDeviceClass

    _attr_entity_category = me.EntityCategory.DIAGNOSTIC

    def __init__(self, manager: MerossDevice):
        super().__init__(manager, None, "update_firmware", self.DeviceClass.FIRMWARE)

    @property
    def available(self):
        return True

    @property
    def supported_features(self):
        """Flag supported features."""
        return update.UpdateEntityFeature.INSTALL

    async def async_install(self, version: str | None, backup: bool, **kwargs) -> None:
        self.warning(
            "The firmware update feature is not (yet) available: use the Meross app to carry the process"
        )

    def set_unavailable(self):
        pass
