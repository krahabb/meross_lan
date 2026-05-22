from typing import TYPE_CHECKING

from homeassistant.components import update
from homeassistant.exceptions import HomeAssistantError

from .helpers.entity import Entity
from .merossclient.protocol import namespaces as mn

if TYPE_CHECKING:
    from typing import ClassVar, Final, NotRequired

    from .helpers.device import BaseDevice, Device
    from .merossclient.cloudapi import LatestVersionType


class UpdateEntity(Entity, update.UpdateEntity):
    if TYPE_CHECKING:
        parent: Final[Device]  # type: ignore[override]
        device: Final[BaseDevice]
        """The PhysicalDevice associated with this entity. Could be either a subdevice or a plain device."""
        latest_version_info: LatestVersionType  # latest firmware version available for the device (if any).
        # HA core entity attributes:
        _attr_device_class: ClassVar[update.UpdateDeviceClass | None]
        in_progress: bool
        installed_version: str | None
        latest_version: str | None
        release_summary: str | None

        class Args(Entity.Args):
            device_class: NotRequired[update.UpdateDeviceClass | None]

    PLATFORM = update.DOMAIN
    DeviceClass = update.UpdateDeviceClass

    init_entity_key = "firmware_update"

    __slots__ = (
        "device",
        "latest_version_info",
        "in_progress",
        "installed_version",
        "latest_version",
        "release_summary",
        "title",
    )

    def __init__(
        self,
        device: "BaseDevice",
        parent: "Device",
        latest_version_info: "LatestVersionType",
        /,
    ):
        self.device = device
        self.latest_version_info = latest_version_info
        self.device_class = update.UpdateDeviceClass.FIRMWARE
        # self.supported_features = (
        #    update.UpdateEntityFeature.INSTALL | update.UpdateEntityFeature.PROGRESS
        # )
        self.title = device.display_name
        self.in_progress = False
        self.installed_version, self.latest_version, self.release_summary = (
            device.descriptor.get_upgrade_info(latest_version_info)
        )
        Entity.__init__(
            self,
            device.id if device is not parent else None,
            parent,
            device_info=device.device_info,
        )
        self.unique_id = None  # override
        device.update_firmware = self

    def update_latest_version(self, latest_version_info: "LatestVersionType"):
        self.latest_version_info = latest_version_info
        self.flush_state()

    def flush_state(self):
        self.installed_version, self.latest_version, self.release_summary = (
            self.device.descriptor.get_upgrade_info(self.latest_version_info)
        )
        if self.installed_version == self.latest_version:
            self.in_progress = False
        Entity.flush_state(self)

    async def async_install(self, *args, **kwargs):
        raise HomeAssistantError("Temporarily unsupproted")
        device = self.device
        if not device.is_connected:
            raise HomeAssistantError("Device is offline")
        upgrade_payload = device.descriptor.get_upgrade_payload(
            self.latest_version_info
        )
        if not upgrade_payload:
            raise HomeAssistantError("No upgrade available")
        await device.async_request(
            *mn.Appliance_Control_Upgrade.request_set(upgrade_payload),
        )
        self.in_progress = True


async_setup_entry = UpdateEntity.platform_setup_entry
