from typing import TYPE_CHECKING

from homeassistant.components import update
from homeassistant.exceptions import HomeAssistantError

from .helpers.entity import Entity
from .merossclient.protocol import namespaces as mn

if TYPE_CHECKING:
    from typing import ClassVar, Final, NotRequired

    from .helpers.device import ChannelType, Device


async def async_setup_entry(hass, config_entry, async_add_devices):
    Entity.platform_setup_entry(hass, config_entry, async_add_devices, update.DOMAIN)


class UpdateEntity(Entity, update.UpdateEntity):
    if TYPE_CHECKING:
        parent: Final[Device]  # type: ignore[override]
        # HA core entity attributes:
        _attr_device_class: ClassVar[update.UpdateDeviceClass | None]
        installed_version: str | None
        latest_version: str | None
        release_summary: str | None

        class Args(Entity.Args):
            device_class: NotRequired[update.UpdateDeviceClass | None]

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

    def __init__(self, channel: "ChannelType | None", device: "Device", /):
        self.device_class = update.UpdateDeviceClass.FIRMWARE
        self.supported_features = update.UpdateEntityFeature.INSTALL
        self.title = device.display_name
        self.unique_id = None
        self.installed_version, self.latest_version, self.release_summary = (
            device.get_upgrade_info()
        )
        Entity.__init__(self, channel, device)

    def flush_state(self):
        self.installed_version, self.latest_version, self.release_summary = (
            self.parent.get_upgrade_info()
        )
        super().flush_state()

    async def async_install(self, version: str | None, backup: bool, **kwargs):
        device = self.parent
        if not device.is_connected:
            raise HomeAssistantError("Device is offline")
        upgrade_payload = device.get_upgrade_payload()
        if not upgrade_payload:
            raise HomeAssistantError("No upgrade available")
        await device.async_request(
            *mn.Appliance_Control_Upgrade.request_set(upgrade_payload),
        )
