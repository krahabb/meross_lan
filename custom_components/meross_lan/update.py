from __future__ import annotations

import typing

from homeassistant.components import update

from . import meross_entity as me

if typing.TYPE_CHECKING:
    from typing import Final

    from .meross_device import MerossDevice


async def async_setup_entry(hass, config_entry, async_add_devices):
    me.platform_setup_entry(hass, config_entry, async_add_devices, update.DOMAIN)


class MLUpdate(me.MerossEntity, update.UpdateEntity):
    PLATFORM = update.DOMAIN
    DeviceClass = update.UpdateDeviceClass

    # HA core entity attributes:
    available: Final[bool] = True
    entity_category = me.EntityCategory.DIAGNOSTIC
    installed_version: str | None = None
    latest_version: str | None = None
    release_summary: str | None = None

    def __init__(self, manager: MerossDevice):
        super().__init__(manager, None, "update_firmware", self.DeviceClass.FIRMWARE)

    @property
    def unique_id(self):
        # this is a 'transient' entity and we don't want it to persist.
        return None

    def set_unavailable(self):
        pass
