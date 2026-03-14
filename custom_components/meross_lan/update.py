from typing import TYPE_CHECKING

from homeassistant.components import update
from homeassistant.exceptions import HomeAssistantError

from .helpers.entity import Entity
from .merossclient.protocol import namespaces as mn

if TYPE_CHECKING:
    from typing import ClassVar, Final, NotRequired

    from .helpers.device import BaseDevice


async def async_setup_entry(hass, config_entry, async_add_devices):
    Entity.platform_setup_entry(hass, config_entry, async_add_devices, update.DOMAIN)


class UpdateEntity(Entity, update.UpdateEntity):
    if TYPE_CHECKING:

        class Args(Entity.Args):
            device_class: NotRequired[update.UpdateDeviceClass | None]

        parent: Final[BaseDevice]  # type: ignore[override]

        # HA core entity attributes:
        _attr_device_class: ClassVar[update.UpdateDeviceClass | None]
        installed_version: str | None
        latest_version: str | None
        release_summary: str | None

    PLATFORM = update.DOMAIN
    DeviceClass = update.UpdateDeviceClass

    init_entity_key = "firmware_update"

    # HA core entity attributes:
    _attr_available = False

    __slots__ = (
        "installed_version",
        "latest_version",
        "release_summary",
        "title",
    )

    def __init__(self, device: "BaseDevice", /):
        self.device_class = update.UpdateDeviceClass.FIRMWARE
        self.supported_features = update.UpdateEntityFeature.INSTALL
        self.title = device.display_name
        self.unique_id = None
        self.installed_version, self.latest_version, self.release_summary = (
            device.get_upgrade_info()
        )
        super().__init__(None, device)

    def update_info(self, /):
        self.installed_version, self.latest_version, self.release_summary = (
            self.parent.get_upgrade_info()
        )
        self.flush_state()

    async def async_install(self, version: str | None, backup: bool, **kwargs):
        basedevice = self.parent
        if not basedevice.is_connected:
            raise HomeAssistantError("Device is offline")
        upgrade_payload = basedevice.get_upgrade_payload()
        if not upgrade_payload:
            raise HomeAssistantError("No upgrade available")
        await basedevice.async_request(
            *mn.Appliance_Control_Upgrade.request_set(upgrade_payload),
        )
