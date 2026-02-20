from typing import TYPE_CHECKING

from homeassistant.components import update
from homeassistant.exceptions import HomeAssistantError

from .helpers.entity import MLEntity
from .merossclient.protocol import namespaces as mn

if TYPE_CHECKING:
    from typing import ClassVar, NotRequired

    from .helpers.device import BaseDevice


async def async_setup_entry(hass, config_entry, async_add_devices):
    MLEntity.platform_setup_entry(hass, config_entry, async_add_devices, update.DOMAIN)


class MLUpdate(MLEntity.PartialAvailableMixin, MLEntity, update.UpdateEntity):
    if TYPE_CHECKING:

        class Args(MLEntity.Args):
            device_class: NotRequired[update.UpdateDeviceClass | None]

        manager: BaseDevice

        # HA core entity attributes:
        _attr_device_class: ClassVar[update.UpdateDeviceClass | None]
        installed_version: str | None
        latest_version: str | None
        release_summary: str | None

    PLATFORM = update.DOMAIN
    DeviceClass = update.UpdateDeviceClass

    ENTITY_KEY = "firmware_update"

    # HA core entity attributes:
    _attr_device_class = DeviceClass.FIRMWARE
    _attr_supported_features = update.UpdateEntityFeature.INSTALL
    entity_category = MLEntity.EntityCategory.DIAGNOSTIC

    __slots__ = (
        "installed_version",
        "latest_version",
        "release_summary",
        "supported_features",
        "title",
    )

    def __init__(self, manager: "BaseDevice", /):
        self.supported_features = self._attr_supported_features
        self.title = manager.display_name
        self.unique_id = None
        self.installed_version, self.latest_version, self.release_summary = (
            manager.get_upgrade_info()
        )
        super().__init__(None, manager)

    def update_info(self, /):
        self.installed_version, self.latest_version, self.release_summary = (
            self.manager.get_upgrade_info()
        )
        self.flush_state()

    @MLEntity.ha_action
    async def async_install(self, version: str | None, backup: bool, **kwargs):
        basedevice = self.manager
        if not basedevice.is_connected:
            raise HomeAssistantError("Device is offline")
        upgrade_payload = basedevice.get_upgrade_payload()
        if not upgrade_payload:
            raise HomeAssistantError("No upgrade available")
        await basedevice.async_request(
            *mn.Appliance_Control_Upgrade.request_set(upgrade_payload),
        )
