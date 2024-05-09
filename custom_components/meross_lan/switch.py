import typing

from homeassistant.components import switch

from . import meross_entity as me
from .helpers.namespaces import EntityPollingStrategy
from .merossclient import const as mc, extract_dict_payloads

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .meross_device import DigestParseFunc, MerossDevice


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


class MLToggle(MLSwitch):

    namespace = mc.NS_APPLIANCE_CONTROL_TOGGLE
    key_namespace = mc.KEY_TOGGLE

    def __init__(self, manager: "MerossDevice"):
        # 2024-03-13: passing entitykey="0" instead of channel in order
        # to mantain unique_id compatibility with installations but
        # updating to new toggle entity model (where channel is None for this entity type)
        super().__init__(manager, None, "0", MLSwitch.DeviceClass.OUTLET)
        manager.register_parser(self.namespace, self)


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

    async def async_request_onoff(self, onoff: int):
        if await self.manager.async_request_ack(
            self.namespace,
            mc.METHOD_SET,
            {
                self.key_namespace: {
                    self.key_channel: self.channel,
                    self.key_value: onoff,
                }
            },
        ):
            self.update_onoff(onoff)


class MLToggleX_(MLToggleX):
    """
    Special 'disabled' ToggleX used when the device exposes other specialized entities
    together with some 'unmapped' ToggleX channels. The policy is to now create these
    plain switches too but, since they're likely 'no use' let them initially disabled.
    """

    # HA core entity attributes:
    entity_registry_enabled_default = False


def digest_init_togglex(device: "MerossDevice", togglex_digest: list) -> "DigestParseFunc":
    """[{ "channel": 0, "onoff": 1 }]"""
    # We don't initialize every switch/ToggleX here since the digest reported channels
    # might be mapped to more specialized entities:
    # this is true for lights (MLLight), garageDoor (MLGarage) and fan (MLFan) though
    # and maybe some more others.
    # In general, it is not very clear how and when these ToggleX entities are really needed
    # so we have some euristics in place to fix 'this and that'.
    # The general rule is to let the togglex namespace/channel be managed by the
    # aforementioned specialized entity, while, if no channel match exists, create a disabled
    # (by default) switch entity. When  switches are really switches (like mssXXX series) instead,
    # we'll setup proper MLToggleX (this is detected by the fact no specialized entity exists in
    # device definition)

    channels = {digest[mc.KEY_CHANNEL] for digest in togglex_digest}

    digest = device.descriptor.digest

    for key_digest in (mc.KEY_FAN, mc.KEY_GARAGEDOOR, mc.KEY_LIGHT):
        if key_digest in digest:
            for digest_channel in extract_dict_payloads(digest[key_digest]):
                channel = digest_channel.get(mc.KEY_CHANNEL)
                if channel in channels:
                    channels.remove(channel)

    # the fan controller 'map100' doesn't expose a fan in digest but it has one at channel 0
    if (mc.NS_APPLIANCE_CONTROL_FAN in device.descriptor.ability) and (mc.KEY_FAN not in digest):
        if 0 in channels:
            channels.remove(0)

    if len(channels) == len(togglex_digest):
        # no exotic device style..just plain switches
        for channel in channels:
            MLToggleX(device, channel)
    else:
        for channel in channels:
            MLToggleX_(device, channel)

    togglex_handler = device.get_handler(mc.NS_APPLIANCE_CONTROL_TOGGLEX)
    togglex_handler.register_entity_class(MLToggleX_)
    return togglex_handler.parse_list
