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
    entity_category = me.EntityCategory.DIAGNOSTIC
    installed_version: str | None = None
    latest_version: str | None = None
    release_summary: str | None = None

    def __init__(self, manager: MerossDevice):
        # TODO: invoke construction with actual state values so it gets added in 1-step to the
        # HA state machine
        super().__init__(manager, None, "update_firmware", self.DeviceClass.FIRMWARE)
        self.available = True

    @property
    def unique_id(self):
        # this is a 'transient' entity and we don't want it to persist.
        return None

    def set_available(self):
        pass

    def set_unavailable(self):
        pass
