from __future__ import annotations

import typing

from homeassistant.components import update

from . import meross_entity as me
from .merossclient import const as mc
from .merossclient.cloudapi import LatestVersionType

if typing.TYPE_CHECKING:

    from .meross_device import MerossDevice


async def async_setup_entry(hass, config_entry, async_add_devices):
    me.platform_setup_entry(hass, config_entry, async_add_devices, update.DOMAIN)


class MLUpdate(me.MerossEntity, update.UpdateEntity):
    PLATFORM = update.DOMAIN
    DeviceClass = update.UpdateDeviceClass
    manager: MerossDevice
    # HA core entity attributes:
    _attr_available = True
    entity_category = me.EntityCategory.DIAGNOSTIC
    installed_version: str | None
    latest_version: str | None
    release_summary: str | None

    __slots__ = (
        "installed_version",
        "latest_version",
        "release_summary",
    )

    def __init__(self, manager: MerossDevice, latest_version: LatestVersionType):
        self.installed_version = manager.descriptor.firmwareVersion
        self.latest_version = latest_version.get(mc.KEY_VERSION)
        self.release_summary = latest_version.get(mc.KEY_DESCRIPTION)
        super().__init__(
            manager,
            None,
            "update_firmware",
            self.DeviceClass.FIRMWARE,
        )

    def set_available(self):
        pass

    def set_unavailable(self):
        pass

    def _generate_unique_id(self):
        return None
