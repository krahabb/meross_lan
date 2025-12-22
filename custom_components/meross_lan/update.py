from typing import TYPE_CHECKING

from homeassistant.components import update

from .helpers import entity as me
from .merossclient.cloudapi import LatestVersionType
from .merossclient.protocol import const as mc

if TYPE_CHECKING:
    from typing import ClassVar, NotRequired

    from .helpers.device import Device


async def async_setup_entry(hass, config_entry, async_add_devices):
    me.platform_setup_entry(hass, config_entry, async_add_devices, update.DOMAIN)


class MLUpdate(me.MEAlwaysAvailableMixin, me.MLEntity, update.UpdateEntity):
    if TYPE_CHECKING:

        class Args(me.MLEntity.Args):
            device_class: NotRequired[update.UpdateDeviceClass | None]

        manager: "Device"

        installed_version: str | None
        latest_version: str | None
        release_summary: str | None

        # HA core entity attributes:
        _attr_device_class: ClassVar[update.UpdateDeviceClass | None]

    PLATFORM = update.DOMAIN
    DeviceClass = update.UpdateDeviceClass

    # HA core entity attributes:
    _attr_device_class = DeviceClass.FIRMWARE
    entity_category = me.MLEntity.EntityCategory.DIAGNOSTIC

    __slots__ = (
        "installed_version",
        "latest_version",
        "release_summary",
    )

    def __init__(self, manager: "Device", latest_version: LatestVersionType):
        self.installed_version = manager.descriptor.firmwareVersion
        self.latest_version = latest_version.get(mc.KEY_VERSION)
        self.release_summary = latest_version.get(mc.KEY_DESCRIPTION)
        super().__init__(manager, None, "update_firmware")

    def _generate_unique_id(self):
        return None
