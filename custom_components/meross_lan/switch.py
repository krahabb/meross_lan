import typing

from homeassistant.components import switch

from . import meross_entity as me
from .helpers.namespaces import EntityPollingStrategy
from .merossclient import const as mc

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.namespaces import DigestParseFunc
    from .meross_device import MerossDevice


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, switch.DOMAIN)


class MLSwitch(me.MerossToggle, switch.SwitchEntity):
    """
    Generic HA switch: could either be a physical outlet or another 'logical' setting
    (see various config switches)
    Switches are sometimes polymorphic and their message dispatching is not 'set in stone'
    since the status updates are likely managed in higher level implementations or so.
    """

    PLATFORM = switch.DOMAIN
    DeviceClass = switch.SwitchDeviceClass


class PhysicalLockSwitch(MLSwitch):

    namespace = mc.NS_APPLIANCE_CONTROL_PHYSICALLOCK
    key_namespace = mc.KEY_LOCK

    # HA core entity attributes:
    entity_category = MLSwitch.EntityCategory.CONFIG

    def __init__(self, manager: "MerossDevice"):
        # right now we expect only 1 entity on channel == 0 (whatever)
        super().__init__(manager, 0, mc.KEY_LOCK, self.DeviceClass.SWITCH)
        manager.register_parser(self.namespace, self)
        EntityPollingStrategy(manager, self.namespace, self, item_count=1)

    # interface: MerossToggle
    async def async_request_onoff(self, onoff: int):
        if await self.manager.async_request_ack(
            self.namespace,
            mc.METHOD_SET,
            {
                self.key_namespace: [
                    {self.key_channel: self.channel, self.key_value: onoff}
                ]
            },
        ):
            self.update_onoff(onoff)


class MLToggle(MLSwitch):

    namespace = mc.NS_APPLIANCE_CONTROL_TOGGLE
    key_namespace = mc.KEY_TOGGLE

    def __init__(self, manager: "MerossDevice"):
        super().__init__(manager, 0, None, MLSwitch.DeviceClass.OUTLET)
        manager.register_parser(self.namespace, self)

    async def async_request_onoff(self, onoff: int):
        if await self.manager.async_request_ack(
            self.namespace,
            mc.METHOD_SET,
            {
                self.key_namespace: {
                    self.key_value: onoff,
                }
            },
        ):
            self.update_onoff(onoff)


def digest_init_toggle(device: "MerossDevice", digest: dict) -> "DigestParseFunc":
    """{"onoff": 0, "lmTime": 1645391086}"""
    MLToggle(device)
    return device.get_handler(mc.NS_APPLIANCE_CONTROL_TOGGLE).parse_generic


class MLToggleX(MLSwitch):

    namespace = mc.NS_APPLIANCE_CONTROL_TOGGLEX
    key_namespace = mc.KEY_TOGGLEX

    def __init__(self, manager: "MerossDevice", channel: object):
        super().__init__(manager, channel, None, MLSwitch.DeviceClass.OUTLET)
        manager.register_parser(self.namespace, self)


def digest_init_togglex(device: "MerossDevice", digest: list) -> "DigestParseFunc":
    """[{ "channel": 0, "onoff": 1 }]"""
    # we don't initialize any switch/ToggleX here since the digest reported channels
    # might be mapped to more specialized entities:
    # this is true for lights (MLLight), garageDoor (MLGarage) and fan (MLFan) though
    # and maybe some more others.
    # The idea is to let the mc.NS_APPLIANCE_CONTROL_TOGGLEX handler dynamically create
    # MLToggleX entities when no other (more specialized) entity has registered itself
    # as a valid handler for the mc.NS_APPLIANCE_CONTROL_TOGGLEX namespace
    togglex_handler = device.get_handler(mc.NS_APPLIANCE_CONTROL_TOGGLEX)
    togglex_handler.register_entity_class(MLToggleX)
    return togglex_handler.parse_list
